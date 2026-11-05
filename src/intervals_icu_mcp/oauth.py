"""OAuth 2.1 for MCP clients, proxied to Intervals.icu.

The server is an OAuth authorization server towards MCP clients (Claude): it serves
discovery metadata, Dynamic Client Registration (DCR) and PKCE, and issues its own
short reference tokens (FastMCP JWTs). Logins are proxied to Intervals.icu, using a
single pre-registered Intervals.icu OAuth app. This is FastMCP's ``OAuthProxy``; this
module adds what is specific to Intervals.icu:

- **Scopes** are comma-separated upstream (``ACTIVITY:READ,CALENDAR:WRITE``) and
  space-separated towards MCP clients (OAuth standard). See ``SCOPE_BY_TOOL``.
- **Token exchange** is ``POST /api/oauth/token`` with form fields ``client_id``,
  ``client_secret``, ``code`` and ``code_verifier`` (PKCE S256 on the upstream leg).
  The response carries ``athlete: {id, name}``, with no refresh token and no expiry.
- **Athlete allowlist**: the athlete in the token response must be listed in
  ``ALLOWED_ATHLETE_IDS``. An empty allowlist rejects everyone. A rejected login gets
  a clear 403 page, and the just-issued upstream token is revoked (best effort).
- **Token verification**: upstream tokens are opaque, so there is no JWKS. At
  exchange time the server stores ``sha256(token) -> athlete`` (encrypted, under
  ``DATA_DIR``), and each request is verified against that mapping and the
  *current* allowlist. There is no extra network round trip per call, and nothing
  depends on ``SETTINGS:READ``.
- **Persistence**: DCR clients, transactions, issued-token mappings, the encrypted
  upstream tokens and the athlete mapping all live in a Fernet-encrypted FileTreeStore
  under ``DATA_DIR/oauth`` (directory mode 0700). They survive restarts.
"""

from __future__ import annotations

import contextvars
import hashlib
import html
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.ui import create_error_html
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .auth import normalize_athlete_id
from .client import register_bearer_unauthorized_hook
from .middleware import INTERVALS_ATHLETE_CLAIM
from .remote import RemoteConfigError, RemoteSettings

logger = logging.getLogger(__name__)

INTERVALS_AUTHORIZE_URL = "https://intervals.icu/oauth/authorize"
INTERVALS_TOKEN_URL = "https://intervals.icu/api/oauth/token"
INTERVALS_DISCONNECT_URL = "https://intervals.icu/api/v1/disconnect-app"
REDIRECT_PATH = "/auth/callback"
UPSTREAM_TIMEOUT_SECONDS = 15.0
# Intervals.icu tokens never expire. Keep the athlete mapping as long as FastMCP keeps
# the upstream token set for a no-refresh token (one year); a re-login renews both.
ATHLETE_MAPPING_TTL_SECONDS = 365 * 24 * 3600

# --------------------------------------------------------------------------- scopes

#: Intervals.icu OAuth scopes, by API area. ``X:WRITE`` is assumed to include ``X:READ``
#: (the brief's example default relies on that; verify live — see NOTES.md).
SCOPE_AREAS = ("ACTIVITY", "WELLNESS", "CALENDAR", "LIBRARY", "SETTINGS")
_SCOPE_RE = re.compile(r"^[A-Z][A-Z_]*:(READ|WRITE)$")

#: The scope each tool needs, derived from the API path family it calls:
#: activities/streams/curves/messages -> ACTIVITY, wellness & fitness -> WELLNESS,
#: events -> CALENDAR, workouts/folders -> LIBRARY, athlete profile, sport settings,
#: gear and custom items -> SETTINGS. ``None`` = not usable with an OAuth token.
SCOPE_BY_TOOL: dict[str, str | None] = {
    # Activities
    "icu_get_recent_activities": "ACTIVITY:READ",
    "icu_get_activities_by_date": "ACTIVITY:READ",
    "icu_get_activity_details": "ACTIVITY:READ",
    "icu_search_activities": "ACTIVITY:READ",
    "icu_search_activities_full": "ACTIVITY:READ",
    "icu_get_activities_around": "ACTIVITY:READ",
    "icu_update_activity": "ACTIVITY:WRITE",
    "icu_update_activity_streams": "ACTIVITY:WRITE",
    "icu_bulk_create_manual_activities": "ACTIVITY:WRITE",
    "icu_delete_activity": "ACTIVITY:WRITE",
    "icu_download_activity_file": "ACTIVITY:READ",
    "icu_download_fit_file": "ACTIVITY:READ",
    "icu_download_gpx_file": "ACTIVITY:READ",
    # Activity analysis
    "icu_get_activity_streams": "ACTIVITY:READ",
    "icu_get_stream_stats": "ACTIVITY:READ",
    "icu_get_activity_intervals": "ACTIVITY:READ",
    "icu_get_best_efforts": "ACTIVITY:READ",
    "icu_search_intervals": "ACTIVITY:READ",
    "icu_get_power_histogram": "ACTIVITY:READ",
    "icu_get_hr_histogram": "ACTIVITY:READ",
    "icu_get_pace_histogram": "ACTIVITY:READ",
    "icu_get_gap_histogram": "ACTIVITY:READ",
    # Activity messages
    "icu_get_activity_messages": "ACTIVITY:READ",
    "icu_add_activity_message": "ACTIVITY:WRITE",
    "icu_update_activity_message": "ACTIVITY:WRITE",
    "icu_delete_activity_message": "ACTIVITY:WRITE",
    # Athlete / fitness
    "icu_list_athletes": None,  # GET /athletes is API-key only
    "icu_get_athlete_profile": "SETTINGS:READ",
    "icu_update_athlete_notes": "SETTINGS:WRITE",
    "icu_get_fitness_summary": "WELLNESS:READ",
    "icu_get_fitness_chart": "WELLNESS:READ",
    # Wellness
    "icu_get_wellness_data": "WELLNESS:READ",
    "icu_get_wellness_for_date": "WELLNESS:READ",
    "icu_update_wellness": "WELLNESS:WRITE",
    # Calendar
    "icu_get_calendar_events": "CALENDAR:READ",
    "icu_get_upcoming_workouts": "CALENDAR:READ",
    "icu_get_event": "CALENDAR:READ",
    "icu_get_seasons": "CALENDAR:READ",
    "icu_get_annual_training_plan": "CALENDAR:READ",
    "icu_create_event": "CALENDAR:WRITE",
    "icu_update_event": "CALENDAR:WRITE",
    "icu_delete_event": "CALENDAR:WRITE",
    "icu_bulk_create_events": "CALENDAR:WRITE",
    "icu_bulk_update_event_access": "CALENDAR:WRITE",
    "icu_bulk_delete_events": "CALENDAR:WRITE",
    "icu_duplicate_events": "CALENDAR:WRITE",
    "icu_apply_training_plan": "CALENDAR:WRITE",
    # Curves
    "icu_get_power_curves": "ACTIVITY:READ",
    "icu_get_hr_curves": "ACTIVITY:READ",
    "icu_get_pace_curves": "ACTIVITY:READ",
    # Workout library
    "icu_get_workout_library": "LIBRARY:READ",
    "icu_get_workouts_in_folder": "LIBRARY:READ",
    "icu_create_workout": "LIBRARY:WRITE",
    "icu_update_workout": "LIBRARY:WRITE",
    "icu_delete_workout": "LIBRARY:WRITE",
    "icu_bulk_create_workouts": "LIBRARY:WRITE",
    "icu_create_workout_folder": "LIBRARY:WRITE",
    "icu_delete_workout_folder": "LIBRARY:WRITE",
    # Gear
    "icu_get_gear_list": "SETTINGS:READ",
    "icu_create_gear": "SETTINGS:WRITE",
    "icu_update_gear": "SETTINGS:WRITE",
    "icu_delete_gear": "SETTINGS:WRITE",
    "icu_create_gear_reminder": "SETTINGS:WRITE",
    "icu_update_gear_reminder": "SETTINGS:WRITE",
    # Sport settings
    "icu_get_sport_settings": "SETTINGS:READ",
    "icu_update_sport_settings": "SETTINGS:WRITE",
    "icu_create_sport_settings": "SETTINGS:WRITE",
    "icu_delete_sport_settings": "SETTINGS:WRITE",
    # Custom items
    "icu_get_custom_items": "SETTINGS:READ",
    "icu_get_custom_item": "SETTINGS:READ",
    "icu_create_custom_item": "SETTINGS:WRITE",
    "icu_update_custom_item": "SETTINGS:WRITE",
    "icu_delete_custom_item": "SETTINGS:WRITE",
}

#: What the daily coach job uses: read training, wellness, fitness and the calendar;
#: write planned workouts to the calendar; read the profile/zones (athlete/0 needs
#: SETTINGS:READ). Library, gear and settings writes are deliberately left out.
COACH_TOOLS = (
    "icu_get_recent_activities",
    "icu_get_activities_by_date",
    "icu_get_activity_details",
    "icu_get_activity_intervals",
    "icu_get_wellness_data",
    "icu_get_fitness_summary",
    "icu_get_fitness_chart",
    "icu_get_calendar_events",
    "icu_get_upcoming_workouts",
    "icu_create_event",
    "icu_update_event",
    "icu_bulk_create_events",
    "icu_get_athlete_profile",
    "icu_get_sport_settings",
)


def _collapse(scopes: set[str]) -> list[str]:
    """Drop ``X:READ`` when ``X:WRITE`` is present; order by area."""
    out: list[str] = []
    for area in SCOPE_AREAS:
        if f"{area}:WRITE" in scopes:
            out.append(f"{area}:WRITE")
        elif f"{area}:READ" in scopes:
            out.append(f"{area}:READ")
    return out


def scopes_for_tools(tools: tuple[str, ...] | list[str]) -> list[str]:
    needed = {s for t in tools if (s := SCOPE_BY_TOOL.get(t))}
    return _collapse(needed)


#: ``ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:READ``
DEFAULT_OAUTH_SCOPES: list[str] = scopes_for_tools(COACH_TOOLS)


def parse_scope_list(value: str) -> list[str]:
    """Parse ``OAUTH_SCOPES`` (comma and/or space separated), de-duplicated, in order."""
    seen: list[str] = []
    for part in value.replace(",", " ").split():
        token = part.strip().upper()
        if token and token not in seen:
            seen.append(token)
    return seen


def format_upstream_scopes(scopes: list[str]) -> str:
    """Intervals.icu wants comma-separated scopes."""
    return ",".join(scopes)


def parse_allowlist(value: str) -> frozenset[str]:
    return frozenset(
        normalize_athlete_id(part) for part in value.replace(",", " ").split() if part.strip()
    )


DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]


def allowed_client_redirect_uris(settings: RemoteSettings) -> list[str] | None:
    """MCP-client redirect URIs accepted at DCR/authorize. ``*`` = any (FastMCP default)."""
    raw = settings.oauth_allowed_client_redirect_uris.strip()
    if not raw:
        return list(DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS)
    if raw == "*":
        return None
    return [u.strip() for u in raw.replace(" ", ",").split(",") if u.strip()]


def effective_scopes(settings: RemoteSettings) -> list[str]:
    return parse_scope_list(settings.oauth_scopes) or list(DEFAULT_OAUTH_SCOPES)


def public_base_url(settings: RemoteSettings) -> str:
    return settings.public_base_url.strip().rstrip("/")


def validate_oauth_settings(settings: RemoteSettings) -> None:
    """Raise RemoteConfigError (never echoing secrets) for an unusable OAuth config."""
    if not settings.intervals_oauth_client_id or not settings.intervals_oauth_client_secret:
        raise RemoteConfigError(
            "OAuth needs both INTERVALS_OAUTH_CLIENT_ID and INTERVALS_OAUTH_CLIENT_SECRET."
        )
    base = public_base_url(settings)
    if not base:
        raise RemoteConfigError(
            "OAuth needs PUBLIC_BASE_URL (e.g. https://mcp.example.com) — issuer and "
            "callback URLs are built from it."
        )
    parts = urlsplit(base)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise RemoteConfigError(
            "PUBLIC_BASE_URL must be an https:// URL (http only for localhost)."
        )
    if not parts.hostname or parts.query or parts.fragment:
        raise RemoteConfigError("PUBLIC_BASE_URL must be a plain base URL without query/fragment.")
    for scope in effective_scopes(settings):
        if not _SCOPE_RE.match(scope):
            raise RemoteConfigError(
                f"OAUTH_SCOPES contains an invalid scope '{scope}'. "
                "Expected AREA:READ or AREA:WRITE, e.g. ACTIVITY:READ,CALENDAR:WRITE."
            )
    if not parse_allowlist(settings.allowed_athlete_ids):
        logger.warning(
            "ALLOWED_ATHLETE_IDS is empty: every OAuth login will be rejected. "
            "Add your athlete ID (e.g. i123456) to allow access."
        )


# --------------------------------------------------------------------------- storage


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - e.g. foreign-owned volume
        logger.warning("Could not restrict permissions on %s", path)
    return path


def jwt_signing_key(settings: RemoteSettings) -> bytes:
    """Key for FastMCP-issued tokens; also the root of the storage encryption key.

    Defaults to an HKDF derivation of the upstream client secret (FastMCP's own
    default), so a restart with the same secret keeps every issued token valid and
    rotating the client secret invalidates them all.
    """
    if settings.oauth_jwt_signing_key:
        return derive_jwt_key(
            low_entropy_material=settings.oauth_jwt_signing_key, salt="fastmcp-jwt-signing-key"
        )
    return derive_jwt_key(
        high_entropy_material=settings.intervals_oauth_client_secret,
        salt="fastmcp-jwt-signing-key",
    )


def build_storage(data_dir: Path, signing_key: bytes) -> AsyncKeyValue:
    """Encrypted file store under ``<data_dir>/oauth`` (mode 0700)."""
    storage_dir = _secure_dir(_secure_dir(data_dir) / "oauth")
    encryption_key = derive_jwt_key(
        high_entropy_material=signing_key.decode(), salt="fastmcp-storage-encryption-key"
    )
    file_store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
    )
    return FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(key=encryption_key),
        raise_on_decryption_error=False,
    )


# --------------------------------------------------------------------------- verifier


class AthleteGrant(BaseModel):
    """What the server remembers about an upstream token (keyed by its SHA-256)."""

    athlete_id: str
    athlete_name: str | None = None
    scopes: list[str]
    created_at: float


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class IntervalsTokenVerifier(TokenVerifier):
    """Verify opaque Intervals.icu tokens against the stored mapping + allowlist."""

    def __init__(
        self,
        *,
        storage: AsyncKeyValue,
        allowed_athlete_ids: frozenset[str],
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(required_scopes=required_scopes)
        self.allowed_athlete_ids = allowed_athlete_ids
        self._grants: PydanticAdapter[AthleteGrant] = PydanticAdapter[AthleteGrant](
            key_value=storage,
            pydantic_model=AthleteGrant,
            default_collection="intervals-athlete-grants",
            raise_on_validation_error=False,
        )

    def is_allowed(self, athlete_id: str) -> bool:
        return normalize_athlete_id(athlete_id) in self.allowed_athlete_ids

    async def remember(
        self, token: str, athlete_id: str, athlete_name: str | None, scopes: list[str]
    ) -> None:
        await self._grants.put(
            key=_token_key(token),
            value=AthleteGrant(
                athlete_id=athlete_id,
                athlete_name=athlete_name,
                scopes=scopes,
                created_at=time.time(),
            ),
            ttl=ATHLETE_MAPPING_TTL_SECONDS,
        )

    async def forget(self, token: str) -> None:
        if await self._grants.delete(key=_token_key(token)):
            logger.info("Forgot a revoked Intervals.icu token; the client must log in again")

    async def verify_token(self, token: str) -> AccessToken | None:
        grant = await self._grants.get(key=_token_key(token))
        if grant is None:
            return None
        if not self.is_allowed(grant.athlete_id):
            # Allowlist is re-checked on every request: removing an athlete takes
            # effect immediately, without waiting for tokens to expire.
            logger.warning("Rejected token for athlete %s: not in allowlist", grant.athlete_id)
            return None
        return AccessToken(
            token=token,
            client_id=grant.athlete_id,
            scopes=list(grant.scopes),
            expires_at=None,
            subject=grant.athlete_id,
            claims={
                INTERVALS_ATHLETE_CLAIM: grant.athlete_id,
                "athlete_name": grant.athlete_name,
            },
        )


# --------------------------------------------------------------------------- upstream


class UpstreamTokenError(Exception):
    """Token exchange with Intervals.icu failed. Message is safe to show."""


class AthleteNotAllowedError(UpstreamTokenError):
    def __init__(self, athlete_id: str, athlete_name: str | None) -> None:
        self.athlete_id = athlete_id
        self.athlete_name = athlete_name
        super().__init__(f"Intervals.icu athlete {athlete_id} is not allowed on this server")


#: Set by IntervalsOAuthProvider._handle_idp_callback so the upstream client can
#: report an allowlist rejection back to the callback handler.
_rejections: contextvars.ContextVar[list[AthleteNotAllowedError] | None] = contextvars.ContextVar(
    "intervals_oauth_rejections", default=None
)


async def revoke_upstream_token(token: str) -> None:
    """Best-effort ``DELETE /api/v1/disconnect-app`` for a token we will not use."""
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            await client.delete(
                INTERVALS_DISCONNECT_URL, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError:
        logger.warning("Could not revoke a rejected Intervals.icu token (network error)")


class IntervalsUpstreamClient:
    """Duck-typed replacement for FastMCP's upstream OAuth2 client.

    It speaks Intervals.icu's token endpoint exactly as documented, normalizes the
    response (athlete ID, scope), enforces the allowlist before any MCP token is
    issued, and records the athlete mapping used by IntervalsTokenVerifier.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        verifier: IntervalsTokenVerifier,
        requested_scopes: list[str],
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._verifier = verifier
        self._requested_scopes = requested_scopes
        self._http = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_token(
        self, url: str, *, grant_type: str = "authorization_code", **params: Any
    ) -> dict[str, Any]:
        if grant_type != "authorization_code":
            raise UpstreamTokenError(f"Unsupported grant type for Intervals.icu: {grant_type}")
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": str(params.get("code") or ""),
        }
        if params.get("code_verifier"):
            data["code_verifier"] = str(params["code_verifier"])

        try:
            response = await self._http.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise UpstreamTokenError(f"Could not reach Intervals.icu ({type(e).__name__})") from e

        if response.status_code != 200:
            detail = ""
            try:
                body: Any = response.json()
                error_code: Any = (
                    cast(dict[str, Any], body).get("error") if isinstance(body, dict) else None
                )
                if isinstance(error_code, str):
                    detail = f" ({error_code[:80]})"
            except ValueError:
                pass
            raise UpstreamTokenError(
                f"Intervals.icu token endpoint returned HTTP {response.status_code}{detail}. "
                "Codes expire after about 2 minutes — try connecting again."
            )

        try:
            token: dict[str, Any] = response.json()
        except ValueError as e:
            raise UpstreamTokenError("Intervals.icu token response was not JSON") from e
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise UpstreamTokenError("Intervals.icu token response had no access_token")

        raw_athlete: Any = token.get("athlete")
        athlete: dict[str, Any] = (
            cast(dict[str, Any], raw_athlete) if isinstance(raw_athlete, dict) else {}
        )
        athlete_id = normalize_athlete_id(athlete.get("id"))
        raw_name: Any = athlete.get("name")
        athlete_name = raw_name if isinstance(raw_name, str) else None
        if not athlete_id:
            raise UpstreamTokenError("Intervals.icu token response had no athlete id")

        # Scope arrives comma-separated; FastMCP's parse_scopes splits on commas and
        # spaces. When omitted, Intervals.icu granted what was requested.
        raw_scope = token.get("scope")
        granted = (
            parse_scope_list(raw_scope)
            if isinstance(raw_scope, str) and raw_scope.strip()
            else list(self._requested_scopes)
        )
        token["scope"] = " ".join(granted)
        token.setdefault("token_type", "Bearer")
        token["athlete_id"] = athlete_id

        if not self._verifier.is_allowed(athlete_id):
            logger.warning("OAuth login rejected for athlete %s: not in allowlist", athlete_id)
            await revoke_upstream_token(access_token)
            error = AthleteNotAllowedError(athlete_id, athlete_name)
            sink = _rejections.get()
            if sink is not None:
                sink.append(error)
            raise error

        await self._verifier.remember(access_token, athlete_id, athlete_name, granted)
        logger.info("OAuth login accepted for athlete %s", athlete_id)
        return token

    async def refresh_token(self, url: str, **params: Any) -> dict[str, Any]:
        # Intervals.icu issues no refresh tokens, so FastMCP never calls this.
        raise UpstreamTokenError("Intervals.icu does not support refresh tokens")


# --------------------------------------------------------------------------- provider


def _rejection_page(error: AthleteNotAllowedError) -> HTMLResponse:
    who = html.escape(error.athlete_id)
    if error.athlete_name:
        who = f"{html.escape(error.athlete_name)} ({who})"
    return HTMLResponse(
        create_error_html(
            error_title="Access denied",
            error_message=(
                f"The Intervals.icu athlete {who} is not allowed to use this MCP server. "
                "Ask the server operator to add this athlete ID to ALLOWED_ATHLETE_IDS, "
                "then connect again. The authorization was revoked."
            ),
        ),
        status_code=403,
    )


class IntervalsOAuthProvider(OAuthProxy):
    """FastMCP OAuthProxy configured for Intervals.icu."""

    def __init__(self, settings: RemoteSettings, *, storage: AsyncKeyValue | None = None) -> None:
        validate_oauth_settings(settings)
        self.settings = settings
        self.scopes = effective_scopes(settings)
        signing_key = jwt_signing_key(settings)
        storage = storage or build_storage(settings.data_path, signing_key)
        self.verifier = IntervalsTokenVerifier(
            storage=storage,
            allowed_athlete_ids=parse_allowlist(settings.allowed_athlete_ids),
            required_scopes=self.scopes,
        )
        # An upstream 401 means the athlete revoked or disconnected the app: drop the
        # mapping so the MCP token stops verifying and the client re-authenticates.
        register_bearer_unauthorized_hook(self.verifier.forget)
        base = public_base_url(settings)
        super().__init__(
            upstream_authorization_endpoint=INTERVALS_AUTHORIZE_URL,
            upstream_token_endpoint=INTERVALS_TOKEN_URL,
            upstream_client_id=settings.intervals_oauth_client_id,
            upstream_client_secret=settings.intervals_oauth_client_secret,
            token_verifier=self.verifier,
            base_url=base,
            issuer_url=base,
            redirect_path=REDIRECT_PATH,
            allowed_client_redirect_uris=allowed_client_redirect_uris(settings),
            valid_scopes=self.scopes,
            forward_pkce=True,
            # Intervals.icu ignores RFC 8707 resource indicators; don't send them.
            forward_resource=False,
            token_endpoint_auth_method="client_secret_post",
            # The upstream scope parameter must be comma-separated. Passing it here
            # overrides the space-joined one OAuthProxy would build.
            extra_authorize_params={"scope": format_upstream_scopes(self.scopes)},
            client_storage=storage,
            jwt_signing_key=signing_key,
            require_authorization_consent=True,
        )

    def _create_upstream_oauth_client(self) -> Any:  # documented override point
        return IntervalsUpstreamClient(
            client_id=self.settings.intervals_oauth_client_id,
            client_secret=self.settings.intervals_oauth_client_secret,
            verifier=self.verifier,
            requested_scopes=self.scopes,
        )

    async def _handle_idp_callback(self, request: Request) -> HTMLResponse | RedirectResponse:
        sink: list[AthleteNotAllowedError] = []
        reset = _rejections.set(sink)
        try:
            response: Response = await super()._handle_idp_callback(request)
        finally:
            _rejections.reset(reset)
        if sink:
            return _rejection_page(sink[0])
        return response  # type: ignore[return-value]


def build_oauth_provider(settings: RemoteSettings) -> IntervalsOAuthProvider:
    return IntervalsOAuthProvider(settings)
