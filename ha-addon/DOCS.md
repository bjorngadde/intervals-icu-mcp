# Intervals.icu MCP — Home Assistant add-on

This add-on runs the [Intervals.icu MCP server](https://github.com/bjorngadde/intervals-icu-mcp)
as a **remote MCP server** that Claude (web, mobile, desktop and scheduled tasks) can use as a
*custom connector*. It listens on port **8099** and is meant to sit behind the
**NGINX Home Assistant SSL proxy** add-on, which provides TLS.

The add-on **refuses to start** unless at least one authentication mode is configured:

| Mode | Claude connector URL | Uses | Needs |
|---|---|---|---|
| **Secret URL** | `https://<host>/<path_secret>/mcp` | your API key | `api_key`, `athlete_id`, `path_secret` |
| **OAuth** | `https://<host>/mcp` | each athlete's own OAuth token | an approved Intervals.icu OAuth app |

The two modes can run together. Use the secret URL until your OAuth app is approved, then
switch Claude over to OAuth.

## Generating strong secrets

Run one of these on any machine. Each prints a 43-character URL-safe secret:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```bash
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
```

Use separate values for `path_secret` and `webhook_secret`. Store them in a password manager.
The add-on never logs them: the secret path shows up in logs as `/__secret__/mcp`.

## Options

| Option | Mode | Description |
|---|---|---|
| `api_key` | secret URL | Intervals.icu → Settings → *Developer Settings* → API key. |
| `athlete_id` | secret URL | Your athlete ID, for example `i123456`. |
| `path_secret` | secret URL | At least 32 characters from `A–Z a–z 0–9 . _ ~ -`. A shorter value stops the add-on. |
| `oauth_client_id` | OAuth | Intervals.icu → Settings → Apps → *Manage App*. |
| `oauth_client_secret` | OAuth | Same page. Changing it logs every connected client out. |
| `public_base_url` | OAuth | The public URL NGINX serves, for example `https://intervals.example.com`. |
| `allowed_athlete_ids` | OAuth | Comma-separated athlete IDs that may log in. **Empty rejects everyone.** |
| `scopes` | OAuth | Default `ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:READ`. It must match what the app allows. |
| `webhook_secret` | webhooks | At least 16 characters. Turns on `POST /webhooks/intervals`. |
| `ha_webhook_url` | webhooks | Optional Home Assistant webhook to forward each event to. |
| `log_level` | all | `debug`, `info`, `warning` or `error`. |

State (OAuth clients and tokens, encrypted, mode 0700; the webhook event log, mode 0600) is kept
in the add-on's `/data` directory. It is included in Home Assistant backups.

## Setup — secret URL mode (works today)

1. Set `api_key`, `athlete_id` and a generated `path_secret`. Start the add-on and check the log
   for `modes=secret-path`.
2. Set up NGINX (see below).
3. In Claude, go to **Settings → Connectors → Add custom connector**. Name it "Intervals.icu",
   enter the URL `https://<host>/<path_secret>/mcp` and leave authentication empty.

Anyone who has that URL can use your Intervals.icu account through the tools. To rotate it, change
`path_secret`, restart the add-on and update the connector.

## Setup — OAuth mode

1. In Intervals.icu → Settings → Apps → *Manage App*:
   - **Redirect URI:** `https://<host>/auth/callback`, exactly.
   - **Scopes:** at least the ones in the `scopes` option.
   - Optionally tick **Require PKCE**. The add-on always uses PKCE (S256).
2. Set `oauth_client_id`, `oauth_client_secret`, `public_base_url` (`https://<host>`) and
   `allowed_athlete_ids` (your ID, for example `i123456`). Restart the add-on and check the log
   for `modes=oauth`.
3. In Claude, go to **Settings → Connectors → Add custom connector** with URL
   `https://<host>/mcp`. Claude registers itself, opens a consent page served by the add-on, then
   sends you to the Intervals.icu login. Connectors added on claude.ai also work in the mobile
   apps and in scheduled tasks.
4. If the athlete is not in `allowed_athlete_ids`, the login ends on an "Access denied" page and
   the authorisation is revoked again in Intervals.icu.

## Setup — webhooks (OAuth apps only)

Intervals.icu sends webhooks only to OAuth apps, for the athletes who authorised them.

1. Set `webhook_secret` and restart.
2. On the app page in Intervals.icu:
   - **Webhook URL:** `https://<host>/webhooks/intervals`
   - **Webhook Authorization Header:** the exact `webhook_secret` value (no `Bearer` needed; it
     is accepted either way).
   - **Tick:** `ACTIVITY_UPLOADED`, `WELLNESS_UPDATED`, `CALENDAR_UPDATED` and
     `SPORT_SETTINGS_UPDATED`. `ACTIVITY_ANALYZED` is optional.
3. Claude can now call `icu_get_recent_events(since=...)` to see what changed.
4. Optional: create a Home Assistant automation with a **Webhook** trigger, and put its URL in
   `ha_webhook_url`, for example `http://homeassistant.local.hass.io:8123/api/webhook/<id>`. Each
   delivery is forwarded as `{"source": "intervals.icu", "events": [...]}`.

## NGINX (Home Assistant SSL proxy add-on)

1. Copy [`nginx/intervals.conf`](nginx/intervals.conf) to `/share/nginx_proxy/intervals.conf`,
   and replace `intervals.gadde.nu` with your host name.
2. In the NGINX add-on configuration, enable customization:

   ```yaml
   customize:
     active: true
     default: nginx_proxy_default*.conf
     servers: nginx_proxy/*.conf
   ```

3. Make sure the certificate in `/ssl/fullchain.pem` covers the host (for example a `*.domain`
   wildcard), or point `ssl_certificate` at the right files. Then restart NGINX.

The snippet proxies to the add-on's published port and passes on the `X-Forwarded-*` headers.
It turns buffering off and allows 1-hour reads for streaming, and it turns off the access log so
that secret paths and tokens never land on disk. It also rate-limits each client IP to 10 requests
per second (burst 50, then `429`), which caps any guessing of secret paths or webhook secrets. **Do not forward port 8099 on your router.** Only
443 (NGINX) should be reachable from the internet.

The add-on trusts `X-Forwarded-*` only from the Supervisor network (`172.30.32.0/23`). Public URLs
are always built from `public_base_url`, never from request headers.

## Checking that it works

- `https://<host>/healthz` returns `{"status":"ok"}` (no authentication).
- `https://<host>/mcp` returns `401` with a `WWW-Authenticate` header (OAuth mode), or `404`
  (secret-URL mode only).
- `https://<host>/.well-known/oauth-authorization-server` returns JSON in OAuth mode.
- Everything else, including wrong secrets, returns `404`.

## Updating

The add-on installs the server from git at the ref set in `build.yaml` (`INTERVALS_MCP_REF`,
default `main`). Dependencies come from the committed `uv.lock` with hash checking. To pick up a
newer server, bump `version` in `config.yaml`, or use **Rebuild** in the add-on page.
