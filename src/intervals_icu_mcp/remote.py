"""Remote (HTTP) deployment features: secret path and OAuth.

Everything here is opt-in through environment variables and only applies to the
streamable-HTTP transport. When none of the settings is present, ``server.main``
keeps using FastMCP's own ``mcp.run(...)`` and behaviour is unchanged.

Security notes:
- The path secret is compared in constant time (``hmac.compare_digest``).
- Validation messages never echo configured values, so a misconfigured secret
  does not end up in a startup log.
- Access-log lines are rewritten before they are formatted (see SecretRedactingFilter),
  and a matched secret path is rewritten in the ASGI scope to ``/__secret__/mcp`` before any
  downstream code (or uvicorn's access logger) sees it.
"""

from __future__ import annotations

import copy
import hmac
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from fastmcp import FastMCP

MIN_PATH_SECRET_LENGTH = 32
# URL-safe, single path segment. Anything else would need percent-encoding or could
# split into several segments, which makes "the first segment" ambiguous.
_PATH_SECRET_RE = re.compile(r"^[A-Za-z0-9._~-]+$")

#: Path the secret-path endpoint is served at *inside* the app. External requests to
#: this literal path are rejected by the gate; only a matched secret is rewritten to it.
#: It doubles as the masked form that appears in access logs.
MASKED_MCP_PATH = "/__secret__/mcp"
DEFAULT_MCP_PATH = "/mcp"
HEALTH_PATHS = ("/health", "/healthz")


class RemoteConfigError(ValueError):
    """Invalid remote-deployment configuration. Messages never contain secret values."""


class RemoteSettings(BaseSettings):
    """Opt-in settings for remote HTTP deployments. All empty by default."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Phase 2 — secret-path mode
    mcp_path_secret: str = ""

    # Phase 3 — OAuth via Intervals.icu
    intervals_oauth_client_id: str = ""
    intervals_oauth_client_secret: str = ""
    public_base_url: str = ""
    allowed_athlete_ids: str = ""
    oauth_scopes: str = ""
    oauth_jwt_signing_key: str = ""
    oauth_allowed_client_redirect_uris: str = ""
    data_dir: str = ""

    def __repr_args__(self):  # pragma: no cover - trivial
        # Never render values: several fields are secrets.
        return [(name, "***" if getattr(self, name) else "") for name in type(self).model_fields]

    # ------------------------------------------------------------------ helpers

    @property
    def path_secret_enabled(self) -> bool:
        return bool(self.mcp_path_secret)

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.intervals_oauth_client_id or self.intervals_oauth_client_secret)

    @property
    def any_enabled(self) -> bool:
        return self.path_secret_enabled or self.oauth_enabled

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir or "./data").expanduser()

    def validate_remote(self) -> None:
        """Validate cross-field rules. Raises RemoteConfigError with safe messages."""
        secret = self.mcp_path_secret
        if secret:
            if len(secret) < MIN_PATH_SECRET_LENGTH:
                raise RemoteConfigError(
                    f"MCP_PATH_SECRET must be at least {MIN_PATH_SECRET_LENGTH} characters "
                    f"(got {len(secret)}). Generate one with: "
                    "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
                )
            if not _PATH_SECRET_RE.match(secret):
                raise RemoteConfigError(
                    "MCP_PATH_SECRET may only contain letters, digits and . _ ~ - "
                    "(it must be a single URL path segment)."
                )
        if self.oauth_enabled:
            from .oauth import validate_oauth_settings

            validate_oauth_settings(self)

    def secrets_to_redact(self) -> list[str]:
        values = [
            self.mcp_path_secret,
            self.intervals_oauth_client_secret,
            self.oauth_jwt_signing_key,
        ]
        return [v for v in values if v]


def load_remote_settings() -> RemoteSettings:
    return RemoteSettings()


# --------------------------------------------------------------------------- logging


class SecretRedactingFilter(logging.Filter):
    """Replace configured secret values with ``***`` in log records.

    Attached to uvicorn's handlers, so it covers the access log and the error log.
    """

    def __init__(self, secrets: list[str] | None = None, name: str = "") -> None:
        super().__init__(name)
        self._secrets = sorted({s for s in (secrets or []) if s}, key=len, reverse=True)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                if secret in value:
                    value = value.replace(secret, "***")
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._redact(v) for k, v in record.args.items()}
        return True


def build_log_config(secrets: list[str], log_level: str = "info") -> dict[str, Any]:
    """Uvicorn's default logging config with the redacting filter on every handler."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_secrets"] = {
        "()": SecretRedactingFilter,
        "secrets": list(secrets),
    }
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", []).append("redact_secrets")
    for logger_cfg in config.get("loggers", {}).values():
        if "level" in logger_cfg:
            logger_cfg["level"] = log_level.upper()
    return config


def install_root_redaction(secrets: list[str]) -> None:
    """Redact secrets in every handler outside uvicorn's own config.

    FastMCP's logger does not propagate to root and has its own handlers, so it
    is covered explicitly alongside the root logger.
    """
    redactor = SecretRedactingFilter(secrets)
    for name in ("", "fastmcp", "mcp"):
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(redactor)


# --------------------------------------------------------------------------- gate


def _not_found() -> ASGIApp:
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("Not Found", status_code=404)


class PathSecretGate:
    """Outermost ASGI wrapper for secret-path mode.

    - ``/<secret>/mcp`` (constant-time match) is rewritten **in place** to
      ``/__secret__/mcp`` and handed to the app. In-place, so uvicorn's access log, which
      reads the same scope dict, records the masked path.
    - Paths in ``public_paths`` (health, OAuth endpoints and the OAuth
      ``/mcp`` when enabled) pass through unchanged.
    - Everything else, including ``/mcp`` without OAuth and the literal masked
      path, gets a 404.
    """

    def __init__(self, app: ASGIApp, secret: str, public_paths: set[str]) -> None:
        self.app = app
        self._secret = secret.encode()
        self.public_paths = frozenset(public_paths)

    def _matches_secret(self, path: str) -> bool:
        # "/<segment>/mcp" or "/<segment>/mcp/" — anything deeper is not an MCP path.
        if not path.startswith("/"):
            return False
        segment, sep, rest = path[1:].partition("/")
        if not sep or rest not in ("mcp", "mcp/"):
            # Still burn a comparison so timing does not reveal the path shape.
            hmac.compare_digest(segment.encode(), self._secret)
            return False
        return hmac.compare_digest(segment.encode(), self._secret)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if self._matches_secret(path):
            scope["path"] = MASKED_MCP_PATH
            scope["raw_path"] = MASKED_MCP_PATH.encode()
            await self.app(scope, receive, send)
            return

        if path in self.public_paths and path != MASKED_MCP_PATH:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            await _not_found()(scope, receive, send)
        else:  # websocket
            await send({"type": "websocket.close", "code": 1008})


# --------------------------------------------------------------------------- app


@dataclass
class RemoteApp:
    """The assembled ASGI app plus what the runner and the startup log need."""

    app: ASGIApp
    public_paths: set[str] = field(default_factory=set[str])
    log_secrets: list[str] = field(default_factory=list[str])
    description: list[str] = field(default_factory=list[str])


async def _health(request: Any) -> Any:
    """Static liveness: no auth, no upstream calls, nothing sensitive."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


def _unwrap_auth(endpoint: Any) -> Any:
    """Return the StreamableHTTP ASGI app inside FastMCP's RequireAuthMiddleware."""
    inner = getattr(endpoint, "app", None)
    if inner is None:
        raise RuntimeError("Unexpected FastMCP route layout: cannot locate the MCP endpoint app")
    return inner


def build_remote_app(
    server: FastMCP[Any],
    settings: RemoteSettings,
    *,
    path: str | None = None,
) -> RemoteApp:
    """Assemble the streamable-HTTP app for the configured remote features.

    Layout:
    - OAuth on:       ``/mcp`` requires an Intervals.icu OAuth token (bearer mode).
    - Secret on:      ``/<secret>/mcp`` serves the API-key mode endpoint, no OAuth.
    - Both:           both of the above, side by side.
    - Always:         ``/health`` and ``/healthz``.
    """
    settings.validate_remote()
    description: list[str] = []

    mcp_path = path or DEFAULT_MCP_PATH
    if settings.oauth_enabled:
        from .oauth import build_oauth_provider

        server.auth = build_oauth_provider(settings)
        description.append(f"oauth=on endpoint={mcp_path}")
        app = server.http_app(path=mcp_path, transport="http")
        if settings.path_secret_enabled:
            # Same session manager, second route without the auth wrapper.
            mcp_route = next(
                r for r in app.router.routes if isinstance(r, Route) and r.path == mcp_path
            )
            app.router.routes.append(
                Route(
                    MASKED_MCP_PATH,
                    endpoint=_unwrap_auth(mcp_route.endpoint),
                    methods=mcp_route.methods,
                )
            )
    elif settings.path_secret_enabled:
        app = server.http_app(path=MASKED_MCP_PATH, transport="http")
    else:
        app = server.http_app(path=mcp_path, transport="http")

    existing = {r.path for r in app.router.routes if isinstance(r, Route)}
    for health_path in HEALTH_PATHS:
        if health_path not in existing:
            app.router.routes.append(Route(health_path, endpoint=_health, methods=["GET"]))

    public_paths = {
        r.path for r in app.router.routes if isinstance(r, Route) and r.path != MASKED_MCP_PATH
    }

    outer: ASGIApp = app
    if settings.path_secret_enabled:
        outer = PathSecretGate(app, settings.mcp_path_secret, public_paths)
        description.append("secret_path=on endpoint=/<MCP_PATH_SECRET>/mcp")

    return RemoteApp(
        app=outer,
        public_paths=public_paths,
        log_secrets=settings.secrets_to_redact(),
        description=description,
    )


def run_remote(
    server: FastMCP[Any],
    settings: RemoteSettings,
    *,
    host: str,
    port: int,
    path: str | None = None,
    log_level: str = "info",
) -> None:
    """Build the remote app and serve it with uvicorn (FastMCP's defaults, plus redaction)."""
    import uvicorn

    remote = build_remote_app(server, settings, path=path)
    install_root_redaction(remote.log_secrets)
    print(
        "intervals-icu MCP remote mode: " + (", ".join(remote.description) or "plain"),
        file=sys.stderr,
    )
    uvicorn.run(
        remote.app,
        host=host,
        port=port,
        log_config=build_log_config(remote.log_secrets, log_level),
        timeout_graceful_shutdown=2,
        lifespan="on",
        ws="websockets-sansio",
        # TLS terminates at the reverse proxy. Uvicorn trusts X-Forwarded-* only from
        # FORWARDED_ALLOW_IPS (env, default 127.0.0.1); public URLs never depend on
        # them because they are built from PUBLIC_BASE_URL.
        proxy_headers=True,
    )
