"""Garmin Connect (and generic) restricted-activity detection."""

import json
from unittest.mock import AsyncMock, MagicMock

from httpx import Response

from intervals_icu_mcp.models import Activity, ActivitySummary
from intervals_icu_mcp.tools._strava import (
    GARMIN_LIMITATION_NOTE,
    STRAVA_LIMITATION_NOTE,
    data_availability_note,
    garmin_limitation_note,
    summarize_restricted,
)
from intervals_icu_mcp.tools.activities import (
    get_activities_by_date,
    get_activity_details,
    get_recent_activities,
)
from intervals_icu_mcp.tools.activity_analysis import get_activity_streams

GARMIN_STUB = {
    "id": "i100",
    "source": "GARMIN_CONNECT",
    "start_date_local": "2026-11-03T07:00:00",
    "type": "Ride",
    "name": "Morning ride",
}
GARMIN_FULL = {
    **GARMIN_STUB,
    "id": "i101",
    "distance": 40000.0,
    "moving_time": 5400,
    "icu_average_watts": 180,
}
STRAVA_STUB = {
    "id": "i102",
    "source": "STRAVA",
    "_note": "STRAVA activities are not available via the API",
    "start_date_local": "2026-11-02T07:00:00",
}
UPLOAD_NOTE_STUB = {
    "id": "i103",
    "source": "UPLOAD",
    "_note": "Not available via the API",
    "start_date_local": "2026-11-01T07:00:00",
}
MANUAL_EMPTY = {"id": "i104", "source": "MANUAL", "start_date_local": "2026-11-01T08:00:00"}


def _ctx(config):
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value=config)
    return ctx


class TestHelpers:
    def test_garmin_stub_detected(self):
        assert (
            garmin_limitation_note(ActivitySummary.model_validate(GARMIN_STUB))
            == GARMIN_LIMITATION_NOTE
        )

    def test_garmin_with_metrics_not_flagged(self):
        assert garmin_limitation_note(ActivitySummary.model_validate(GARMIN_FULL)) is None
        assert data_availability_note(ActivitySummary.model_validate(GARMIN_FULL)) is None

    def test_garmin_note_text_is_quoted(self):
        stub = ActivitySummary.model_validate({**GARMIN_STUB, "_note": "Garmin data not available"})
        note = data_availability_note(stub)
        assert note and note.endswith("Intervals.icu says: Garmin data not available")

    def test_strava_still_uses_strava_note(self):
        assert (
            data_availability_note(ActivitySummary.model_validate(STRAVA_STUB))
            == STRAVA_LIMITATION_NOTE
        )

    def test_any_source_with_api_note_and_no_metrics(self):
        note = data_availability_note(ActivitySummary.model_validate(UPLOAD_NOTE_STUB))
        assert note and "source: UPLOAD" in note and "Not available via the API" in note

    def test_plain_manual_entry_not_flagged(self):
        assert data_availability_note(ActivitySummary.model_validate(MANUAL_EMPTY)) is None

    def test_summary_counts_per_source(self):
        acts = [ActivitySummary.model_validate(a) for a in (GARMIN_STUB, GARMIN_FULL, STRAVA_STUB)]
        summary = summarize_restricted(acts)
        assert summary and summary.startswith("2 of 3 activities")
        assert "1 from GARMIN_CONNECT" in summary and "1 from STRAVA" in summary
        assert summarize_restricted([ActivitySummary.model_validate(GARMIN_FULL)]) is None


class TestTools:
    async def test_recent_activities_flags_and_warns(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/activities").mock(
            return_value=Response(200, json=[GARMIN_STUB, GARMIN_FULL])
        )
        result = json.loads(await get_recent_activities(ctx=_ctx(mock_config)))
        items = {a["id"]: a for a in result["data"]["activities"]}
        assert items["i100"]["data_restricted"] == "GARMIN_CONNECT"
        assert "data_restricted" not in items["i101"]
        assert "Garmin" in result["analysis"]["data_availability"]

    async def test_by_date_without_stubs_has_no_warning(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/activities").mock(
            return_value=Response(200, json=[GARMIN_FULL])
        )
        result = json.loads(
            await get_activities_by_date(oldest="2026-11-01", ctx=_ctx(mock_config))
        )
        assert "analysis" not in result or not result["analysis"]

    async def test_activity_details_garmin_stub(self, mock_config, respx_mock):
        respx_mock.get("/activity/i100").mock(return_value=Response(200, json=GARMIN_STUB))
        result = json.loads(await get_activity_details(activity_id="i100", ctx=_ctx(mock_config)))
        assert result["analysis"]["data_availability"] == GARMIN_LIMITATION_NOTE

    async def test_streams_empty_garmin_stub_explained(self, mock_config, respx_mock):
        respx_mock.get("/activity/i100/streams.json").mock(return_value=Response(200, json=[]))
        respx_mock.get("/activity/i100").mock(return_value=Response(200, json=GARMIN_STUB))
        result = json.loads(await get_activity_streams(activity_id="i100", ctx=_ctx(mock_config)))
        assert result["analysis"]["data_availability"] == GARMIN_LIMITATION_NOTE

    def test_activity_model_accepts_stub(self):
        assert Activity.model_validate(GARMIN_STUB).source == "GARMIN_CONNECT"
