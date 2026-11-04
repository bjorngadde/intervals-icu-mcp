# Draft issue for hhopke/intervals-icu-mcp

> **Not posted.** Draft for Björn to review and post himself if he wants to.

---

**Title:** Proposal: built-in auth for remote HTTP use (per-request client, secret path, opt-in Intervals.icu OAuth)

Hi! Thanks for this server. I've been running it remotely as a Claude **custom connector**
(claude.ai web/mobile and scheduled tasks), and to do that safely I added built-in
authentication. All of it is opt-in, and stdio/API-key behaviour is unchanged. Would you be
interested in PRs for any of these? I'm happy to split them up and adjust them to your
conventions.

1. **Per-request client (bearer mode).** `ICUConfig` can carry a per-request OAuth access token
   (a private attribute, never read from env). `ICUClient` then sends
   `Authorization: Bearer …` with athlete `0` (the token owner) and drops the API key for that
   request. `ConfigMiddleware` picks the mode per call from the authenticated `AccessToken`.
   Tools are unchanged because they already build their client per call. About 70 lines plus
   tests.

2. **Optional secret path (`MCP_PATH_SECRET`).** When set (at least 32 URL-safe characters,
   otherwise startup fails), MCP is served only at `/<secret>/mcp` and every other path returns
   404, except health. It is enforced in the app with a constant-time comparison. The matched
   path is rewritten to a masked path before uvicorn's access log sees it, and a log filter
   redacts configured secrets. It also adds an unauthenticated `/healthz`. It is a simple option
   for people without an OAuth app, safer than an open `--transport http`.

3. **Opt-in Intervals.icu OAuth.** This is a small subclass of FastMCP's `OAuthProxy`:
   - DCR and PKCE for MCP clients, with PKCE S256 upstream;
   - comma-separated upstream scopes, with a documented tool → scope map and a minimal default
     set;
   - an athlete allowlist, checked at the callback (a clear 403 and the token revoked) and on
     every request;
   - opaque-token verification through an encrypted stored mapping, so there is no extra API
     call per request;
   - state persisted under `DATA_DIR`.
   Configured with `INTERVALS_OAUTH_CLIENT_ID`/`_SECRET`, `PUBLIC_BASE_URL`,
   `ALLOWED_ATHLETE_IDS`, `OAUTH_SCOPES` and `DATA_DIR`. It is tested end to end against a mocked
   Intervals.icu (DCR → consent → upstream → callback → token → tool call).

There are two smaller items that could go in separately:
- `indoor` and `external_id` parameters on `icu_create_event` / `icu_update_event` (additive);
- an optional webhook receiver (`POST /webhooks/intervals` plus a read-only
  `icu_get_recent_events` tool), registered only when configured.

Notes: (3) would raise the `fastmcp` floor to 4.1.4, because it uses `OAuthProxy`'s
upstream-client hook. Nothing changes which tools register by default.

Would PRs for 1 and 2 be welcome first, with 3 after that if you like the direction?
