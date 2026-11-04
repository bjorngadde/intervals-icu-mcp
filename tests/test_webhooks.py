"""Webhook receiver, event log and icu_get_recent_events."""

import asyncio
import json
import logging
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastmcp import FastMCP

from intervals_icu_mcp.auth import ICUConfig
from intervals_icu_mcp.remote import RemoteConfigError, RemoteSettings, build_remote_app
from intervals_icu_mcp.tools.webhook_events import get_recent_events
from intervals_icu_mcp.webhooks import (
    EVENT_LOG_NAME,
    EventLog,
    extract_ids,
    is_authorized,
    register_webhook_route,
    summarize_delivery,
)

SECRET = "webhook-shared-secret-placeholder"
HA_URL = "http://homeassistant.local:8123/api/webhook/ha-hook-id-placeholder"


def _delivery(*events: dict) -> dict:
    return {"events": list(events)}


def _event(event_type: str, **extra) -> dict:
    return {
        "athlete_id": "987654",
        "type": event_type,
        "timestamp": "2026-11-04T06:12:00.000+00:00",
        **extra,
    }


@asynccontextmanager
async def _client(
    tmp_path: Path, **overrides: str
) -> AsyncIterator[tuple[httpx.AsyncClient, Path]]:
    values = {"intervals_webhook_secret": SECRET, "data_dir": str(tmp_path)}
    values.update(overrides)
    settings = RemoteSettings(**values)
    server = FastMCP("webhook-test")
    receiver = register_webhook_route(server, settings)
    app = server.http_app(path="/mcp")
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            client.receiver = receiver  # type: ignore[attr-defined]
            yield client, tmp_path / EVENT_LOG_NAME


def _read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class TestAuthentication:
    @pytest.mark.parametrize("header", [SECRET, f"Bearer {SECRET}", f"bearer {SECRET}"])
    async def test_valid_header_accepted(self, tmp_path, header):
        async with _client(tmp_path) as (client, log):
            resp = await client.post(
                "/webhooks/intervals",
                json=_delivery(_event("ACTIVITY_UPLOADED", activity={"id": "i1"})),
                headers={"Authorization": header},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "stored": 1}
        assert len(_read_log(log)) == 1

    async def test_secret_in_body_accepted(self, tmp_path):
        async with _client(tmp_path) as (client, log):
            body = {**_delivery(_event("WELLNESS_UPDATED")), "secret": SECRET}
            resp = await client.post("/webhooks/intervals", json=body)
        assert resp.status_code == 200
        assert "secret" not in log.read_text()

    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "wrong"}, {"Authorization": SECRET + "x"}, {"Authorization": ""}],
    )
    async def test_bad_or_missing_secret_rejected(self, tmp_path, headers):
        async with _client(tmp_path) as (client, log):
            resp = await client.post(
                "/webhooks/intervals",
                json={**_delivery(_event("ACTIVITY_UPLOADED")), "secret": "nope"},
                headers=headers,
            )
        assert resp.status_code == 401
        assert not log.exists()

    async def test_invalid_json_with_valid_secret_is_400(self, tmp_path):
        async with _client(tmp_path) as (client, _):
            resp = await client.post(
                "/webhooks/intervals", content=b"{not json", headers={"Authorization": SECRET}
            )
        assert resp.status_code == 400

    async def test_invalid_json_without_secret_is_401(self, tmp_path):
        async with _client(tmp_path) as (client, _):
            resp = await client.post("/webhooks/intervals", content=b"{not json")
        assert resp.status_code == 401

    async def test_oversized_body_is_413(self, tmp_path):
        async with _client(tmp_path) as (client, _):
            resp = await client.post(
                "/webhooks/intervals",
                content=b"x" * (1024 * 1024 + 1),
                headers={"Authorization": SECRET},
            )
        assert resp.status_code == 413

    async def test_get_not_allowed(self, tmp_path):
        async with _client(tmp_path) as (client, _):
            assert (await client.get("/webhooks/intervals")).status_code == 405

    def test_is_authorized_constant_time_helper(self):
        assert is_authorized(SECRET, None, SECRET)
        assert is_authorized("", SECRET, SECRET)
        assert not is_authorized("", None, SECRET)
        assert not is_authorized("Bearer ", "", SECRET)


class TestEventsAndLog:
    async def test_required_types_logged_with_ids(self, tmp_path):
        delivery = _delivery(
            _event(
                "ACTIVITY_UPLOADED", activity={"id": "i555", "name": "Morning ride", "watts": [1]}
            ),
            _event("WELLNESS_UPDATED", records=[{"id": "2026-11-04", "hrv": 70}]),
            _event("CALENDAR_UPDATED", records=[{"id": 91}, {"id": 92}], deleted_ids=[90]),
            _event("SPORT_SETTINGS_UPDATED", records=[{"id": 3}]),
        )
        async with _client(tmp_path) as (client, log):
            resp = await client.post(
                "/webhooks/intervals", json=delivery, headers={"Authorization": SECRET}
            )
        assert resp.json()["stored"] == 4
        rows = _read_log(log)
        assert [r["type"] for r in rows] == [
            "ACTIVITY_UPLOADED",
            "WELLNESS_UPDATED",
            "CALENDAR_UPDATED",
            "SPORT_SETTINGS_UPDATED",
        ]
        assert all(r["known_type"] for r in rows)
        assert all(r["athlete_id"] == "i987654" for r in rows)
        assert rows[0]["timestamp"] == "2026-11-04T06:12:00.000+00:00"
        assert rows[0]["received_at"].endswith("Z")
        assert rows[0]["ids"] == {"activity": ["i555"]}
        assert rows[2]["ids"] == {"records": ["91", "92"], "deleted_ids": ["90"]}
        # Only summaries are stored, never payload content.
        text = log.read_text()
        assert "Morning ride" not in text
        assert "hrv" not in text
        assert stat.S_IMODE(log.stat().st_mode) == 0o600

    async def test_unknown_type_logged_not_failed(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="intervals_icu_mcp.webhooks")
        async with _client(tmp_path) as (client, log):
            resp = await client.post(
                "/webhooks/intervals",
                json=_delivery(_event("SOMETHING_NEW", widget={"id": 7})),
                headers={"Authorization": SECRET},
            )
        assert resp.status_code == 200
        row = _read_log(log)[0]
        assert row["type"] == "SOMETHING_NEW"
        assert row["known_type"] is False
        assert "Unknown webhook type SOMETHING_NEW" in caplog.text

    async def test_single_event_body_and_empty_delivery(self, tmp_path):
        async with _client(tmp_path) as (client, log):
            one = await client.post(
                "/webhooks/intervals",
                json=_event("CALENDAR_UPDATED"),
                headers={"Authorization": SECRET},
            )
            empty = await client.post(
                "/webhooks/intervals", json={"events": []}, headers={"Authorization": SECRET}
            )
        assert one.json()["stored"] == 1
        assert empty.json()["stored"] == 0

    async def test_allowlist_drops_other_athletes(self, tmp_path):
        async with _client(tmp_path, allowed_athlete_ids="i1") as (client, log):
            resp = await client.post(
                "/webhooks/intervals",
                json=_delivery(
                    _event("ACTIVITY_UPLOADED"), {**_event("WELLNESS_UPDATED"), "athlete_id": "1"}
                ),
                headers={"Authorization": SECRET},
            )
        assert resp.json()["stored"] == 1
        assert _read_log(log)[0]["athlete_id"] == "i1"

    async def test_log_rotates_with_size_cap(self, tmp_path):
        log = EventLog(tmp_path / EVENT_LOG_NAME, max_bytes=4096)
        for i in range(60):
            await log.append(
                [{"received_at": "2026-11-04T00:00:00Z", "type": "X", "n": i, "pad": "p" * 100}]
            )
        assert log.backup_path.exists()
        assert log.path.stat().st_size < 4096 + 200
        numbers = [r["n"] for r in log.read()]
        assert numbers[-1] == 59
        assert numbers == sorted(numbers)
        assert len(numbers) < 60  # the oldest rotated out

    def test_extract_ids_shapes(self):
        assert extract_ids({"activity_id": "i9", "event_ids": [1, 2], "type": "X"}) == {
            "activity_id": ["i9"],
            "event_ids": ["1", "2"],
        }
        assert extract_ids({"names": ["a"], "flag": True}) == {}

    def test_summarize_tolerates_garbage(self):
        now = datetime.now(UTC)
        assert summarize_delivery("text", now) == []
        assert summarize_delivery({"events": ["x", 1]}, now) == []


class TestHomeAssistantHook:
    async def test_forwards_summaries_when_configured(self, tmp_path):
        with respx.mock(assert_all_called=False) as router:
            ha = router.post(HA_URL).mock(return_value=httpx.Response(200))
            async with _client(tmp_path, ha_webhook_url=HA_URL) as (client, _):
                await client.post(
                    "/webhooks/intervals",
                    json=_delivery(_event("ACTIVITY_UPLOADED", activity={"id": "i1"})),
                    headers={"Authorization": SECRET},
                )
                await asyncio.gather(*client.receiver._background)  # type: ignore[attr-defined]
        assert ha.called
        sent = json.loads(ha.calls.last.request.content)
        assert sent["source"] == "intervals.icu"
        assert sent["events"][0]["type"] == "ACTIVITY_UPLOADED"
        assert sent["events"][0]["ids"] == {"activity": ["i1"]}

    async def test_off_by_default(self, tmp_path):
        with respx.mock(assert_all_called=False) as router:
            ha = router.post(HA_URL).mock(return_value=httpx.Response(200))
            async with _client(tmp_path) as (client, _):
                await client.post(
                    "/webhooks/intervals",
                    json=_delivery(_event("ACTIVITY_UPLOADED")),
                    headers={"Authorization": SECRET},
                )
                assert not client.receiver._background  # type: ignore[attr-defined]
        assert not ha.called

    async def test_ha_failure_does_not_fail_delivery_or_leak_url(self, tmp_path, caplog):
        with respx.mock(assert_all_called=False) as router:
            router.post(HA_URL).mock(side_effect=httpx.ConnectError("down"))
            async with _client(tmp_path, ha_webhook_url=HA_URL) as (client, log):
                resp = await client.post(
                    "/webhooks/intervals",
                    json=_delivery(_event("ACTIVITY_UPLOADED")),
                    headers={"Authorization": SECRET},
                )
                await asyncio.gather(*client.receiver._background)  # type: ignore[attr-defined]
        assert resp.status_code == 200
        assert len(_read_log(log)) == 1
        assert "ha-hook-id-placeholder" not in caplog.text

    def test_ha_url_requires_webhook_secret(self):
        with pytest.raises(RemoteConfigError):
            RemoteSettings(ha_webhook_url=HA_URL).validate_remote()

    def test_short_webhook_secret_rejected(self):
        with pytest.raises(RemoteConfigError):
            RemoteSettings(intervals_webhook_secret="short").validate_remote()

    def test_ha_url_is_redacted_from_logs(self):
        settings = RemoteSettings(intervals_webhook_secret=SECRET, ha_webhook_url=HA_URL)
        assert HA_URL in settings.secrets_to_redact()


class TestRecentEventsTool:
    @pytest.fixture
    def populated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        now = datetime.now(UTC)

        def row(hours_ago: float, event_type: str, athlete: str) -> dict:
            received = (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
            return {
                "received_at": received.replace("+00:00", "Z"),
                "timestamp": None,
                "type": event_type,
                "known_type": True,
                "athlete_id": athlete,
                "ids": {},
            }

        rows = [
            row(50, "ACTIVITY_UPLOADED", "i1"),
            row(5, "WELLNESS_UPDATED", "i1"),
            row(2, "ACTIVITY_UPLOADED", "i2"),
            row(1, "CALENDAR_UPDATED", "i1"),
        ]
        (tmp_path / EVENT_LOG_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows))
        return now

    @staticmethod
    def _ctx(config: ICUConfig) -> MagicMock:
        ctx = MagicMock()
        ctx.get_state = AsyncMock(return_value=config)
        return ctx

    async def test_default_window_is_24h_newest_first(self, populated, mock_config):
        result = json.loads(await get_recent_events(ctx=self._ctx(mock_config)))
        types = [e["type"] for e in result["data"]["events"]]
        assert types == ["CALENDAR_UPDATED", "ACTIVITY_UPLOADED", "WELLNESS_UPDATED"]

    async def test_since_and_type_filters(self, populated, mock_config):
        since = (populated - timedelta(days=3)).date().isoformat()
        result = json.loads(
            await get_recent_events(
                since=since, event_types=["activity_uploaded"], ctx=self._ctx(mock_config)
            )
        )
        assert result["data"]["count"] == 2

    async def test_bearer_mode_sees_only_own_athlete(self, populated, mock_config):
        bearer = mock_config.with_bearer_token("t", athlete_id="2")
        result = json.loads(
            await get_recent_events(since="2000-01-01", athlete_id="i1", ctx=self._ctx(bearer))
        )
        assert {e["athlete_id"] for e in result["data"]["events"]} == {"i2"}

    async def test_invalid_since(self, populated, mock_config):
        result = json.loads(await get_recent_events(since="yesterday", ctx=self._ctx(mock_config)))
        assert result["error"]["type"] == "validation_error"

    async def test_empty_log(self, tmp_path, monkeypatch, mock_config):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "empty"))
        result = json.loads(await get_recent_events(ctx=self._ctx(mock_config)))
        assert result["data"]["count"] == 0
        assert "message" in result["metadata"]


class TestWiring:
    async def test_webhook_route_behind_secret_path_gate(self, tmp_path):
        path_secret = secrets.token_urlsafe(32)
        settings = RemoteSettings(
            mcp_path_secret=path_secret, intervals_webhook_secret=SECRET, data_dir=str(tmp_path)
        )
        remote = build_remote_app(FastMCP("wiring"), settings)
        async with LifespanManager(remote.app) as manager:  # type: ignore[arg-type]
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                ok = await client.post(
                    "/webhooks/intervals",
                    json=_delivery(_event("CALENDAR_UPDATED")),
                    headers={"Authorization": SECRET},
                )
        assert ok.status_code == 200

    async def test_webhook_route_absent_when_disabled(self, tmp_path):
        settings = RemoteSettings(mcp_path_secret=secrets.token_urlsafe(32))
        remote = build_remote_app(FastMCP("wiring2"), settings)
        async with LifespanManager(remote.app) as manager:  # type: ignore[arg-type]
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                resp = await client.post("/webhooks/intervals", json={})
        assert resp.status_code == 404

    @pytest.mark.parametrize(("secret", "expected"), [("", 70), (SECRET, 71)])
    def test_tool_registered_only_with_webhook_secret(self, secret, expected):
        code = (
            "import asyncio, dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
            "from intervals_icu_mcp.auth import ICUConfig\n"
            "from intervals_icu_mcp.remote import RemoteSettings\n"
            "ICUConfig.model_config['env_file'] = None\n"
            "RemoteSettings.model_config['env_file'] = None\n"
            "from intervals_icu_mcp.server import mcp\n"
            "names = {t.name for t in asyncio.run(mcp.list_tools())}\n"
            "print(len(names), 'icu_get_recent_events' in names)\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("INTERVALS_")}
        if secret:
            env["INTERVALS_WEBHOOK_SECRET"] = secret
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        ).stdout.split()
        assert out == [str(expected), str(bool(secret))]
