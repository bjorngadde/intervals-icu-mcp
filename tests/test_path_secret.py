"""Secret-path mode (MCP_PATH_SECRET) and /healthz."""

import json
import logging
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager

from intervals_icu_mcp import server as server_module
from intervals_icu_mcp.remote import (
    MASKED_MCP_PATH,
    PathSecretGate,
    RemoteConfigError,
    RemoteSettings,
    SecretRedactingFilter,
    build_log_config,
    build_remote_app,
)
from intervals_icu_mcp.server import mcp

SECRET = secrets.token_urlsafe(32)  # 43 chars, URL-safe
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def _client(settings: RemoteSettings) -> AsyncIterator[httpx.AsyncClient]:
    remote = build_remote_app(mcp, settings)
    async with LifespanManager(remote.app) as manager:  # type: ignore[arg-type]
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


def _first_sse_json(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(body)


class TestSecretPathRouting:
    async def test_correct_secret_serves_mcp(self):
        async with _client(RemoteSettings(mcp_path_secret=SECRET)) as client:
            async with client.stream(
                "POST", f"/{SECRET}/mcp", json=INIT, headers=MCP_HEADERS
            ) as resp:
                assert resp.status_code == 200
                payload = _first_sse_json((await resp.aread()).decode())
        assert payload["result"]["serverInfo"]["name"] == "intervals_icu_mcp"

    async def test_trailing_slash_also_served(self):
        async with _client(RemoteSettings(mcp_path_secret=SECRET)) as client:
            async with client.stream(
                "POST", f"/{SECRET}/mcp/", json=INIT, headers=MCP_HEADERS
            ) as resp:
                assert resp.status_code == 200

    @pytest.mark.parametrize(
        "path",
        [
            "/mcp",
            "/mcp/",
            "/",
            f"/{SECRET[:-1]}/mcp",
            f"/{SECRET}x/mcp",
            f"/{SECRET.upper() if SECRET.upper() != SECRET else SECRET + 'A'}/mcp",
            f"/{SECRET}",
            f"/{SECRET}/",
            f"/{SECRET}/mcp/extra",
            f"/{SECRET}/other",
            f"/x/{SECRET}/mcp",
            MASKED_MCP_PATH,
            "/__secret__/mcp/",
            "/.well-known/oauth-authorization-server",
            "/webhooks/intervals",
            "/authorize",
            "/docs",
        ],
    )
    async def test_wrong_or_missing_secret_is_404(self, path):
        async with _client(RemoteSettings(mcp_path_secret=SECRET)) as client:
            post = await client.post(path, json=INIT, headers=MCP_HEADERS)
            get = await client.get(path)
        assert post.status_code == 404
        assert get.status_code == 404
        assert SECRET not in post.text

    @pytest.mark.parametrize("path", ["/healthz", "/health"])
    async def test_health_always_200(self, path):
        async with _client(RemoteSettings(mcp_path_secret=SECRET)) as client:
            resp = await client.get(path)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestHealthzWithoutSecret:
    async def test_healthz_on_plain_http_app(self):
        app = mcp.http_app(path="/mcp")
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                resp = await client.get("/healthz")
                mcp_resp = await client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        # Unset secret: /mcp is served as before.
        assert mcp_resp.status_code == 200


class TestSecretValidation:
    def test_short_secret_rejected(self):
        with pytest.raises(RemoteConfigError) as exc:
            RemoteSettings(mcp_path_secret="x" * 31).validate_remote()
        assert "at least 32" in str(exc.value)
        assert "x" * 31 not in str(exc.value)

    def test_32_char_secret_accepted(self):
        RemoteSettings(mcp_path_secret="a" * 32).validate_remote()

    @pytest.mark.parametrize("bad", ["a" * 31 + "/", "a" * 31 + "?", "a" * 32 + " ", "ä" * 32])
    def test_non_url_safe_secret_rejected(self, bad):
        with pytest.raises(RemoteConfigError) as exc:
            RemoteSettings(mcp_path_secret=bad).validate_remote()
        assert bad not in str(exc.value)

    def test_build_refuses_short_secret(self):
        with pytest.raises(RemoteConfigError):
            build_remote_app(mcp, RemoteSettings(mcp_path_secret="short"))

    def test_main_exits_on_short_secret(self, monkeypatch, capsys):
        monkeypatch.setenv("MCP_PATH_SECRET", "too-short-secret")
        monkeypatch.setattr(sys, "argv", ["intervals-icu-mcp", "--transport", "http"])
        called = []
        monkeypatch.setattr("intervals_icu_mcp.remote.run_remote", lambda *a, **k: called.append(1))
        with pytest.raises(SystemExit) as exc:
            server_module.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "MCP_PATH_SECRET must be at least 32" in err
        assert "too-short-secret" not in err
        assert not called

    def test_main_rejects_path_with_secret(self, monkeypatch, capsys):
        monkeypatch.setenv("MCP_PATH_SECRET", SECRET)
        monkeypatch.setattr(
            sys, "argv", ["intervals-icu-mcp", "--transport", "http", "--path", "/x"]
        )
        with pytest.raises(SystemExit):
            server_module.main()
        assert SECRET not in capsys.readouterr().err

    def test_stdio_ignores_remote_settings(self, monkeypatch):
        monkeypatch.setenv("MCP_PATH_SECRET", "short")
        monkeypatch.setattr(sys, "argv", ["intervals-icu-mcp"])
        ran = []
        monkeypatch.setattr(server_module.mcp, "run", lambda *a, **k: ran.append(a))
        server_module.main()
        assert ran == [()]

    def test_unset_secret_uses_plain_fastmcp_run(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["intervals-icu-mcp", "--transport", "http"])
        ran = []
        monkeypatch.setattr(server_module.mcp, "run", lambda *a, **k: ran.append(k))
        server_module.main()
        assert ran == [{"transport": "http", "host": "127.0.0.1", "port": 8000}]


class TestLogMasking:
    def test_redacting_filter_masks_access_log_args(self):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "POST", f"/{SECRET}/mcp", "1.1", 404),
            None,
        )
        SecretRedactingFilter([SECRET]).filter(record)
        assert SECRET not in record.getMessage()
        assert "/***/mcp" in record.getMessage()

    def test_log_config_attaches_filter_to_every_handler(self):
        config = build_log_config([SECRET])
        assert config["handlers"]
        for handler in config["handlers"].values():
            assert "redact_secrets" in handler["filters"]

    async def test_matched_secret_is_rewritten_in_scope(self):
        """uvicorn's access log reads the same scope dict: it must hold the masked path."""
        seen: list[str] = []

        async def app(scope, receive, send):
            seen.append(scope["path"])
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        gate = PathSecretGate(app, SECRET, public_paths=set())
        scope = {"type": "http", "path": f"/{SECRET}/mcp", "raw_path": f"/{SECRET}/mcp".encode()}
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        await gate(scope, receive, send)
        assert seen == [MASKED_MCP_PATH]
        assert scope["path"] == MASKED_MCP_PATH
        assert SECRET.encode() not in scope["raw_path"]

    def test_settings_repr_hides_values(self):
        settings = RemoteSettings(mcp_path_secret=SECRET, oauth_jwt_signing_key="w" * 20)
        assert SECRET not in repr(settings)
        assert "w" * 20 not in repr(settings)
