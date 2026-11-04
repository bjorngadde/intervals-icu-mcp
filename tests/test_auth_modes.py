"""Per-request client: API-key mode (unchanged) and OAuth bearer mode."""

import base64
import json

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.server.auth import AccessToken
from httpx import Response
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from intervals_icu_mcp.auth import ICUConfig, normalize_athlete_id, validate_credentials
from intervals_icu_mcp.client import ICUAPIError, ICUClient
from intervals_icu_mcp.middleware import (
    INTERVALS_ATHLETE_CLAIM,
    ConfigMiddleware,
    resolve_request_config,
)

UPSTREAM_TOKEN = "upstream-token-placeholder"


class TestNormalizeAthleteId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("i987654", "i987654"),
            ("987654", "i987654"),
            (987654, "i987654"),
            (" I987654 ", "i987654"),
            ("", ""),
            (None, ""),
            ("abc", "abc"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_athlete_id(raw) == expected


class TestConfigModes:
    def test_api_key_mode_is_default(self, mock_config):
        assert mock_config.auth_mode == "api_key"
        assert mock_config.bearer_token is None
        assert mock_config.display_athlete_id == "i123456"

    def test_with_bearer_token_drops_api_key_and_targets_athlete_zero(self, mock_config):
        bearer = mock_config.with_bearer_token(UPSTREAM_TOKEN, athlete_id="987654")
        assert bearer.auth_mode == "bearer"
        assert bearer.bearer_token == UPSTREAM_TOKEN
        assert bearer.intervals_icu_api_key == ""
        assert bearer.intervals_icu_athlete_id == "0"
        assert bearer.display_athlete_id == "i987654"
        assert validate_credentials(bearer)
        # The source config is untouched (no shared/global state).
        assert mock_config.auth_mode == "api_key"
        assert mock_config.intervals_icu_api_key == "test_api_key_12345"

    def test_delete_mode_survives_bearer_copy(self):
        config = ICUConfig(
            intervals_icu_api_key="k",
            intervals_icu_athlete_id="i1",
            intervals_icu_delete_mode="full",
        )
        assert config.with_bearer_token("t").intervals_icu_delete_mode == "full"

    def test_bearer_token_cannot_come_from_environment(self, monkeypatch):
        monkeypatch.setenv("_BEARER_TOKEN", "from-env")
        monkeypatch.setenv("BEARER_TOKEN", "from-env")
        config = ICUConfig(intervals_icu_api_key="k", intervals_icu_athlete_id="i1")
        assert config.bearer_token is None

    def test_secrets_are_not_in_repr(self, mock_config):
        bearer = mock_config.with_bearer_token(UPSTREAM_TOKEN)
        assert "test_api_key_12345" not in repr(mock_config)
        assert UPSTREAM_TOKEN not in repr(bearer)

    def test_empty_token_rejected(self, mock_config):
        with pytest.raises(ValueError):
            mock_config.with_bearer_token("")


class TestClientModes:
    async def test_api_key_mode_uses_basic_auth_and_configured_athlete(
        self, mock_config, respx_mock, mock_athlete_data
    ):
        route = respx_mock.get("/athlete/i123456").mock(
            return_value=Response(200, json=mock_athlete_data)
        )
        async with ICUClient(mock_config) as client:
            await client.get_athlete()
        header = route.calls.last.request.headers["Authorization"]
        expected = base64.b64encode(b"API_KEY:test_api_key_12345").decode()
        assert header == f"Basic {expected}"

    async def test_bearer_mode_uses_bearer_header_and_athlete_zero(
        self, mock_config, respx_mock, mock_athlete_data
    ):
        route = respx_mock.get("/athlete/0").mock(
            return_value=Response(200, json=mock_athlete_data)
        )
        bearer = mock_config.with_bearer_token(UPSTREAM_TOKEN, athlete_id="i123456")
        async with ICUClient(bearer) as client:
            await client.get_athlete()
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {UPSTREAM_TOKEN}"

    async def test_bearer_mode_explicit_athlete_id_still_honoured(self, mock_config, respx_mock):
        route = respx_mock.get("/athlete/i999/events").mock(return_value=Response(200, json=[]))
        bearer = mock_config.with_bearer_token(UPSTREAM_TOKEN)
        async with ICUClient(bearer) as client:
            await client.get_events(athlete_id="i999")
        assert route.called

    async def test_bearer_mode_401_message_mentions_reconnect(self, mock_config, respx_mock):
        respx_mock.get("/athlete/0").mock(return_value=Response(401))
        bearer = mock_config.with_bearer_token(UPSTREAM_TOKEN)
        async with ICUClient(bearer) as client:
            with pytest.raises(ICUAPIError) as exc:
                await client.get_athlete()
        assert exc.value.status_code == 401
        assert "OAuth" in exc.value.message
        assert UPSTREAM_TOKEN not in exc.value.message

    async def test_bearer_mode_403_message_mentions_scope(self, mock_config, respx_mock):
        respx_mock.get("/athlete/0").mock(return_value=Response(403, text="nope"))
        bearer = mock_config.with_bearer_token(UPSTREAM_TOKEN)
        async with ICUClient(bearer) as client:
            with pytest.raises(ICUAPIError) as exc:
                await client.get_athlete()
        assert exc.value.status_code == 403
        assert "scope" in exc.value.message


def _intervals_token(athlete_id: str = "i987654") -> AccessToken:
    return AccessToken(
        token=UPSTREAM_TOKEN,
        client_id="dcr-client",
        scopes=["ACTIVITY:READ"],
        claims={INTERVALS_ATHLETE_CLAIM: athlete_id},
    )


class TestResolveRequestConfig:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "operator_api_key")
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i111")

    def test_no_token_means_api_key_mode(self):
        config = resolve_request_config()
        assert config.auth_mode == "api_key"
        assert config.intervals_icu_api_key == "operator_api_key"

    def test_intervals_token_means_bearer_mode(self):
        reset = auth_context_var.set(AuthenticatedUser(_intervals_token()))
        try:
            config = resolve_request_config()
        finally:
            auth_context_var.reset(reset)
        assert config.auth_mode == "bearer"
        assert config.bearer_token == UPSTREAM_TOKEN
        assert config.intervals_icu_api_key == ""
        assert config.intervals_icu_athlete_id == "0"
        assert config.display_athlete_id == "i987654"

    def test_foreign_token_without_claim_stays_api_key(self):
        token = AccessToken(token="other", client_id="x", scopes=[], claims={})
        reset = auth_context_var.set(AuthenticatedUser(token))
        try:
            config = resolve_request_config()
        finally:
            auth_context_var.reset(reset)
        assert config.auth_mode == "api_key"

    def test_bearer_mode_works_without_api_key(self, monkeypatch):
        monkeypatch.delenv("INTERVALS_ICU_API_KEY")
        monkeypatch.delenv("INTERVALS_ICU_ATHLETE_ID")
        reset = auth_context_var.set(AuthenticatedUser(_intervals_token()))
        try:
            config = resolve_request_config()
        finally:
            auth_context_var.reset(reset)
        assert config.auth_mode == "bearer"


class TestMiddlewarePerRequest:
    """Two calls in a row with different identities must not share state."""

    async def test_middleware_switches_per_call(self, monkeypatch):
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "operator_api_key")
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i111")
        server = FastMCP("auth-modes")
        server.add_middleware(ConfigMiddleware())

        @server.tool()
        async def whoami(ctx: Context) -> str:
            config: ICUConfig = await ctx.get_state("config")
            return json.dumps(
                {
                    "mode": config.auth_mode,
                    "athlete": config.intervals_icu_athlete_id,
                    "has_api_key": bool(config.intervals_icu_api_key),
                }
            )

        async with Client(server) as client:
            first = json.loads((await client.call_tool("whoami", {})).data)
            reset = auth_context_var.set(AuthenticatedUser(_intervals_token()))
            try:
                second = json.loads((await client.call_tool("whoami", {})).data)
            finally:
                auth_context_var.reset(reset)
            third = json.loads((await client.call_tool("whoami", {})).data)

        assert first == {"mode": "api_key", "athlete": "i111", "has_api_key": True}
        assert second == {"mode": "bearer", "athlete": "0", "has_api_key": False}
        assert third == first
