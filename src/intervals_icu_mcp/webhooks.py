"""Intervals.icu webhook receiver (``POST /webhooks/intervals``).

Intervals.icu sends webhooks to OAuth apps for the athletes who authorised them. This
module authenticates a delivery with a shared secret, extracts a small summary of each
event (type, athlete, timestamp, IDs — never full payloads), appends it to a capped
JSONL log under ``DATA_DIR``, and optionally forwards the summary to a Home Assistant
webhook. The ``icu_get_recent_events`` tool reads the log back.

Authentication: the brief configures the secret as the app's "Webhook Authorization
Header", so the ``Authorization`` header must equal the secret (``Bearer <secret>`` is
accepted too). The Intervals.icu API docs instead describe a ``secret`` field in the
JSON body, so that is accepted as well. Every comparison is constant-time.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import normalize_athlete_id

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .remote import RemoteSettings

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/intervals"
EVENT_LOG_NAME = "webhook-events.jsonl"
DEFAULT_LOG_MAX_BYTES = 1024 * 1024
MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS_PER_DELIVERY = 500
MAX_IDS_PER_KEY = 100
HA_TIMEOUT_SECONDS = 10.0

#: Types the coach cares about (and the brief requires). Others are logged and stored.
KNOWN_EVENT_TYPES = frozenset(
    {
        "ACTIVITY_UPLOADED",
        "ACTIVITY_ANALYZED",
        "ACTIVITY_UPDATED",
        "ACTIVITY_DELETED",
        "ACTIVITY_ACHIEVEMENTS",
        "WELLNESS_UPDATED",
        "CALENDAR_UPDATED",
        "CALENDAR_EVENT_UPDATED",  # legacy, superseded by CALENDAR_UPDATED
        "CALENDAR_EVENT_DELETED",  # legacy
        "SPORT_SETTINGS_UPDATED",
        "FITNESS_UPDATED",
        "APP_SCOPE_CHANGED",
    }
)
_META_KEYS = {"type", "athlete_id", "athleteId", "timestamp", "secret"}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- parsing


def _scalar_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)) and str(value).strip():
        return str(value).strip()[:64]
    return None


def extract_ids(event: dict[str, Any]) -> dict[str, list[str]]:
    """Collect IDs from an event without storing its payload.

    ``{"activity": {"id": "i1"}}`` -> ``{"activity": ["i1"]}``;
    ``{"records": [{"id": 5}, …]}`` -> ``{"records": ["5", …]}``;
    ``{"activity_id": "i1"}`` / ``{"event_ids": [1, 2]}`` are kept as-is.
    """
    ids: dict[str, list[str]] = {}
    for key, value in event.items():
        if key in _META_KEYS:
            continue
        found: list[str] = []
        if isinstance(value, dict):
            item_id = _scalar_id(cast(dict[str, Any], value).get("id"))
            if item_id:
                found.append(item_id)
        elif isinstance(value, list):
            for item in cast(list[Any], value)[:MAX_IDS_PER_KEY]:
                item_id = (
                    _scalar_id(cast(dict[str, Any], item).get("id"))
                    if isinstance(item, dict)
                    else _scalar_id(item)
                    if key.endswith("_ids") or key == "ids"
                    else None
                )
                if item_id:
                    found.append(item_id)
        elif key.endswith("_id") or key == "id":
            item_id = _scalar_id(value)
            if item_id:
                found.append(item_id)
        if found:
            ids[key] = found
    return ids


def summarize_delivery(body: Any, received_at: datetime) -> list[dict[str, Any]]:
    """Turn a webhook body into event summaries.

    Accepts ``{"events": [...]}`` (documented shape), a bare list of events, or a
    single event object.
    """
    if isinstance(body, dict) and isinstance(cast(dict[str, Any], body).get("events"), list):
        raw_events: list[Any] = cast(dict[str, Any], body)["events"]
    elif isinstance(body, list):
        raw_events = cast(list[Any], body)
    elif isinstance(body, dict):
        raw_events = [body]
    else:
        raw_events = []

    summaries: list[dict[str, Any]] = []
    for raw in raw_events[:MAX_EVENTS_PER_DELIVERY]:
        if not isinstance(raw, dict):
            continue
        event = cast(dict[str, Any], raw)
        event_type = str(event.get("type") or "UNKNOWN").strip().upper()[:64]
        athlete = event.get("athlete_id", event.get("athleteId"))
        timestamp = event.get("timestamp")
        summaries.append(
            {
                "received_at": _iso(received_at),
                "timestamp": str(timestamp)[:40] if timestamp else None,
                "type": event_type,
                "known_type": event_type in KNOWN_EVENT_TYPES,
                "athlete_id": normalize_athlete_id(athlete) if athlete else None,
                "ids": extract_ids(event),
            }
        )
    return summaries


def is_authorized(authorization: str, body_secret: Any, secret: str) -> bool:
    """Constant-time check of the Authorization header (or body ``secret``)."""
    expected = secret.encode()
    header = authorization.strip()
    bearer = header[7:].strip() if header.lower().startswith("bearer ") else ""
    ok = hmac.compare_digest(header.encode(), expected)
    ok = hmac.compare_digest(bearer.encode(), expected) or ok
    if isinstance(body_secret, str):
        ok = hmac.compare_digest(body_secret.encode(), expected) or ok
    return ok


# --------------------------------------------------------------------------- log


class EventLog:
    """Append-only JSONL log with size-based rotation (one ``.1`` backup)."""

    def __init__(self, path: Path, max_bytes: int = DEFAULT_LOG_MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max(4096, max_bytes)
        self._lock = asyncio.Lock()

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".1")

    def _append_sync(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            os.replace(self.path, self.backup_path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.writelines(lines)

    async def append(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        lines = [json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in records]
        async with self._lock:
            await asyncio.to_thread(self._append_sync, lines)

    def read(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in (self.backup_path, self.path):
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        parsed: Any = json.loads(line)
                    except ValueError:
                        continue  # tolerate a torn last line
                    if isinstance(parsed, dict):
                        records.append(cast(dict[str, Any], parsed))
        return records


def event_log_for(settings: RemoteSettings) -> EventLog:
    try:
        max_bytes = int(os.environ.get("WEBHOOK_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))
    except ValueError:
        max_bytes = DEFAULT_LOG_MAX_BYTES
    return EventLog(settings.data_path / EVENT_LOG_NAME, max_bytes)


def parse_since(value: str | None, default: timedelta = timedelta(days=1)) -> datetime:
    """``YYYY-MM-DD`` or an ISO datetime (naive = UTC). None = now - default."""
    if not value:
        return _now() - default
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def query_events(
    log: EventLog,
    *,
    since: datetime,
    event_types: set[str] | None = None,
    athlete_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Events received at or after ``since``, newest first."""
    selected: list[dict[str, Any]] = []
    for record in log.read():
        try:
            received = parse_since(str(record.get("received_at")))
        except ValueError:
            continue
        if received < since:
            continue
        if event_types and record.get("type") not in event_types:
            continue
        if athlete_id and record.get("athlete_id") != athlete_id:
            continue
        selected.append(record)
    selected.sort(key=lambda r: str(r.get("received_at")), reverse=True)
    return selected[:limit]


# --------------------------------------------------------------------------- receiver


@dataclass
class WebhookReceiver:
    secret: str
    log: EventLog
    allowed_athlete_ids: frozenset[str]
    ha_webhook_url: str = ""

    def __post_init__(self) -> None:
        self._background: set[asyncio.Task[None]] = set()

    async def _forward_to_home_assistant(self, summaries: list[dict[str, Any]]) -> None:
        try:
            async with httpx.AsyncClient(timeout=HA_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    self.ha_webhook_url, json={"source": "intervals.icu", "events": summaries}
                )
            if response.status_code >= 400:
                logger.warning("Home Assistant webhook returned HTTP %d", response.status_code)
        except httpx.HTTPError as e:
            # Never log the URL: a Home Assistant webhook ID is a secret.
            logger.warning("Home Assistant webhook failed (%s)", type(e).__name__)

    def _schedule_forward(self, summaries: list[dict[str, Any]]) -> None:
        task = asyncio.create_task(self._forward_to_home_assistant(summaries))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def handle(self, request: Request) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload too large"}, status_code=413)

        body: Any = None
        parse_error = False
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            parse_error = True
        body_secret = cast(dict[str, Any], body).get("secret") if isinstance(body, dict) else None
        if not is_authorized(request.headers.get("authorization", ""), body_secret, self.secret):
            logger.warning("Rejected webhook delivery: bad or missing secret")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if parse_error or body is None:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        summaries = summarize_delivery(body, _now())
        accepted: list[dict[str, Any]] = []
        for summary in summaries:
            athlete = summary.get("athlete_id")
            if self.allowed_athlete_ids and athlete not in self.allowed_athlete_ids:
                logger.info("Dropped webhook %s for non-allowlisted athlete", summary["type"])
                continue
            if not summary["known_type"]:
                logger.info("Unknown webhook type %s (stored)", summary["type"])
            accepted.append(summary)

        await self.log.append(accepted)
        logger.info(
            "Webhook delivery: %d event(s) stored (%s)",
            len(accepted),
            ", ".join(sorted({s["type"] for s in accepted})) or "none",
        )
        if accepted and self.ha_webhook_url:
            self._schedule_forward(accepted)
        return JSONResponse({"status": "ok", "stored": len(accepted)})


def build_receiver(settings: RemoteSettings) -> WebhookReceiver:
    from .oauth import parse_allowlist

    return WebhookReceiver(
        secret=settings.intervals_webhook_secret,
        log=event_log_for(settings),
        allowed_athlete_ids=parse_allowlist(settings.allowed_athlete_ids),
        ha_webhook_url=settings.ha_webhook_url.strip(),
    )


def register_webhook_route(server: FastMCP[Any], settings: RemoteSettings) -> WebhookReceiver:
    receiver = build_receiver(settings)
    server.custom_route(WEBHOOK_PATH, methods=["POST"], include_in_schema=False)(receiver.handle)
    return receiver
