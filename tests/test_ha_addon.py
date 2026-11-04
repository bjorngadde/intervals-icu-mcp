"""Home Assistant add-on: option mapping, startup refusal, and file sanity."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ADDON = Path(__file__).resolve().parent.parent / "ha-addon"

_spec = importlib.util.spec_from_file_location("ha_addon_run", ADDON / "run.py")
assert _spec and _spec.loader
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)

SECRET = "s" * 43


def _opts(**values: str) -> dict:
    base = {key: "" for key in run.OPTION_ENV}
    base["log_level"] = "info"
    base.update(values)
    return base


class TestEnvironmentMapping:
    def test_refuses_without_auth_mode(self):
        with pytest.raises(run.AddonConfigError) as exc:
            run.build_environment(_opts(api_key="k", athlete_id="i1"), {})
        assert "No authentication mode" in str(exc.value)

    def test_secret_mode_maps_env(self):
        env = run.build_environment(
            _opts(api_key="k", athlete_id="i1", path_secret=SECRET), {"PATH": "/bin"}
        )
        assert env["MCP_PATH_SECRET"] == SECRET
        assert env["INTERVALS_ICU_API_KEY"] == "k"
        assert env["INTERVALS_ICU_ATHLETE_ID"] == "i1"
        assert env["DATA_DIR"] == "/data"
        assert env["FORWARDED_ALLOW_IPS"] == "172.30.32.0/23,127.0.0.1"
        assert env["PATH"] == "/bin"
        assert "INTERVALS_OAUTH_CLIENT_ID" not in env  # empty options are not exported

    def test_secret_mode_needs_api_key(self):
        with pytest.raises(run.AddonConfigError) as exc:
            run.build_environment(_opts(path_secret=SECRET), {})
        assert "api_key" in str(exc.value)

    def test_oauth_mode_maps_env(self):
        env = run.build_environment(
            _opts(
                oauth_client_id="id",
                oauth_client_secret="sec",
                public_base_url="https://intervals.example.com",
                allowed_athlete_ids="i1",
                scopes="ACTIVITY:READ",
                webhook_secret="w" * 20,
                ha_webhook_url="http://ha/api/webhook/x",
            ),
            {},
        )
        assert env["INTERVALS_OAUTH_CLIENT_ID"] == "id"
        assert env["INTERVALS_OAUTH_CLIENT_SECRET"] == "sec"
        assert env["PUBLIC_BASE_URL"] == "https://intervals.example.com"
        assert env["ALLOWED_ATHLETE_IDS"] == "i1"
        assert env["OAUTH_SCOPES"] == "ACTIVITY:READ"
        assert env["INTERVALS_WEBHOOK_SECRET"] == "w" * 20
        assert env["HA_WEBHOOK_URL"] == "http://ha/api/webhook/x"

    @pytest.mark.parametrize(
        "missing", ["oauth_client_id", "oauth_client_secret", "public_base_url"]
    )
    def test_oauth_mode_incomplete(self, missing):
        values = {
            "oauth_client_id": "id",
            "oauth_client_secret": "sec",
            "public_base_url": "https://x.example",
        }
        values[missing] = ""
        with pytest.raises(run.AddonConfigError) as exc:
            run.build_environment(_opts(**values), {})
        assert missing in str(exc.value)
        assert "sec" not in str(exc.value).replace("oauth_client_secret", "")

    def test_empty_allowlist_warns_but_starts(self, capsys):
        run.build_environment(
            _opts(oauth_client_id="id", oauth_client_secret="sec", public_base_url="https://x.io"),
            {},
        )
        assert "allowed_athlete_ids is empty" in capsys.readouterr().err

    def test_ha_url_needs_webhook_secret(self):
        with pytest.raises(run.AddonConfigError):
            run.build_environment(
                _opts(api_key="k", athlete_id="i1", path_secret=SECRET, ha_webhook_url="http://h"),
                {},
            )

    def test_stale_env_values_cannot_leak_in(self):
        env = run.build_environment(
            _opts(api_key="k", athlete_id="i1", path_secret=SECRET),
            {"INTERVALS_OAUTH_CLIENT_ID": "stale"},
        )
        assert "INTERVALS_OAUTH_CLIENT_ID" not in env

    def test_server_argv(self):
        argv = run.server_argv(_opts(log_level="debug"))
        assert argv[1:] == [
            "-m",
            "intervals_icu_mcp.server",
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "8099",
            "--log-level",
            "debug",
        ]
        assert run.server_argv(_opts(log_level="bogus"))[-1] == "info"

    def test_describe_never_prints_values(self):
        line = run.describe(_opts(path_secret=SECRET, oauth_client_id="client-id-value"))
        assert SECRET not in line
        assert "client-id-value" not in line
        assert line == "modes=oauth,secret-path"

    def test_main_exits_cleanly_on_bad_config(self, tmp_path, monkeypatch, capsys):
        options = tmp_path / "options.json"
        options.write_text(json.dumps(_opts(api_key="secret-api-key-value")))
        monkeypatch.setattr(run, "OPTIONS_PATH", options)
        with pytest.raises(SystemExit) as exc:
            run.main()
        assert exc.value.code == 1
        assert "secret-api-key-value" not in capsys.readouterr().err

    def test_main_execs_server(self, tmp_path, monkeypatch):
        options = tmp_path / "options.json"
        options.write_text(json.dumps(_opts(api_key="k", athlete_id="i1", path_secret=SECRET)))
        monkeypatch.setattr(run, "OPTIONS_PATH", options)
        calls = []
        monkeypatch.setattr(run.os, "execvpe", lambda *a: calls.append(a))
        run.main()
        ((file, argv, env),) = calls
        assert file == sys.executable
        assert "intervals_icu_mcp.server" in argv
        assert env["MCP_PATH_SECRET"] == SECRET


class TestAddonFiles:
    def test_config_options_match_schema_and_launcher(self):
        config = yaml.safe_load((ADDON / "config.yaml").read_text())
        assert set(config["options"]) == set(config["schema"])
        assert set(config["options"]) == set(run.OPTION_ENV) | {"log_level"}
        for secret in ("api_key", "path_secret", "oauth_client_secret", "webhook_secret"):
            assert config["schema"][secret].startswith("password")
        assert config["arch"] == ["amd64", "aarch64"]
        assert config["ports"] == {"8099/tcp": 8099}
        assert config["ingress"] is False
        assert config["init"] is False
        assert "/healthz" in config["watchdog"]

    def test_build_targets_home_assistant_python_base(self):
        build = yaml.safe_load((ADDON / "build.yaml").read_text())
        assert set(build["build_from"]) == {"amd64", "aarch64"}
        for image in build["build_from"].values():
            assert image.startswith("ghcr.io/home-assistant/")
            assert "-base-python:" in image

    def test_dockerfile_runs_launcher(self):
        dockerfile = (ADDON / "Dockerfile").read_text()
        assert "ARG BUILD_FROM" in dockerfile
        assert 'CMD ["python3", "/run.py"]' in dockerfile
        assert "--frozen" in dockerfile  # dependencies come from uv.lock

    def test_translations_cover_every_option(self):
        config = yaml.safe_load((ADDON / "config.yaml").read_text())
        translations = yaml.safe_load((ADDON / "translations" / "en.yaml").read_text())
        assert set(translations["configuration"]) == set(config["options"])

    def test_nginx_snippet_requirements(self):
        conf = (ADDON / "nginx" / "intervals.conf").read_text()
        for directive in (
            "server_name intervals.gadde.nu;",
            "proxy_pass http://homeassistant.local.hass.io:8099;",
            "proxy_buffering         off;",
            "access_log off;",
            "X-Forwarded-For",
            "X-Forwarded-Proto",
            "X-Forwarded-Host",
            "proxy_read_timeout      1h;",
            "limit_req_zone $binary_remote_addr zone=intervals_mcp:10m rate=10r/s;",
            "limit_req        zone=intervals_mcp burst=50 nodelay;",
            "limit_req_status 429;",
        ):
            assert directive in conf, directive

    def test_repository_manifest(self):
        manifest = yaml.safe_load((ADDON.parent / "repository.yaml").read_text())
        assert manifest["name"]
        assert "github.com" in manifest["url"]
