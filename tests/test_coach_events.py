"""Coach-facing calendar features: push_errors, external_id filter, upsert."""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

from httpx import Response

from intervals_icu_mcp.tools.event_management import bulk_create_events, create_event
from intervals_icu_mcp.tools.events import (
    get_calendar_events,
    get_event,
    get_upcoming_workouts,
)

TOMORROW = (date.today() + timedelta(days=1)).isoformat()
PUSH_ERROR = {
    "service": "GARMIN_CONNECT",
    "message": "Workout has too many steps",
    "date": "2026-11-04T06:00:00Z",
}


def _ctx(config):
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value=config)
    return ctx


def _workout(event_id: int, external_id: str | None = None, **extra) -> dict:
    data = {
        "id": event_id,
        "start_date_local": f"{TOMORROW}T00:00:00",
        "name": f"Workout {event_id}",
        "category": "WORKOUT",
        "type": "Ride",
    }
    if external_id:
        data["external_id"] = external_id
    data.update(extra)
    return data


class TestPushErrors:
    async def test_calendar_lists_push_errors_and_summary(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/events").mock(
            return_value=Response(
                200,
                json=[
                    _workout(1, "ai-coach-1", push_errors=[PUSH_ERROR], indoor=True),
                    _workout(2, push_errors=[]),
                ],
            )
        )
        result = json.loads(await get_calendar_events(ctx=_ctx(mock_config)))
        items = result["data"]["events_by_date"][f"{TOMORROW}T00:00:00"]
        first, second = items
        assert first["push_errors"] == [PUSH_ERROR]
        assert first["external_id"] == "ai-coach-1"
        assert first["indoor"] is True
        assert "push_errors" not in second
        assert result["data"]["summary"]["events_with_push_errors"] == 1
        assert "Garmin" in result["analysis"]["push_errors"]

    async def test_no_push_errors_means_no_noise(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/events").mock(
            return_value=Response(200, json=[_workout(1)])
        )
        result = json.loads(await get_calendar_events(ctx=_ctx(mock_config)))
        assert "events_with_push_errors" not in result["data"]["summary"]
        assert "analysis" not in result or "push_errors" not in (result.get("analysis") or {})

    async def test_get_event_shows_push_errors(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/events/1").mock(
            return_value=Response(200, json=_workout(1, push_errors=[PUSH_ERROR]))
        )
        result = json.loads(await get_event(event_id=1, ctx=_ctx(mock_config)))
        assert result["data"]["push_errors"] == [PUSH_ERROR]

    async def test_upcoming_workouts_show_push_errors(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/events").mock(
            return_value=Response(200, json=[_workout(1, "ai-coach-x", push_errors=[PUSH_ERROR])])
        )
        result = json.loads(await get_upcoming_workouts(ctx=_ctx(mock_config)))
        workout = result["data"]["workouts"][0]
        assert workout["push_errors"] == [PUSH_ERROR]
        assert workout["external_id"] == "ai-coach-x"

    async def test_create_event_echoes_push_errors(self, mock_config, respx_mock):
        respx_mock.post("/athlete/i123456/events").mock(
            return_value=Response(200, json=_workout(5, push_errors=[PUSH_ERROR]))
        )
        result = json.loads(
            await create_event(
                start_date=TOMORROW, name="Workout 5", category="WORKOUT", ctx=_ctx(mock_config)
            )
        )
        assert result["data"]["push_errors"] == [PUSH_ERROR]


class TestExternalIdFilter:
    async def test_prefix_filter(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/events").mock(
            return_value=Response(
                200,
                json=[_workout(1, "ai-coach-a"), _workout(2, "other-b"), _workout(3)],
            )
        )
        result = json.loads(
            await get_calendar_events(external_id_prefix="ai-coach-", ctx=_ctx(mock_config))
        )
        items = [e for day in result["data"]["events_by_date"].values() for e in day]
        assert [e["id"] for e in items] == [1]
        assert result["data"]["summary"]["total_events"] == 1

    async def test_prefix_filter_no_match(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/events").mock(
            return_value=Response(200, json=[_workout(2, "other-b")])
        )
        result = json.loads(
            await get_calendar_events(external_id_prefix="ai-coach-", ctx=_ctx(mock_config))
        )
        assert result["data"]["count"] == 0


class TestUpsert:
    PAYLOAD = json.dumps(
        [
            {
                "start_date_local": TOMORROW,
                "name": "Z2",
                "category": "WORKOUT",
                "external_id": "ai-coach-1",
            }
        ]
    )

    async def test_upsert_sends_query_flag_in_oauth_mode(self, mock_config, respx_mock):
        route = respx_mock.post("/athlete/0/events/bulk").mock(
            return_value=Response(200, json=[_workout(9, "ai-coach-1")])
        )
        bearer = mock_config.with_bearer_token("upstream-token", athlete_id="i123456")
        result = json.loads(
            await bulk_create_events(
                events=self.PAYLOAD, upsert_by_external_id=True, ctx=_ctx(bearer)
            )
        )
        assert route.calls.last.request.url.params["upsert"] == "true"
        assert "updated" in result["metadata"]["message"]

    async def test_no_upsert_flag_by_default(self, mock_config, respx_mock):
        route = respx_mock.post("/athlete/i123456/events/bulk").mock(
            return_value=Response(200, json=[_workout(9, "ai-coach-1")])
        )
        await bulk_create_events(events=self.PAYLOAD, ctx=_ctx(mock_config))
        assert "upsert" not in route.calls.last.request.url.params

    async def test_upsert_refused_in_api_key_mode(self, mock_config, respx_mock):
        route = respx_mock.post("/athlete/i123456/events/bulk")
        result = json.loads(
            await bulk_create_events(
                events=self.PAYLOAD, upsert_by_external_id=True, ctx=_ctx(mock_config)
            )
        )
        assert result["error"]["type"] == "validation_error"
        assert "OAuth" in result["error"]["message"]
        assert not route.called

    async def test_upsert_requires_external_id_everywhere(self, mock_config, respx_mock):
        bearer = mock_config.with_bearer_token("upstream-token")
        payload = json.dumps(
            [
                {"start_date_local": TOMORROW, "name": "a", "category": "NOTE", "external_id": "x"},
                {"start_date_local": TOMORROW, "name": "b", "category": "NOTE"},
            ]
        )
        result = json.loads(
            await bulk_create_events(events=payload, upsert_by_external_id=True, ctx=_ctx(bearer))
        )
        assert result["error"]["type"] == "validation_error"
        assert "[1]" in result["error"]["message"]
