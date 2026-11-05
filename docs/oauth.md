# OAuth and secret URL

How to run the server as a **remote MCP server** that Claude (web, mobile, desktop and scheduled
tasks) can use as a *custom connector*, with authentication built in. Everything here is
**opt-in**: without these environment variables the server behaves exactly as described in
[remote-deployment.md](remote-deployment.md).

| Mode | What protects `/mcp` | Intervals.icu credentials used | Needs |
|---|---|---|---|
| **OAuth** | OAuth 2.1 (PKCE, DCR); login at Intervals.icu | the logged-in athlete's own OAuth token (bearer, athlete `0`) | an approved Intervals.icu OAuth app |
| **Secret URL** | an unguessable path: `/<secret>/mcp` | the operator's API key | nothing |
| both | `/mcp` uses OAuth, `/<secret>/mcp` uses the API key | as above, per endpoint | — |

All of these modes need the streamable-HTTP transport (`--transport http`). Run the server
behind a TLS-terminating reverse proxy.

## Configuration

| Variable | Mode | Meaning |
|---|---|---|
| `MCP_PATH_SECRET` | secret URL | At least 32 characters from `[A-Za-z0-9._~-]`. MCP is served **only** at `/<secret>/mcp`; every other path returns 404, except the health and OAuth routes. A shorter secret stops the server at startup. |
| `INTERVALS_OAUTH_CLIENT_ID` | OAuth | From Intervals.icu → Settings → Apps → *Manage App*. |
| `INTERVALS_OAUTH_CLIENT_SECRET` | OAuth | Same page. Also the root of the token-signing and storage keys, unless `OAUTH_JWT_SIGNING_KEY` is set. Rotating it invalidates every issued token. |
| `PUBLIC_BASE_URL` | OAuth | The public https URL, for example `https://mcp.example.com`. The issuer, metadata and callback URLs are built from it, never from request headers. |
| `ALLOWED_ATHLETE_IDS` | OAuth | Comma-separated athlete IDs. `i123456` and `123456` both work. **Empty rejects everyone.** It is re-checked on every request. |
| `OAUTH_SCOPES` | OAuth | Comma-separated. Default `ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:READ`; see the table below. |
| `DATA_DIR` | OAuth | Persistent directory (default `./data`). OAuth state lives in `DATA_DIR/oauth` (encrypted, mode 0700). |
| `OAUTH_JWT_SIGNING_KEY` | OAuth (optional) | Separate key for issued tokens and storage encryption. |
| `OAUTH_ALLOWED_CLIENT_REDIRECT_URIS` | OAuth (optional) | MCP-client callbacks accepted at registration. The default is Claude (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`) plus loopback (`http://localhost:*`, `http://127.0.0.1:*`). `*` allows any. |
| `FORWARDED_ALLOW_IPS` | all | Uvicorn's setting: the proxies whose `X-Forwarded-*` headers are trusted (default `127.0.0.1`). |

Generate secrets with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Never put real values in a committed file. The server never logs them: a filter on every log
handler replaces them with `***`, and a matched secret path shows up in the access log as
`/__secret__/mcp`.

## OAuth setup

1. **Create an OAuth app in Intervals.icu** (Settings → Apps). Set its redirect URI to exactly
   `<PUBLIC_BASE_URL>/auth/callback`. Choose the scopes you will configure in `OAUTH_SCOPES`.
   Optionally tick *Require PKCE*: the server always uses PKCE (S256) towards Intervals.icu.
2. **Start the server:**

   ```bash
   INTERVALS_OAUTH_CLIENT_ID=<client-id> \
   INTERVALS_OAUTH_CLIENT_SECRET=<client-secret> \
   PUBLIC_BASE_URL=https://mcp.example.com \
   ALLOWED_ATHLETE_IDS=i123456 \
   DATA_DIR=/var/lib/intervals-icu-mcp \
   intervals-icu-mcp --transport http --host 0.0.0.0 --port 8000
   ```

3. **Add it to Claude:** Settings → Connectors → *Add custom connector*, URL
   `<PUBLIC_BASE_URL>/mcp`. Claude discovers the metadata and registers itself through DCR. It
   then opens a consent page served by this server, and after that the Intervals.icu login.
   Connectors added on claude.ai are also available in the mobile apps and in scheduled tasks.

### What happens during a login

```
Claude ──/register (DCR)──▶ server
Claude ──/authorize (PKCE)─▶ server ──consent page──▶ user approves
server ──302──▶ intervals.icu/oauth/authorize?client_id&redirect_uri&scope=A,B&state&code_challenge(S256)
intervals.icu ──302──▶ server /auth/callback?code&state
server ──POST /api/oauth/token {client_id, client_secret, code, code_verifier}──▶ intervals.icu
        ◀── {access_token, scope, athlete:{id,name}}
        athlete not in ALLOWED_ATHLETE_IDS → 403 page + token revoked (DELETE /api/v1/disconnect-app)
server ──302──▶ Claude callback ?code&state ; Claude ──/token (PKCE)──▶ server JWT
Claude ──/mcp  Authorization: Bearer <server JWT>──▶ tools run with the athlete's own token
```

- Unauthenticated requests to `/mcp` get **401** with
  `WWW-Authenticate: Bearer … resource_metadata="<base>/.well-known/oauth-protected-resource/mcp"`.
- Metadata is served at `/.well-known/oauth-protected-resource/mcp` and
  `/.well-known/oauth-authorization-server`.
- Tools run in **bearer mode**: `Authorization: Bearer <intervals token>` with athlete `0` (the
  token owner). The operator's API key is never used for an OAuth request.
- Intervals.icu tokens do not expire and have no refresh token. The server's own tokens last
  about a year (FastMCP's default when the upstream has no expiry). To revoke access, disconnect
  the app in Intervals.icu, remove the athlete from `ALLOWED_ATHLETE_IDS`, or rotate the client
  secret.

### Token verification and storage

Intervals.icu tokens are opaque, so there is no JWKS to verify them with. During the token
exchange the server stores `sha256(token) → athlete ID, scopes`. Every request is then checked
against that mapping and the current allowlist. This needs no extra API call per request and does
not depend on `SETTINGS:READ`. If a token is revoked at Intervals.icu, the next tool call gets a
401 from the API with a "reconnect the connector" message.

DCR clients, pending authorizations, issued-token mappings, the upstream tokens and the athlete
mapping are kept in a Fernet-encrypted file store under `DATA_DIR/oauth` (directory mode 0700).
They survive restarts.

### Scopes per tool

Scopes are comma-separated towards Intervals.icu and space-separated towards MCP clients (the
OAuth standard). `X:WRITE` is treated as including `X:READ`. The default set covers a training
coach: it reads activities, wellness, fitness, the calendar, the profile and zones, and writes
planned workouts. Add `LIBRARY:READ` or `*:WRITE` scopes only if you need those tools. A tool
whose scope was not granted fails with a "lacks the scope" error.

| Scope | Tools |
|---|---|
| `ACTIVITY:READ` | `icu_get_recent_activities`, `icu_get_activities_by_date`, `icu_get_activity_details`, `icu_search_activities`, `icu_search_activities_full`, `icu_get_activities_around`, `icu_download_activity_file`, `icu_download_fit_file`, `icu_download_gpx_file`, `icu_get_activity_streams`, `icu_get_stream_stats`, `icu_get_activity_intervals`, `icu_get_best_efforts`, `icu_search_intervals`, `icu_get_power_histogram`, `icu_get_hr_histogram`, `icu_get_pace_histogram`, `icu_get_gap_histogram`, `icu_get_activity_messages`, `icu_get_power_curves`, `icu_get_hr_curves`, `icu_get_pace_curves` |
| `ACTIVITY:WRITE` | `icu_update_activity`, `icu_update_activity_streams`, `icu_bulk_create_manual_activities`, `icu_delete_activity`, `icu_add_activity_message`, `icu_update_activity_message`, `icu_delete_activity_message` |
| `WELLNESS:READ` | `icu_get_fitness_summary`, `icu_get_fitness_chart`, `icu_get_wellness_data`, `icu_get_wellness_for_date` |
| `WELLNESS:WRITE` | `icu_update_wellness` |
| `CALENDAR:READ` | `icu_get_calendar_events`, `icu_get_upcoming_workouts`, `icu_get_event`, `icu_get_seasons`, `icu_get_annual_training_plan` |
| `CALENDAR:WRITE` | `icu_create_event`, `icu_update_event`, `icu_delete_event`, `icu_bulk_create_events`, `icu_bulk_update_event_access`, `icu_bulk_delete_events`, `icu_duplicate_events`, `icu_apply_training_plan` |
| `LIBRARY:READ` | `icu_get_workout_library`, `icu_get_workouts_in_folder` |
| `LIBRARY:WRITE` | `icu_create_workout`, `icu_update_workout`, `icu_delete_workout`, `icu_bulk_create_workouts`, `icu_create_workout_folder`, `icu_delete_workout_folder` |
| `SETTINGS:READ` | `icu_get_athlete_profile`, `icu_get_gear_list`, `icu_get_sport_settings`, `icu_get_custom_items`, `icu_get_custom_item` |
| `SETTINGS:WRITE` | `icu_update_athlete_notes`, `icu_create_gear`, `icu_update_gear`, `icu_delete_gear`, `icu_create_gear_reminder`, `icu_update_gear_reminder`, `icu_update_sport_settings`, `icu_create_sport_settings`, `icu_delete_sport_settings`, `icu_create_custom_item`, `icu_update_custom_item`, `icu_delete_custom_item` |
| `— (not available with OAuth)` | `icu_list_athletes` |

`icu_list_athletes` calls `GET /athletes`, which needs an API key. Use it through the secret URL
or stdio.

## Secret URL setup

```bash
MCP_PATH_SECRET=<43-char token from the command above> \
INTERVALS_ICU_API_KEY=<api key> INTERVALS_ICU_ATHLETE_ID=i123456 \
intervals-icu-mcp --transport http --host 0.0.0.0 --port 8000
```

In Claude, add the custom connector with URL `https://mcp.example.com/<secret>/mcp` and no
authentication. Anyone who has the URL can use every tool with your API key, so treat it like a
password. To rotate it, change `MCP_PATH_SECRET` and update the connector.
