"""Webhook event log tool (registered only when the webhook receiver is configured)."""

from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig, normalize_athlete_id
from ..remote import load_remote_settings
from ..response_builder import ResponseBuilder
from ..webhooks import KNOWN_EVENT_TYPES, event_log_for, parse_since, query_events


async def get_recent_events(
    since: Annotated[
        str | None,
        "Only events received at/after this time: YYYY-MM-DD or ISO datetime (UTC if no "
        "offset). Default: last 24 hours",
    ] = None,
    event_types: Annotated[
        list[str] | None,
        "Filter by webhook type, e.g. ['ACTIVITY_UPLOADED', 'WELLNESS_UPDATED', "
        "'CALENDAR_UPDATED', 'SPORT_SETTINGS_UPDATED']",
    ] = None,
    limit: Annotated[int, "Max events to return, newest first (1-500)"] = 50,
    athlete_id: Annotated[
        str | None, "Only this athlete's events (ignored with OAuth: always the caller)"
    ] = None,
    ctx: Context | None = None,
) -> str:
    """List Intervals.icu webhook notifications this server received — what changed and when.

    Use to decide what to re-read without polling: new uploads (ACTIVITY_UPLOADED /
    ACTIVITY_ANALYZED), wellness syncs (WELLNESS_UPDATED), calendar edits
    (CALENDAR_UPDATED), threshold changes (SPORT_SETTINGS_UPDATED). Returns type,
    athlete, timestamps and IDs only; fetch details with the matching icu_get_* tool.
    """
    assert ctx is not None
    config: ICUConfig = await ctx.get_state("config")

    try:
        since_dt = parse_since(since)
    except ValueError:
        return ResponseBuilder.build_error_response(
            "Invalid since. Use YYYY-MM-DD or an ISO datetime like 2026-11-04T06:00:00Z.",
            error_type="validation_error",
        )
    limit = max(1, min(limit, 500))
    types = {t.strip().upper() for t in event_types} if event_types else None

    # OAuth callers only ever see their own athlete's events.
    if config.auth_mode == "bearer":
        athlete_filter: str | None = config.display_athlete_id or "__none__"
    else:
        athlete_filter = normalize_athlete_id(athlete_id) if athlete_id else None

    log = event_log_for(load_remote_settings())
    try:
        events = query_events(
            log, since=since_dt, event_types=types, athlete_id=athlete_filter, limit=limit
        )
    except OSError as e:
        return ResponseBuilder.build_error_response(
            f"Could not read the webhook event log ({type(e).__name__})",
            error_type="internal_error",
        )

    data: dict[str, Any] = {
        "events": events,
        "count": len(events),
        "since": since_dt.isoformat(timespec="seconds"),
    }
    metadata: dict[str, Any] = {}
    if not events:
        metadata["message"] = (
            "No webhook events in this window. Events only arrive for athletes who "
            "authorised the OAuth app, for the types ticked on its settings page."
        )
    if types and not types <= KNOWN_EVENT_TYPES:
        metadata["unknown_types_requested"] = sorted(types - KNOWN_EVENT_TYPES)
    return ResponseBuilder.build_response(
        data=data, metadata=metadata or None, query_type="recent_events"
    )
