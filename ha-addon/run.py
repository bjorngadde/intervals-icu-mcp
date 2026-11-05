"""Home Assistant add-on launcher.

Maps /data/options.json to the server's environment variables, refuses to start
when no authentication mode is configured, then replaces itself with the server
(streamable HTTP on 0.0.0.0:8099, DATA_DIR=/data). Never prints option values.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("/data/options.json")
DATA_DIR = "/data"
PORT = "8099"
# Supervisor's internal network (hassio): the NGINX proxy add-on reaches us from it.
FORWARDED_ALLOW_IPS = "172.30.32.0/23,127.0.0.1"

#: add-on option -> server environment variable
OPTION_ENV = {
    "api_key": "INTERVALS_ICU_API_KEY",
    "athlete_id": "INTERVALS_ICU_ATHLETE_ID",
    "path_secret": "MCP_PATH_SECRET",
    "oauth_client_id": "INTERVALS_OAUTH_CLIENT_ID",
    "oauth_client_secret": "INTERVALS_OAUTH_CLIENT_SECRET",
    "public_base_url": "PUBLIC_BASE_URL",
    "allowed_athlete_ids": "ALLOWED_ATHLETE_IDS",
    "scopes": "OAUTH_SCOPES",
}
LOG_LEVELS = ("debug", "info", "warning", "error")


class AddonConfigError(Exception):
    """Configuration problem; the message never contains option values."""


def _text(options: Mapping[str, Any], key: str) -> str:
    value = options.get(key)
    return str(value).strip() if value is not None else ""


def build_environment(options: Mapping[str, Any], base: Mapping[str, str]) -> dict[str, str]:
    """Return the server environment for these add-on options.

    Raises AddonConfigError when no auth mode is configured or a mode is incomplete.
    """
    env = {k: v for k, v in base.items() if k not in OPTION_ENV.values()}
    for option, variable in OPTION_ENV.items():
        value = _text(options, option)
        if value:
            env[variable] = value

    path_secret = bool(_text(options, "path_secret"))
    oauth_id = bool(_text(options, "oauth_client_id"))
    oauth_secret = bool(_text(options, "oauth_client_secret"))
    oauth = oauth_id or oauth_secret

    if not path_secret and not oauth:
        raise AddonConfigError(
            "No authentication mode configured. Set path_secret (secret URL mode) "
            "and/or oauth_client_id + oauth_client_secret (OAuth mode). The add-on "
            "refuses to expose the MCP server without authentication."
        )
    if path_secret and not (_text(options, "api_key") and _text(options, "athlete_id")):
        raise AddonConfigError(
            "path_secret mode uses your Intervals.icu API key: set api_key and athlete_id."
        )
    if oauth:
        missing = [
            name
            for name in ("oauth_client_id", "oauth_client_secret", "public_base_url")
            if not _text(options, name)
        ]
        if missing:
            raise AddonConfigError("OAuth mode needs: " + ", ".join(missing) + ".")
        if not _text(options, "allowed_athlete_ids"):
            print(
                "[intervals-icu-mcp] WARNING: allowed_athlete_ids is empty — every OAuth "
                "login will be rejected.",
                file=sys.stderr,
            )

    env["DATA_DIR"] = DATA_DIR
    env.setdefault("FORWARDED_ALLOW_IPS", FORWARDED_ALLOW_IPS)
    return env


def server_argv(options: Mapping[str, Any]) -> list[str]:
    level = _text(options, "log_level").lower() or "info"
    if level not in LOG_LEVELS:
        level = "info"
    return [
        sys.executable,
        "-m",
        "intervals_icu_mcp.server",
        "--transport",
        "http",
        "--host",
        "0.0.0.0",
        "--port",
        PORT,
        "--log-level",
        level,
    ]


def describe(options: Mapping[str, Any]) -> str:
    """One safe startup line: which modes are on, never the values."""
    modes = []
    if _text(options, "oauth_client_id"):
        modes.append("oauth")
    if _text(options, "path_secret"):
        modes.append("secret-path")
    return "modes=" + ",".join(modes)


def main() -> None:
    try:
        options: Any = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("[intervals-icu-mcp] cannot read /data/options.json", file=sys.stderr)
        raise SystemExit(1) from None
    if not isinstance(options, dict):
        print("[intervals-icu-mcp] /data/options.json is not an object", file=sys.stderr)
        raise SystemExit(1)
    try:
        env = build_environment(options, os.environ)
    except AddonConfigError as e:
        print(f"[intervals-icu-mcp] configuration error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"[intervals-icu-mcp] starting on port {PORT} ({describe(options)})", file=sys.stderr)
    argv = server_argv(options)
    os.execvpe(argv[0], argv, env)


if __name__ == "__main__":
    main()
