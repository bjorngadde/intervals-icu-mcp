# Changelog

## 0.3.0

- New option `delete_mode` (`safe`, `full` or `none`), passed on as `INTERVALS_ICU_DELETE_MODE`.
- New tool `icu_delete_duplicate_activity`, available in `safe` and `full` mode.

## 0.2.0

- Removed the webhook receiver and the `webhook_secret` / `ha_webhook_url` options. The coach
  reads the data directly on each run, so the event log added little. Remove both options from
  your configuration if you had set them.

## 0.1.0

- First version: streamable HTTP on port 8099, secret-URL and OAuth modes, webhook receiver,
  NGINX snippet.
