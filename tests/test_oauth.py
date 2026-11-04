"""OAuth mode: MCP-client OAuth 2.1 proxied to a mocked Intervals.icu."""

import base64
import hashlib
import json
import logging
import re
import secrets
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastmcp import Context, FastMCP

from intervals_icu_mcp.auth import ICUConfig
from intervals_icu_mcp.client import ICUClient
from intervals_icu_mcp.middleware import ConfigMiddleware
from intervals_icu_mcp.oauth import (
    COACH_TOOLS,
    DEFAULT_OAUTH_SCOPES,
    INTERVALS_DISCONNECT_URL,
    INTERVALS_TOKEN_URL,
    SCOPE_BY_TOOL,
    format_upstream_scopes,
    parse_allowlist,
    parse_scope_list,
    scopes_for_tools,
)
from intervals_icu_mcp.remote import RemoteConfigError, RemoteSettings, build_remote_app

BASE = "https://intervals.gadde.nu"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLIENT_SECRET = "upstream-client-secret-placeholder"
UPSTREAM_TOKEN = "upstream-access-token-placeholder"
ATHLETE = "i784427"
SCOPE_STRING = "ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:READ"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _settings(tmp_path: Path, **overrides: str) -> RemoteSettings:
    values = {
        "intervals_oauth_client_id": "intervals-client-id",
        "intervals_oauth_client_secret": CLIENT_SECRET,
        "public_base_url": BASE,
        "allowed_athlete_ids": ATHLETE,
        "data_dir": str(tmp_path / "data"),
    }
    values.update(overrides)
    return RemoteSettings(**values)


def _make_server() -> FastMCP:
    """A fresh server per test (build_remote_app sets server.auth)."""
    server = FastMCP("intervals_icu_mcp")
    server.add_middleware(ConfigMiddleware())

    @server.tool()
    async def whoami(ctx: Context) -> str:
        config: ICUConfig = await ctx.get_state("config")
        async with ICUClient(config) as client:
            athlete = await client.get_athlete()
        return json.dumps(
            {"mode": config.auth_mode, "athlete": athlete.id, "display": config.display_athlete_id}
        )

    return server


@asynccontextmanager
async def _app_client(settings: RemoteSettings) -> AsyncIterator[httpx.AsyncClient]:
    remote = build_remote_app(_make_server(), settings)
    async with LifespanManager(remote.app) as manager:  # type: ignore[arg-type]
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            yield client


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def _sse_json(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    return json.loads(body)


@dataclass
class FlowResult:
    client_id: str
    upstream_authorize: httpx.URL
    callback: httpx.Response
    access_token: str | None = None


@pytest.fixture
def intervals():
    """Mock intervals.icu (token endpoint, disconnect, API). The ASGI app is not mocked."""
    with respx.mock(assert_all_called=False) as router:
        router.post(INTERVALS_TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": UPSTREAM_TOKEN,
                    "token_type": "Bearer",
                    "scope": SCOPE_STRING,
                    "athlete": {"id": "784427", "name": "Test Athlete"},
                },
            )
        )
        router.delete(INTERVALS_DISCONNECT_URL).mock(return_value=httpx.Response(200))
        router.get("https://intervals.icu/api/v1/athlete/0").mock(
            return_value=httpx.Response(200, json={"id": ATHLETE, "name": "Test Athlete"})
        )
        yield router


async def _register(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/register",
        json={
            "redirect_uris": [CLAUDE_CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Claude",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


async def _run_flow(
    client: httpx.AsyncClient, *, client_id: str | None = None, exchange: bool = True
) -> FlowResult:
    client_id = client_id or await _register(client)
    verifier, challenge = _pkce()

    authorize = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "claude-state",
            "scope": " ".join(DEFAULT_OAUTH_SCOPES),
        },
    )
    assert authorize.status_code in (302, 303, 307), authorize.text
    consent = await client.get(authorize.headers["location"])
    assert consent.status_code == 200, consent.text
    txn_id = re.search(r'name="txn_id" value="([^"]+)"', consent.text).group(1)  # type: ignore[union-attr]
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', consent.text).group(1)  # type: ignore[union-attr]

    approve = await client.post(
        "/consent", data={"txn_id": txn_id, "csrf_token": csrf, "action": "approve"}
    )
    assert approve.status_code == 302, approve.text
    upstream = httpx.URL(approve.headers["location"])

    callback = await client.get(
        "/auth/callback", params={"code": "upstream-code", "state": upstream.params["state"]}
    )
    result = FlowResult(client_id=client_id, upstream_authorize=upstream, callback=callback)
    if not exchange or callback.status_code != 302:
        return result

    location = urlsplit(callback.headers["location"])
    query = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == CLAUDE_CALLBACK
    assert query["state"] == ["claude-state"]
    token = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    result.access_token = token.json()["access_token"]
    return result


async def _call_whoami(client: httpx.AsyncClient, token: str | None, path: str = "/mcp") -> dict:
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }
    resp = await client.post(path, json=init, headers=headers)
    assert resp.status_code == 200, resp.text
    headers["mcp-session-id"] = resp.headers["mcp-session-id"]
    await client.post(
        path, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )
    call = await client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
        headers=headers,
    )
    payload = _sse_json(call.text)
    return json.loads(payload["result"]["content"][0]["text"])


# --------------------------------------------------------------------------- scopes


class TestScopes:
    def test_default_scopes_match_coach_needs(self):
        assert DEFAULT_OAUTH_SCOPES == [
            "ACTIVITY:READ",
            "WELLNESS:READ",
            "CALENDAR:WRITE",
            "SETTINGS:READ",
        ]
        assert format_upstream_scopes(DEFAULT_OAUTH_SCOPES) == SCOPE_STRING

    def test_parse_scope_list_accepts_commas_spaces_and_case(self):
        assert parse_scope_list("activity:read, CALENDAR:WRITE  calendar:write") == [
            "ACTIVITY:READ",
            "CALENDAR:WRITE",
        ]

    def test_write_implies_read_when_collapsing(self):
        assert scopes_for_tools(["icu_get_calendar_events", "icu_create_event"]) == [
            "CALENDAR:WRITE"
        ]

    async def test_every_registered_tool_has_a_scope_entry(self):
        from intervals_icu_mcp.server import mcp

        names = {t.name for t in await mcp.list_tools()}
        missing = names - SCOPE_BY_TOOL.keys()
        assert not missing, f"Add these tools to SCOPE_BY_TOOL: {sorted(missing)}"
        assert set(COACH_TOOLS) <= names

    def test_allowlist_normalizes(self):
        assert parse_allowlist("784427, i1 ,I2") == frozenset({"i784427", "i1", "i2"})
        assert parse_allowlist("") == frozenset()


class TestValidation:
    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"public_base_url": ""}, "PUBLIC_BASE_URL"),
            ({"public_base_url": "http://intervals.gadde.nu"}, "https://"),
            ({"intervals_oauth_client_secret": ""}, "INTERVALS_OAUTH_CLIENT_SECRET"),
            ({"oauth_scopes": "ACTIVITY:READ,EVERYTHING"}, "invalid scope"),
        ],
    )
    def test_invalid_config_rejected(self, tmp_path, overrides, fragment):
        with pytest.raises(RemoteConfigError) as exc:
            _settings(tmp_path, **overrides).validate_remote()
        assert fragment in str(exc.value)
        assert CLIENT_SECRET not in str(exc.value)

    def test_localhost_http_allowed_for_development(self, tmp_path):
        _settings(tmp_path, public_base_url="http://localhost:8000").validate_remote()


# --------------------------------------------------------------------------- metadata


class TestDiscoveryAnd401:
    async def test_unauthenticated_mcp_is_401_with_www_authenticate(self, tmp_path):
        async with _app_client(_settings(tmp_path)) as client:
            resp = await client.post("/mcp", json={}, headers=MCP_HEADERS)
        assert resp.status_code == 401
        challenge = resp.headers["www-authenticate"]
        assert challenge.startswith("Bearer")
        assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge

    async def test_invalid_token_is_401(self, tmp_path):
        async with _app_client(_settings(tmp_path)) as client:
            resp = await client.post(
                "/mcp", json={}, headers={**MCP_HEADERS, "Authorization": "Bearer nope"}
            )
        assert resp.status_code == 401
        assert "invalid_token" in resp.headers["www-authenticate"]

    async def test_protected_resource_metadata(self, tmp_path):
        async with _app_client(_settings(tmp_path)) as client:
            resp = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 200
        meta = resp.json()
        assert meta["resource"] == f"{BASE}/mcp"
        assert [s.rstrip("/") for s in meta["authorization_servers"]] == [BASE]
        assert meta["scopes_supported"] == DEFAULT_OAUTH_SCOPES

    async def test_authorization_server_metadata(self, tmp_path):
        async with _app_client(_settings(tmp_path)) as client:
            resp = await client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        meta = resp.json()
        assert meta["issuer"].rstrip("/") == BASE
        assert meta["authorization_endpoint"] == f"{BASE}/authorize"
        assert meta["token_endpoint"] == f"{BASE}/token"
        assert meta["registration_endpoint"] == f"{BASE}/register"
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert "none" in meta["token_endpoint_auth_methods_supported"]  # public clients
        assert meta["scopes_supported"] == DEFAULT_OAUTH_SCOPES

    async def test_forwarded_headers_do_not_change_public_urls(self, tmp_path):
        async with _app_client(_settings(tmp_path)) as client:
            resp = await client.get(
                "/.well-known/oauth-authorization-server",
                headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"},
            )
        assert "evil.example" not in resp.text

    async def test_unknown_redirect_uri_rejected_at_registration(self, tmp_path):
        async with _app_client(_settings(tmp_path)) as client:
            resp = await client.post(
                "/register",
                json={
                    "redirect_uris": ["https://attacker.example/cb"],
                    "token_endpoint_auth_method": "none",
                },
            )
        assert resp.status_code == 400


# --------------------------------------------------------------------------- flow


class TestProxyFlow:
    async def test_full_flow_reaches_tool_in_bearer_mode(self, tmp_path, intervals):
        async with _app_client(_settings(tmp_path)) as client:
            flow = await _run_flow(client)

            # Upstream authorize URL: Intervals.icu, exact callback, comma scopes, PKCE S256.
            up = flow.upstream_authorize
            assert f"{up.scheme}://{up.host}{up.path}" == "https://intervals.icu/oauth/authorize"
            assert up.params["client_id"] == "intervals-client-id"
            assert up.params["redirect_uri"] == f"{BASE}/auth/callback"
            assert up.params["scope"] == SCOPE_STRING
            assert up.params["code_challenge_method"] == "S256"
            assert up.params["code_challenge"]
            assert "resource" not in up.params

            # Token exchange used exactly the documented form fields.
            sent = parse_qs(intervals.calls[0].request.content.decode())
            assert sent["client_id"] == ["intervals-client-id"]
            assert sent["client_secret"] == [CLIENT_SECRET]
            assert sent["code"] == ["upstream-code"]
            assert len(sent["code_verifier"][0]) >= 43
            assert "scope" not in sent

            assert flow.access_token and flow.access_token != UPSTREAM_TOKEN
            result = await _call_whoami(client, flow.access_token)

        assert result == {"mode": "bearer", "athlete": ATHLETE, "display": ATHLETE}
        api_call = intervals.routes[2].calls.last.request
        assert api_call.headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"

    async def test_rejected_athlete_gets_clear_403_and_token_revoked(self, tmp_path, intervals):
        settings = _settings(tmp_path, allowed_athlete_ids="i1, i2")
        async with _app_client(settings) as client:
            flow = await _run_flow(client)
        assert flow.callback.status_code == 403
        assert "i784427" in flow.callback.text
        assert "ALLOWED_ATHLETE_IDS" in flow.callback.text
        assert "location" not in flow.callback.headers
        disconnect = intervals.routes[1]
        assert disconnect.called
        assert disconnect.calls.last.request.headers["authorization"] == f"Bearer {UPSTREAM_TOKEN}"

    async def test_empty_allowlist_rejects_everyone(self, tmp_path, intervals):
        async with _app_client(_settings(tmp_path, allowed_athlete_ids="")) as client:
            flow = await _run_flow(client)
        assert flow.callback.status_code == 403

    async def test_bare_numeric_allowlist_entry_matches(self, tmp_path, intervals):
        async with _app_client(_settings(tmp_path, allowed_athlete_ids="784427")) as client:
            flow = await _run_flow(client)
        assert flow.callback.status_code == 302

    async def test_upstream_error_is_reported_without_secrets(self, tmp_path, intervals):
        intervals.post(INTERVALS_TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        async with _app_client(_settings(tmp_path)) as client:
            flow = await _run_flow(client)
        assert flow.callback.status_code == 500
        assert "invalid_grant" in flow.callback.text
        assert CLIENT_SECRET not in flow.callback.text

    async def test_missing_scope_in_response_falls_back_to_requested(self, tmp_path, intervals):
        intervals.post(INTERVALS_TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": UPSTREAM_TOKEN, "athlete": {"id": "i784427"}}
            )
        )
        async with _app_client(_settings(tmp_path)) as client:
            flow = await _run_flow(client)
            assert flow.access_token
            assert (await _call_whoami(client, flow.access_token))["mode"] == "bearer"

    async def test_secrets_and_tokens_never_logged(self, tmp_path, intervals, caplog):
        caplog.set_level(logging.DEBUG)
        # FastMCP's logger does not propagate to root; capture it directly too.
        fastmcp_logger = logging.getLogger("fastmcp")
        old_level = fastmcp_logger.level
        fastmcp_logger.setLevel(logging.DEBUG)
        fastmcp_logger.addHandler(caplog.handler)
        try:
            async with _app_client(_settings(tmp_path)) as client:
                flow = await _run_flow(client)
                assert flow.access_token
                await _call_whoami(client, flow.access_token)
                # A rejected token path logs too.
                await client.post(
                    "/mcp", json={}, headers={**MCP_HEADERS, "Authorization": "Bearer junk"}
                )
        finally:
            fastmcp_logger.removeHandler(caplog.handler)
            fastmcp_logger.setLevel(old_level)
        assert any(r.name.startswith("fastmcp") for r in caplog.records)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert UPSTREAM_TOKEN not in text
        assert CLIENT_SECRET not in text
        assert flow.access_token not in text


class TestPersistence:
    async def test_tokens_and_clients_survive_restart(self, tmp_path, intervals):
        settings = _settings(tmp_path)
        async with _app_client(settings) as client:
            flow = await _run_flow(client)
        assert flow.access_token

        # "Restart": brand-new server, provider and app on the same DATA_DIR.
        async with _app_client(_settings(tmp_path)) as client:
            assert (await _call_whoami(client, flow.access_token))["mode"] == "bearer"
            # The DCR client registered before the restart is still known.
            again = await _run_flow(client, client_id=flow.client_id)
            assert again.access_token

    async def test_allowlist_rechecked_after_restart(self, tmp_path, intervals):
        async with _app_client(_settings(tmp_path)) as client:
            flow = await _run_flow(client)
        async with _app_client(_settings(tmp_path, allowed_athlete_ids="i1")) as client:
            resp = await client.post(
                "/mcp",
                json={},
                headers={**MCP_HEADERS, "Authorization": f"Bearer {flow.access_token}"},
            )
        assert resp.status_code == 401

    async def test_rotated_client_secret_invalidates_tokens(self, tmp_path, intervals):
        async with _app_client(_settings(tmp_path)) as client:
            flow = await _run_flow(client)
        rotated = _settings(tmp_path, intervals_oauth_client_secret="another-secret-placeholder")
        async with _app_client(rotated) as client:
            resp = await client.post(
                "/mcp",
                json={},
                headers={**MCP_HEADERS, "Authorization": f"Bearer {flow.access_token}"},
            )
        assert resp.status_code == 401

    async def test_storage_is_encrypted_and_private(self, tmp_path, intervals):
        async with _app_client(_settings(tmp_path)) as client:
            await _run_flow(client)
        storage = tmp_path / "data" / "oauth"
        assert stat.S_IMODE(storage.stat().st_mode) == 0o700
        files = [p for p in storage.rglob("*") if p.is_file()]
        assert files
        for path in files:
            content = path.read_bytes()
            assert UPSTREAM_TOKEN.encode() not in content
            assert b"784427" not in content


class TestWithSecretPath:
    async def test_oauth_and_secret_path_side_by_side(self, tmp_path, intervals, monkeypatch):
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "operator-api-key")
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i555")
        intervals.get("https://intervals.icu/api/v1/athlete/i555").mock(
            return_value=httpx.Response(200, json={"id": "i555", "name": "Operator"})
        )
        secret = secrets.token_urlsafe(32)
        settings = _settings(tmp_path, mcp_path_secret=secret)
        async with _app_client(settings) as client:
            unauth = await client.post("/mcp", json={}, headers=MCP_HEADERS)
            assert unauth.status_code == 401
            via_secret = await _call_whoami(client, None, path=f"/{secret}/mcp")
            assert via_secret == {"mode": "api_key", "athlete": "i555", "display": "i555"}
            assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 200
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/random")).status_code == 404
            flow = await _run_flow(client)
            assert (await _call_whoami(client, flow.access_token))["mode"] == "bearer"
