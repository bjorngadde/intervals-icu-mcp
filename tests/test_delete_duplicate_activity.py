"""Tests for icu_delete_duplicate_activity."""

import json
from unittest.mock import AsyncMock, MagicMock

from httpx import Response

from intervals_icu_mcp.tools.activities import delete_duplicate_activity

# The real case that prompted the tool: the same trainer session from Auuki
# (complete) and from a watch started ~10 minutes late.
AUUKI = {
    "id": "i206530729",
    "start_date_local": "2026-11-06T11:50:42",
    "name": "Tempo 2x8",
    "type": "VirtualRide",
    "elapsed_time": 2407,
    "moving_time": 2407,
    "device_name": "GARMIN EDGE_1030_PLUS",
}
EPIX = {
    "id": "i206531114",
    "start_date_local": "2026-11-06T12:01:11",
    "name": "Indoor Cycling",
    "type": "VirtualRide",
    "elapsed_time": 1852,
    "moving_time": 1852,
    "device_name": "Garmin epix (Gen 2)",
}


def _ctx(mock_config):
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value=mock_config)
    return ctx


class TestDeleteDuplicateActivity:
    async def test_deletes_overlapping_copy(self, mock_config, respx_mock):
        respx_mock.get("/activity/i206531114").mock(return_value=Response(200, json=EPIX))
        respx_mock.get("/activity/i206530729").mock(return_value=Response(200, json=AUUKI))
        deleted = respx_mock.delete("/activity/i206531114").mock(return_value=Response(200))

        result = json.loads(
            await delete_duplicate_activity(
                activity_id="i206531114", keep_activity_id="i206530729", ctx=_ctx(mock_config)
            )
        )

        assert deleted.called
        assert result["data"]["deleted"]["id"] == "i206531114"
        assert result["data"]["kept"]["id"] == "i206530729"
        # 12:01:11 to 12:30:49 = 1778 s of the 1852 s watch recording.
        assert result["data"]["overlap_seconds"] == 1778
        assert result["data"]["overlap_share_of_shorter"] > 0.95

    async def test_ride_and_virtual_ride_count_as_same_sport(self, mock_config, respx_mock):
        outdoor = {**EPIX, "type": "Ride"}
        respx_mock.get("/activity/i206531114").mock(return_value=Response(200, json=outdoor))
        respx_mock.get("/activity/i206530729").mock(return_value=Response(200, json=AUUKI))
        deleted = respx_mock.delete("/activity/i206531114").mock(return_value=Response(200))

        await delete_duplicate_activity(
            activity_id="i206531114", keep_activity_id="i206530729", ctx=_ctx(mock_config)
        )
        assert deleted.called

    async def test_refuses_different_sport(self, mock_config, respx_mock):
        swim = {**EPIX, "type": "Swim"}
        respx_mock.get("/activity/i206531114").mock(return_value=Response(200, json=swim))
        respx_mock.get("/activity/i206530729").mock(return_value=Response(200, json=AUUKI))
        deleted = respx_mock.delete("/activity/i206531114").mock(return_value=Response(200))

        result = json.loads(
            await delete_duplicate_activity(
                activity_id="i206531114", keep_activity_id="i206530729", ctx=_ctx(mock_config)
            )
        )
        assert not deleted.called
        assert "error" in result

    async def test_refuses_without_enough_overlap(self, mock_config, respx_mock):
        later = {**EPIX, "start_date_local": "2026-11-06T12:25:00"}
        respx_mock.get("/activity/i206531114").mock(return_value=Response(200, json=later))
        respx_mock.get("/activity/i206530729").mock(return_value=Response(200, json=AUUKI))
        deleted = respx_mock.delete("/activity/i206531114").mock(return_value=Response(200))

        result = json.loads(
            await delete_duplicate_activity(
                activity_id="i206531114", keep_activity_id="i206530729", ctx=_ctx(mock_config)
            )
        )
        assert not deleted.called
        assert "error" in result

    async def test_refuses_same_id(self, mock_config, respx_mock):
        deleted = respx_mock.delete("/activity/i206530729").mock(return_value=Response(200))
        result = json.loads(
            await delete_duplicate_activity(
                activity_id="i206530729", keep_activity_id="i206530729", ctx=_ctx(mock_config)
            )
        )
        assert not deleted.called
        assert "error" in result
