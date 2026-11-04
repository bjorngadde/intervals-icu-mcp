# NOTES — remote MCP for a personal training coach

Working notes for the fork work: OAuth, the secret URL, webhooks and the Home Assistant
add-on. Each phase records what was found, what was chosen and why, and what to verify live.
No secrets are written here: every credential is a placeholder.

---

## Phase 0 — Orientation

### Repository layout

| Piece | Where | Notes |
|---|---|---|
| Entry point | `src/intervals_icu_mcp/server.py` | One module-level `FastMCP("intervals_icu_mcp")`. Tools are registered with `mcp.tool(name=…)(fn)`. Destructive tools are gated at import time by `INTERVALS_ICU_DELETE_MODE`. `main()` parses `--transport/--host/--port/--path` and calls `mcp.run(...)`. |
| Config | `auth.py` | `ICUConfig` (pydantic-settings) reads `INTERVALS_ICU_API_KEY`, `INTERVALS_ICU_ATHLETE_ID` and `INTERVALS_ICU_DELETE_MODE` from env or `.env`. |
| Per-call config | `middleware.py` | `ConfigMiddleware.on_call_tool` loads the config **on every tool call**, validates it and puts it into the context state (`ctx.get_state("config")`). |
| HTTP client | `client.py` | `ICUClient(config)` is an async context manager built **per tool call** (`async with ICUClient(config) as client`). It uses HTTP Basic auth (`API_KEY:<key>`), base `https://intervals.icu/api/v1`, a 30 s timeout, and defaults `athlete_id` to `config.intervals_icu_athlete_id` in about 50 methods. |
| Resources/prompts | `server.py` | 4 resources and 9 prompts. `intervals-icu://athlete/profile` loads the config directly (resources skip the tool middleware). |
| Health | `server.py` + `healthcheck.py` | `GET /health` returns `{"status":"ok"}`. Docker `HEALTHCHECK` probes it. |
| Tests | `tests/` | pytest, pytest-asyncio (auto mode), respx for HTTP mocks. Tools are unit-tested with a mocked `ctx.get_state`. `test_transport_integration.py` runs the real server in-memory and over ASGI. 630 tests pass on the base commit. |

**FastMCP version:** the lock pins `fastmcp==4.1.4` (with `mcp==2.3.0` and `starlette==1.10.0`).
`pyproject.toml` says `fastmcp>=3.0.0`. `OAuthProxy` exists in this version and has the hooks the
OAuth phase needs (see Phase 3), so **no upgrade was needed**.

Good news for Phase 1: there is **no global client**. Every tool already builds its client per
call from a per-call config. The only "global" is the athlete ID default inside the config, plus
the resource, which reads the config itself.

### HTTP transport: how the endpoint is mounted

`main()` → `mcp.run(transport="http", host, port, path?)` → FastMCP `http_app()` →
`create_streamable_http_app(server, streamable_http_path=path or "/mcp", auth=mcp.auth)`.
That creates a Starlette app with:

- a `Route(path)` to a `StreamableHTTPASGIApp`. When `auth` is set, the route is wrapped in
  `RequireAuthMiddleware`, which returns 401 plus `WWW-Authenticate: Bearer resource_metadata=…`;
- the auth provider's own routes, when `auth` is set (`/authorize`, `/token`, `/register`,
  `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`, the
  upstream callback and `/consent`);
- the custom routes (`@mcp.custom_route`), at the lowest precedence;
- a lifespan that starts the streamable-HTTP session manager.

The default endpoint path is `/mcp`. Uvicorn serves the app.

### Tools, grouped by area (73 in `full` mode, 70 by default)

| Area | Tools |
|---|---|
| Activities | `icu_get_recent_activities`, `icu_get_activities_by_date`, `icu_get_activity_details`, `icu_search_activities`, `icu_search_activities_full`, `icu_get_activities_around`, `icu_update_activity`, `icu_update_activity_streams`, `icu_bulk_create_manual_activities`, `icu_delete_activity`*, `icu_download_activity_file`, `icu_download_fit_file`, `icu_download_gpx_file` |
| Activity analysis | `icu_get_activity_streams`, `icu_get_stream_stats`, `icu_get_activity_intervals`, `icu_get_best_efforts`, `icu_search_intervals`, `icu_get_power_histogram`, `icu_get_hr_histogram`, `icu_get_pace_histogram`, `icu_get_gap_histogram` |
| Activity messages | `icu_get_activity_messages`, `icu_add_activity_message`, `icu_update_activity_message`, `icu_delete_activity_message` |
| Athlete / fitness | `icu_list_athletes`, `icu_get_athlete_profile`, `icu_update_athlete_notes`, `icu_get_fitness_summary`, `icu_get_fitness_chart` |
| Wellness | `icu_get_wellness_data`, `icu_get_wellness_for_date`, `icu_update_wellness` |
| Calendar (read) | `icu_get_calendar_events`, `icu_get_upcoming_workouts`, `icu_get_event`, `icu_get_seasons`, `icu_get_annual_training_plan` |
| Calendar (write) | `icu_create_event`, `icu_update_event`, `icu_delete_event`, `icu_bulk_create_events`, `icu_bulk_update_event_access`, `icu_bulk_delete_events`, `icu_duplicate_events`, `icu_apply_training_plan` |
| Curves | `icu_get_power_curves`, `icu_get_hr_curves`, `icu_get_pace_curves` |
| Workout library | `icu_get_workout_library`, `icu_get_workouts_in_folder`, `icu_create_workout`, `icu_update_workout`, `icu_delete_workout`, `icu_bulk_create_workouts`, `icu_create_workout_folder`, `icu_delete_workout_folder` |
| Gear | `icu_get_gear_list`, `icu_create_gear`, `icu_update_gear`, `icu_delete_gear`*, `icu_create_gear_reminder`, `icu_update_gear_reminder` |
| Sport settings | `icu_get_sport_settings`, `icu_update_sport_settings`, `icu_create_sport_settings`, `icu_delete_sport_settings` |
| Custom items | `icu_get_custom_items`, `icu_get_custom_item`, `icu_create_custom_item`, `icu_update_custom_item`, `icu_delete_custom_item`* |
| Webhook events (new, Phase 4) | `icu_get_recent_events`, registered only when the webhook receiver is configured |

\* only in `INTERVALS_ICU_DELETE_MODE=full`. The `safe` default also hides nothing else;
`none` hides every delete tool.

### Can the coach write a complete planned workout?

What a planned workout needs, and what the API field is (from `openapi-spec.json` → `EventEx`):

| Need | API field | `icu_create_event` / `icu_update_event` before | After this branch |
|---|---|---|---|
| Workout text in Intervals.icu syntax | `description` | ✅ | ✅ |
| Category | `category=WORKOUT` | ✅ | ✅ |
| Sport type | `type` (`event_type` param) | ✅ | ✅ |
| Date | `start_date_local` | ✅ | ✅ |
| Name | `name` | ✅ | ✅ |
| Tags | `tags` | ✅ | ✅ |
| **Indoor flag** | `indoor` | ❌ missing | ✅ `indoor` param |
| **Marker for events the coach created** | `external_id` | ❌ missing (read-only via `icu_get_event`) | ✅ `external_id` param |

`icu_bulk_create_events` already passed unknown keys through, so `indoor` and `external_id`
worked there. Only its parameter description was updated to mention them.

**Change made (smallest that covers it):** added the optional `indoor` and `external_id`
parameters to `icu_create_event` and `icu_update_event`, added `indoor` to the `Event` model, and
echoed both fields in event responses. All of this is additive, so it is a SemVer minor change.

Suggested convention for the coach: `tags=["ai-coach"]` **and**
`external_id="ai-coach-<date>-<slug>"`. Tags are visible in the UI. `external_id` survives tag
edits, and in OAuth mode it lets `POST /events/bulk?upsert=true` update events "created by the
same OAuth application" in place. That upsert parameter is **not** wired into the tool yet; it is
a possible follow-up.

### Live check (API key mode)

**Not possible from this environment.** Its network policy blocks `intervals.icu`: the egress
proxy answers `403` to `CONNECT intervals.icu:443`. As the brief asks, there was no workaround.
Everything below is mocked in tests. Björn should run this himself:

```bash
export INTERVALS_ICU_API_KEY=…  INTERVALS_ICU_ATHLETE_ID=i…
uv run python - <<'PY'
import asyncio, datetime as dt
from intervals_icu_mcp.auth import load_config
from intervals_icu_mcp.client import ICUClient
async def main():
    cfg = load_config(); today = dt.date.today(); week = today - dt.timedelta(days=7)
    async with ICUClient(cfg) as c:
        acts = await c.get_activities(oldest=week.isoformat(), newest=today.isoformat())
        for a in acts: print(a.start_date_local, a.type, a.name, getattr(a, "source", None))
        for w in await c.get_wellness(oldest=week.isoformat(), newest=today.isoformat()):
            print(w.id, w.hrv, w.resting_hr, w.sleep_secs)
asyncio.run(main())
PY
```

Look at whether activities whose `source` is `GARMIN_CONNECT` come back with full data. If they
come back as stubs (the way Strava ones do: `_note` / missing streams), Intervals.icu has started
blocking Garmin data in the API. Also check that the wellness rows have HRV and sleep values.

---

## Phase 1 — Per-request client

**What changed**

- `ICUConfig` gained two *private* attributes: `_bearer_token` and `_token_athlete_id`.
  pydantic-settings never fills private attributes from env or `.env`, so a bearer token can only
  come from an authenticated request. It never comes from process-wide config.
- `ICUConfig.with_bearer_token(token, athlete_id)` returns a **copy** that:
  - drops the API key;
  - sets `intervals_icu_athlete_id="0"` (the token owner);
  - keeps the non-credential settings (delete mode).
  The original config is untouched.
- `ICUClient` sends `Authorization: Bearer …` when the config carries a token, and HTTP Basic
  otherwise. In bearer mode, 401 and 403 now produce OAuth-specific messages ("reconnect", "missing
  scope").
- `middleware.resolve_request_config()` runs on every tool call, and also for the athlete-profile
  resource. If the request was authenticated by the Intervals.icu OAuth provider (the
  `AccessToken` carries the `intervals_icu_athlete_id` claim), it returns the bearer-mode config.
  Otherwise it returns the API-key config, exactly as before. A token *without* that claim (some
  other auth provider) stays in API-key mode.
- `normalize_athlete_id()` maps `784427`, `i784427` and `I784427` all to `i784427`.
- `icu_list_athletes` and `icu_get_fitness_summary` show the token owner's real ID instead of
  `"0"` (`config.display_athlete_id`).
- `intervals_icu_api_key` is now `repr=False`, so it no longer shows up in a config repr.

**Decision: athlete `0` rather than the token's athlete ID.** This follows the brief, and it is
the safest choice: the API resolves `0` server-side from the token, so no ID normalization can go
wrong. An explicit `athlete_id` parameter on a tool is still passed through. Intervals.icu then
enforces what the token may access.

**Compatibility:** in stdio and API-key mode nothing changes. 630 of the existing tests pass
unchanged. `tests/test_auth_modes.py` adds 23 tests that cover both client modes, the copy
semantics, the env-isolation of the token, middleware switching between consecutive calls, and the
error messages.

---

## Phase 2 — Secret-path mode

`MCP_PATH_SECRET` is optional. When it is set (HTTP transport only):

- `/<secret>/mcp` (and `/<secret>/mcp/`) serves the MCP endpoint in **API-key mode**.
- `/healthz` and `/health` always return `{"status":"ok"}`. `/health` is kept because the Docker
  `HEALTHCHECK` and `healthcheck.py` probe it.
- `/webhooks/intervals` is reachable when the webhook receiver is configured (Phase 4). The OAuth
  endpoints and the OAuth-protected `/mcp` are reachable when OAuth is configured (Phase 3).
- **Every other path returns 404**, including `/mcp` when OAuth is off and the internal path.

**How it is enforced (in the app, not only in NGINX).** `remote.PathSecretGate` is the outermost
ASGI wrapper:

- It compares the first path segment with `hmac.compare_digest`. It does a comparison even when
  the path has the wrong shape, so timing does not reveal the shape.
- On a match it rewrites `scope["path"]` **in place** to `/__secret__/mcp`. Uvicorn's access log
  reads the same scope dict, so the log shows `/__secret__/mcp` and never the secret.
- The allowed public paths are taken from the app's actual routes, so nothing is listed twice.

**Validation.** The secret must be at least 32 characters and use only `[A-Za-z0-9._~-]`, so
that it is one URL segment. Otherwise the server exits with code 2 before it binds the port.
Error messages give the length, never the value.

**Log masking, belt and braces.** Uvicorn runs with a log config whose handlers all carry
`SecretRedactingFilter`. The filter replaces the path secret, the OAuth client secret, the JWT key
and the webhook secret with `***` in any record. `RemoteSettings.__repr__` also masks every value.
A real uvicorn smoke run confirmed this: the secret path returned 200, `/mcp` returned 404, and
the secret appeared zero times in the log.

**Decisions**

- *Which credentials does the secret path use?* The operator's API key (API-key mode). Having the
  URL is the authentication. That is the point of using it before OAuth is approved.
- *Remote features need `--transport http`.* With `sse` they cause a startup error; with stdio
  they are ignored. `--path` together with `MCP_PATH_SECRET` is a startup error.
- *Unset secret means unchanged behaviour.* When no remote setting is present, `main()` still
  calls `mcp.run(transport=…)` exactly as before (there is a test for this). `/healthz` is the only
  addition there.
- *Runner.* In remote mode the server uses `uvicorn.run(...)` with FastMCP's defaults
  (`timeout_graceful_shutdown=2`, `lifespan=on`, `ws=websockets-sansio`) plus the redacting log
  config and `proxy_headers=True`. Uvicorn trusts `X-Forwarded-*` only from `FORWARDED_ALLOW_IPS`
  (env, default `127.0.0.1`). The add-on sets it to the hassio network.
- *Alternatives considered:* (a) NGINX-only enforcement. Rejected by the brief, and it would leave
  the LAN port open. (b) A Starlette route literally named `/<secret>/mcp`. Rejected: Starlette's
  router matches with a regex, which is not constant-time, and FastMCP logs the mount path at
  startup.

---

## Phase 3 — OAuth via Intervals.icu

**Built:** `src/intervals_icu_mcp/oauth.py` defines `IntervalsOAuthProvider`, a subclass of
FastMCP 4.1.4's `OAuthProxy`. It is turned on when `INTERVALS_OAUTH_CLIENT_ID` or
`INTERVALS_OAUTH_CLIENT_SECRET` is set. It works on its own or alongside the secret path: then
`/mcp` uses OAuth and `/<secret>/mcp` uses the API key, both served by the same session manager.
User documentation: `docs/oauth.md`, plus a short README section.

**FastMCP:** no upgrade was needed. 4.1.4 already provides `OAuthProxy` with DCR, PKCE, CIMD, a
consent screen, encrypted storage and these override points:

- `_create_upstream_oauth_client`, documented as "Subclasses can override";
- `extra_authorize_params`, `token_endpoint_auth_method` and `forward_resource`;
- `_handle_idp_callback`, a thin wrapper.

The `pyproject.toml` floor went from `fastmcp>=3.0.0` to `>=4.1.4`, because those hooks are
version-specific. Only the lock metadata changed.

### Decisions

| Topic | Choice | Why / alternatives |
|---|---|---|
| Token verification | **Stored mapping.** At code exchange the server stores `sha256(upstream token) → {athlete_id, name, scopes}` in the encrypted store. `verify_token` checks it and the *current* allowlist on every request. | `GET /athlete/0` on every request would add latency and an Intervals.icu rate-limit cost to every MCP message, and it needs `SETTINGS:READ`. A cache would hide revocations anyway. Revocation still surfaces: the next API call returns 401 with a "reconnect" message. Removing an athlete from the allowlist takes effect immediately. |
| Upstream token client | Own `IntervalsUpstreamClient` (httpx) in place of FastMCP's generic one. | It sends **exactly** the documented form fields (`client_id`, `client_secret`, `code`, `code_verifier`) and nothing else (no `grant_type`, `redirect_uri` or `scope`). It normalises the athlete ID and scope, enforces the allowlist *before* any MCP token is issued, records the mapping, and is mockable with respx. |
| Scope formatting | Upstream: `scope=A,B,C` through `extra_authorize_params`, which overrides OAuthProxy's space-joined value. Towards MCP: space-separated. A response `scope` with commas is parsed; a missing one falls back to the requested scopes. | Intervals.icu uses commas; MCP and OAuth clients use spaces. |
| Default scopes | `ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:READ`, **derived in code** from `COACH_TOOLS` via `SCOPE_BY_TOOL`. A test makes sure every registered tool has a mapping. | It is the minimal set for the daily coach job, and it matches the brief's example. `SETTINGS:READ` is needed for `athlete/0` (profile, zones). Library and other writes are left out. |
| Required scopes | The configured scopes are the verifier's `required_scopes`, so the token must carry all of them; otherwise MCP returns 403 `insufficient_scope`. | This fails loudly when a user unticks a scope, instead of half-working. |
| Allowlist rejection | At the callback: a 403 HTML page that names the athlete and `ALLOWED_ATHLETE_IDS`, and a best-effort `DELETE /api/v1/disconnect-app` with the fresh token. The MCP client gets no code. | It is a clear error at the moment the human is looking, and no orphaned grant is left at Intervals.icu. |
| Persistence | A FileTreeStore wrapped in Fernet encryption under `DATA_DIR/oauth`, directory mode 0700. The signing and encryption keys are HKDF-derived from the client secret (FastMCP's default scheme), or from `OAUTH_JWT_SIGNING_KEY` when set. | Both encryption and restrictive permissions, as the brief asked. Honest caveat: on the add-on the client secret is also on `/data` (in `options.json`), so the encryption mostly protects backups and copies that leave out `options.json`. The 0700 permissions are the real local barrier. |
| Consent screen | FastMCP's consent page stays **on** (`require_authorization_consent=True`). | It protects against confused-deputy / AS-in-the-middle attacks for a proxy that does DCR. It costs one extra click per login. |
| MCP-client redirect URIs | Defaults to Claude's callbacks (`claude.ai` and `claude.com`) plus loopback. `OAUTH_ALLOWED_CLIENT_REDIRECT_URIS` overrides it; `*` allows any. | Least privilege: the only real client is Claude. Claude Code and MCP Inspector use loopback. |
| Resource indicator | `forward_resource=False`. | Intervals.icu does not document RFC 8707. Sending an unknown `resource` parameter is a needless risk. |
| Public URLs | All of them come from `PUBLIC_BASE_URL` (https is required except for localhost). `X-Forwarded-*` is honoured by uvicorn (`proxy_headers`, `FORWARDED_ALLOW_IPS`) for client IP and scheme, but it can never change the issuer or callback URLs (there is a test). | TLS terminates at NGINX. Building URLs from headers would let a spoofed `Host` change the issuer. |
| Token lifetime | FastMCP's default for upstream tokens with no expiry: one year. There is no refresh token. | Intervals.icu tokens never expire. To revoke, use the allowlist, Intervals.icu "disconnect", or rotate the client secret. |

### Differences between the brief and the code/docs (flagged)

- The brief lists the token-endpoint fields as `client_id, client_secret, code, code_verifier`.
  That is implemented literally. If Intervals.icu also expects `grant_type=authorization_code` or
  `redirect_uri` (standard RFC 6749), the token exchange will return a 400; it is a one-line
  change in `IntervalsUpstreamClient.fetch_token`. **Live check.**
- The brief says `X:WRITE` covers `X:READ` (the default has `CALENDAR:WRITE` and no
  `CALENDAR:READ`). The OpenAPI spec has no scope information to confirm this. **Live check:**
  `icu_get_calendar_events` works with the default scopes.

### Tests (`tests/test_oauth.py`, 28 tests, Intervals.icu mocked with respx)

- Full proxy flow, as Claude does it: DCR, authorize with PKCE, consent with CSRF and cookie,
  the upstream authorize URL (exact callback, comma scopes, S256, no `resource`), callback, token
  exchange (exact form fields), `/token` with PKCE, MCP `initialize`, and a tool call. The tool
  call reaches `GET /athlete/0` with `Bearer <upstream token>`.
- Allowlist: accept; reject with a 403 page and revocation; empty list rejects everyone; a bare
  numeric ID matches; the allowlist is re-checked after a restart.
- Persistence: a token issued before a "restart" (new server, provider and app on the same
  `DATA_DIR`) still works, and the DCR client is still known. A rotated client secret invalidates
  tokens. Files on disk never contain the token or the athlete ID, and the directory is 0700.
- 401 with `WWW-Authenticate` / `resource_metadata` for missing and invalid tokens; both metadata
  documents; `X-Forwarded-Host` cannot change the URLs; foreign redirect URIs are rejected at DCR.
- Scope parsing and formatting, the default-scope derivation, and the tool-to-scope coverage.
- Nothing secret is logged, with FastMCP's non-propagating logger captured at DEBUG.
- OAuth and the secret path side by side.

---

## Phase 4 — Webhook receiver

**Built:** `src/intervals_icu_mcp/webhooks.py` (`POST /webhooks/intervals`) and
`tools/webhook_events.py` (`icu_get_recent_events`). Both are turned on by
`INTERVALS_WEBHOOK_SECRET` (at least 16 characters). Without it the route does not exist (404)
and the tool is not registered, so the default tool count stays 70.

| Topic | Choice | Why / alternatives |
|---|---|---|
| Authentication | `Authorization` header equals the secret; `Bearer <secret>` is also accepted. A `secret` field in the JSON body is accepted too. All comparisons run in constant time, and a mismatch returns 401. | **Flag:** the brief says the secret arrives in the Authorization header ("Webhook Authorization Header" on the app page). The Intervals.icu forum API docs say the webhook POST body carries a `secret`. Accepting either keeps it working whichever is true. The body secret is never stored. **Live check:** look at a real delivery. |
| Order of checks | Size limit (1 MiB → 413), then authentication, then JSON validity (400). | An unauthenticated caller learns nothing about parsing. |
| Payload shape | `{"events": [...]}` (documented), a bare list, or a single event. | The format is not in the OpenAPI spec, so the parser is tolerant. Unknown types are stored with `known_type=false` and logged at info level; they never fail the delivery. |
| What is stored | One JSONL line per event: `received_at`, `timestamp`, `type`, `known_type`, `athlete_id` (normalised), and `ids` (IDs found in `activity`, `records`, `*_id` and `*_ids`). No payload contents. | Keeps personal data out of the log and keeps lines small. |
| Rotation | At `WEBHOOK_LOG_MAX_BYTES` (default 1 MiB) the file moves to `.1`, keeping one backup. The file mode is 0600. | Simple and bounded: at most about 2 MiB. Reads merge the backup and the live file. |
| Allowlist | When `ALLOWED_ATHLETE_IDS` is set, events for other athletes are dropped. | Only the athletes the operator serves are stored. |
| Tool | `icu_get_recent_events(since, event_types, limit, athlete_id)`. `since` defaults to 24 h ago. It returns the newest events first. In OAuth (bearer) mode it is always filtered to the caller's athlete. | The brief's `get_recent_events(since)`, with the repository's `icu_` prefix. |
| Home Assistant hook | `HA_WEBHOOK_URL` (off by default). A background POST of `{"source":"intervals.icu","events":[summaries]}` with a 10 s timeout. Failures are logged without the URL, and the URL is in the redaction list because it contains the HA webhook ID. | The server does not try to trigger Claude. HA can, for example, notify Björn or start an automation. |

**Which types to tick on the app page:** `ACTIVITY_UPLOADED`, `WELLNESS_UPDATED`,
`CALENDAR_UPDATED` and `SPORT_SETTINGS_UPDATED`. `ACTIVITY_ANALYZED` is optional; Intervals.icu
sends it about 60 s later, once analysis is done. The legacy `CALENDAR_EVENT_*` types are not
needed.

**Tests** (`tests/test_webhooks.py`, 35 tests): the header, Bearer and body-secret variants; bad
or missing secrets (401, nothing stored); invalid JSON (400 only after authentication); 413; GET
returns 405; the four required types with ID extraction and no payload leakage; file mode 0600;
unknown types stored and logged; allowlist filtering; rotation; the HA hook forwards when
configured, stays off by default, does not fail the delivery when HA is down, and does not leak
its URL; the tool's window, type and athlete filters; wiring behind the secret-path gate; and the
tool registered only with the secret (subprocess check: 70 vs 71 tools).
