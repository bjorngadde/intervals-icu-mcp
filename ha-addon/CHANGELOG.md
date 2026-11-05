# Changelog

## 0.2.0

- Removed the webhook receiver and the `webhook_secret` / `ha_webhook_url` options. The coach
  reads the data directly on each run, so the event log added little. Remove both options from
  your configuration if you had set them.

## 0.1.0

- First version: streamable HTTP on port 8099, secret-URL and OAuth modes, webhook receiver,
  NGINX snippet.
