"""Tests for config dataclasses and YAML loading (config.py)."""
import textwrap
from pathlib import Path

import pytest

from config import (
    KEYCHAIN_PREFIX,
    ArrConfig,
    BeszelConfig,
    HAConfig,
    PiholeInstanceConfig,
    PlexConfig,
    SeerrConfig,
    SSHConfig,
    TautulliConfig,
    load_config,
)

# ── base_url properties ───────────────────────────────────────────────────────

class TestArrConfigBaseUrl:
    def test_http_defaults(self):
        cfg = ArrConfig(name="radarr", host="192.168.1.1", port=7878, api_key="k")
        assert cfg.base_url == "http://192.168.1.1:7878"

    def test_https(self):
        cfg = ArrConfig(name="sonarr", host="sonarr.local", port=443, https=True, api_key="k")
        assert cfg.base_url == "https://sonarr.local:443"

    def test_base_path_appended(self):
        cfg = ArrConfig(name="radarr", host="nas", port=80, base_path="/radarr", api_key="k")
        assert cfg.base_url == "http://nas:80/radarr"

    def test_https_with_base_path(self):
        cfg = ArrConfig(name="sonarr", host="nas", port=443, https=True, base_path="/sonarr", api_key="k")
        assert cfg.base_url == "https://nas:443/sonarr"

    def test_empty_base_path_no_trailing_slash(self):
        cfg = ArrConfig(name="radarr", host="h", port=7878, base_path="", api_key="k")
        assert not cfg.base_url.endswith("/")


class TestSeerrConfigBaseUrl:
    def test_https_default(self):
        cfg = SeerrConfig(host="seerr.local", port=443, api_key="k")
        assert cfg.base_url == "https://seerr.local:443"

    def test_http_override(self):
        cfg = SeerrConfig(host="seerr.local", port=80, https=False, api_key="k")
        assert cfg.base_url == "http://seerr.local:80"


class TestPlexConfigBaseUrl:
    def test_default_port(self):
        cfg = PlexConfig(host="plex.local", token="t")
        assert cfg.base_url == "http://plex.local:32400"

    def test_https(self):
        cfg = PlexConfig(host="plex.local", port=443, https=True, token="t")
        assert cfg.base_url == "https://plex.local:443"


class TestHAConfigBaseUrl:
    def test_default(self):
        cfg = HAConfig(host="ha.local", token="t")
        assert cfg.base_url == "http://ha.local:8123"

    def test_https(self):
        cfg = HAConfig(host="ha.local", port=443, https=True, token="t")
        assert cfg.base_url == "https://ha.local:443"


class TestBeszelConfigBaseUrl:
    def test_https_default(self):
        cfg = BeszelConfig(host="beszel.local", api_key="k")
        assert cfg.base_url == "https://beszel.local:443"

    def test_http_override(self):
        cfg = BeszelConfig(host="beszel.local", port=8090, https=False, api_key="k")
        assert cfg.base_url == "http://beszel.local:8090"


class TestPiholeConfigBaseUrl:
    def test_http(self):
        cfg = PiholeInstanceConfig(name="ph", host="pihole.local", port=80, password="p")
        assert cfg.base_url == "http://pihole.local:80"

    def test_https(self):
        cfg = PiholeInstanceConfig(name="ph", host="pihole.local", port=8443, https=True, password="p")
        assert cfg.base_url == "https://pihole.local:8443"


class TestTautulliConfigBaseUrl:
    def test_http_default_port(self):
        cfg = TautulliConfig(host="192.168.1.10", api_key="k")
        assert cfg.base_url == "http://192.168.1.10:8181"

    def test_http_custom_port(self):
        cfg = TautulliConfig(host="tautulli.local", port=8282, api_key="k")
        assert cfg.base_url == "http://tautulli.local:8282"

    def test_https(self):
        cfg = TautulliConfig(host="tautulli.local", port=443, https=True, api_key="k")
        assert cfg.base_url == "https://tautulli.local:443"


# ── SSHConfig ─────────────────────────────────────────────────────────────────

class TestSSHConfig:
    def test_node_host_default_pattern(self):
        cfg = SSHConfig()
        assert cfg.node_host("draken") == "draken"

    def test_node_host_custom_pattern(self):
        cfg = SSHConfig(node_host_pattern="pve-{name}.local.example.com")
        assert cfg.node_host("draken") == "pve-draken.local.example.com"

    def test_expanded_key_file_resolves_tilde(self):
        cfg = SSHConfig(key_file="~/.ssh/id_rsa")
        assert not cfg.expanded_key_file.startswith("~")
        assert "id_rsa" in cfg.expanded_key_file

    def test_expanded_key_file_absolute_unchanged(self):
        cfg = SSHConfig(key_file="/etc/ssh/key")
        assert cfg.expanded_key_file == "/etc/ssh/key"


# ── Keychain sentinel ─────────────────────────────────────────────────────────

class TestKeychainSentinel:
    def test_sentinel_prefix_constant(self):
        assert KEYCHAIN_PREFIX == "keychain:"

    def test_plain_value_does_not_start_with_sentinel(self):
        assert not "myplaintoken".startswith(KEYCHAIN_PREFIX)

    def test_sentinel_value_starts_with_prefix(self):
        assert "keychain:my-secret-key".startswith(KEYCHAIN_PREFIX)

    def test_key_name_extraction(self):
        raw = "keychain:pve-cluster-token"
        key_name = raw[len(KEYCHAIN_PREFIX):]
        assert key_name == "pve-cluster-token"


# ── YAML loading ──────────────────────────────────────────────────────────────

def _minimal_yaml(tmp_path: Path, extra: str = "") -> Path:
    """Write a minimal valid config.yaml that avoids keychain prompts.

    `extra` is appended as-is after the base block — pass a dedented YAML
    string with no leading spaces (textwrap.dedent the caller side).
    """
    path = tmp_path / "config.yaml"
    base = textwrap.dedent("""\
        proxmox:
          host: pve.local
          user: root@pam
          token_name: atc
          token_value: plaintoken
        ssh: {}
    """)
    path.write_text(base + (textwrap.dedent(extra) if extra else ""))
    return path


class TestLoadConfig:
    def test_loads_proxmox_section(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path))
        assert cfg.proxmox.host == "pve.local"
        assert cfg.proxmox.user == "root@pam"
        assert cfg.proxmox.token_value == "plaintoken"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="config.yaml not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_optional_sections_default_none(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path))
        assert cfg.radarr is None
        assert cfg.sonarr is None
        assert cfg.plex is None
        assert cfg.ha is None
        assert cfg.seerr is None
        assert cfg.tautulli is None

    def test_radarr_section_parsed(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path, extra=textwrap.dedent("""\
            radarr:
              host: 192.168.1.1
              port: 7878
              api_key: testradarrkey
        """)))
        assert cfg.radarr is not None
        assert cfg.radarr.host == "192.168.1.1"
        assert cfg.radarr.port == 7878
        assert cfg.radarr.api_key == "testradarrkey"
        assert cfg.radarr.base_url == "http://192.168.1.1:7878"

    def test_sonarr_section_parsed(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path, extra=textwrap.dedent("""\
            sonarr:
              host: 192.168.1.1
              port: 8989
              api_key: testsonarrkey
        """)))
        assert cfg.sonarr is not None
        assert cfg.sonarr.base_url == "http://192.168.1.1:8989"

    def test_plex_section_parsed(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path, extra=textwrap.dedent("""\
            plex:
              host: 192.168.1.192
              port: 32400
              token: testplextoken
        """)))
        assert cfg.plex is not None
        assert cfg.plex.token == "testplextoken"

    def test_ha_stats_parsed(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path, extra=textwrap.dedent("""\
            homeassistant:
              host: 192.168.1.45
              token: hatoken
              stats:
                - name: Room Temp
                  entity: sensor.loft_temperature
                  unit: "°C"
        """)))
        assert cfg.ha is not None
        assert len(cfg.ha.stats) == 1
        assert cfg.ha.stats[0].name == "Room Temp"
        assert cfg.ha.stats[0].entity == "sensor.loft_temperature"
        assert cfg.ha.stats[0].unit == "°C"

    def test_tls_checks_parsed(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path, extra=textwrap.dedent("""\
            tls:
              - name: "example.com"
                host: example.com
                port: 443
        """)))
        assert len(cfg.tls_checks) == 1
        assert cfg.tls_checks[0].name == "example.com"

    def test_config_path_stored(self, tmp_path):
        path = _minimal_yaml(tmp_path)
        cfg = load_config(path)
        assert cfg.config_path == path.resolve()

    def test_scripts_dir_default(self, tmp_path):
        # scripts_dir unset in config.yaml → no raw default string; the
        # fallback behavior lives in resolved_scripts_dir (scripts/ next to
        # config.yaml), not in the raw field itself. See the comment on
        # resolved_scripts_dir for why "../../" stopped being the default.
        cfg = load_config(_minimal_yaml(tmp_path))
        assert cfg.scripts_dir is None
        assert cfg.resolved_scripts_dir == (tmp_path / "scripts").resolve()

    def test_pihole_primary_flag(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path, extra=textwrap.dedent("""\
            pihole:
              - name: Primary
                host: pihole1.local
                port: 80
                password: pw1
                primary: true
              - name: Replica
                host: pihole2.local
                port: 80
                password: pw2
        """)))
        assert len(cfg.pihole_instances) == 2
        primary = next(p for p in cfg.pihole_instances if p.primary)
        assert primary.name == "Primary"

    def test_tautulli_section_parsed(self, tmp_path):
        cfg = load_config(_minimal_yaml(tmp_path, extra=textwrap.dedent("""\
            tautulli:
              host: 192.168.1.10
              port: 8181
              api_key: testtautullikey
        """)))
        assert cfg.tautulli is not None
        assert cfg.tautulli.host == "192.168.1.10"
        assert cfg.tautulli.port == 8181
        assert cfg.tautulli.api_key == "testtautullikey"
        assert cfg.tautulli.base_url == "http://192.168.1.10:8181"


# ── Encrypted keyring migration ─────────────────────────────────────────────

class TestSecretsMigrateEncryption:
    """End-to-end: real plaintext keyring -> real AES-256 encrypted keyring,
    no mocks. Monkeypatches config.py's path constants to tmp_path so this
    never touches the real keyring files on the machine running the tests.
    """

    def _seed_old_plaintext_backend(self, old_path, entries: dict[str, str]):
        from keyrings.alt.file import PlaintextKeyring

        old_path.parent.mkdir(parents=True, exist_ok=True)
        backend = PlaintextKeyring()
        backend.file_path = str(old_path)
        for k, v in entries.items():
            backend.set_password("atc", k, v)
        index = ",".join(sorted(entries.keys()))
        backend.set_password("atc", "__index__", index)

    def test_migrates_and_verifies_all_entries(self, tmp_path, monkeypatch):
        import config as config_module

        old_path = tmp_path / "old_keyring" / "keyring_pass.cfg"
        new_path = tmp_path / "new_keyring" / "keyring_crypt.cfg"
        pw_path = tmp_path / "pw" / "keyring.key"

        entries = {"secret-a": "value-a", "secret-b": "value-b-with-symbols!@#"}
        self._seed_old_plaintext_backend(old_path, entries)

        monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            config_module, "_ENCRYPTED_KEYRING_FILE", new_path
        )
        monkeypatch.setattr(
            config_module, "_ENCRYPTED_KEYRING_PASSWORD_FILE", pw_path
        )
        # secrets_migrate_encryption() hardcodes the old path relative to
        # Path.home(), which we've patched above to tmp_path — so re-point
        # the seeded old backend to match exactly what the function expects.
        expected_old_path = tmp_path / ".local/share/python_keyring/keyring_pass.cfg"
        expected_old_path.parent.mkdir(parents=True, exist_ok=True)
        self._seed_old_plaintext_backend(expected_old_path, entries)

        count, mismatches = config_module.secrets_migrate_encryption()

        assert mismatches == []
        assert count == len(entries) + 1  # +1 for __index__

        # Old plaintext file must be completely untouched.
        assert expected_old_path.exists()

        # New encrypted backend must actually resolve the real values.
        # Use the same fast-KDF subclass the migration itself writes with
        # (default CryptFileKeyring params would derive a different key).
        CryptFileKeyring = config_module._fast_cryptfile_keyring_class()

        password = pw_path.read_text().strip()
        assert password  # a real password was generated
        verify_backend = CryptFileKeyring()
        verify_backend.file_path = str(new_path)
        verify_backend.keyring_key = password
        for k, v in entries.items():
            assert verify_backend.get_password("atc", k) == v

    def test_new_file_is_not_plaintext(self, tmp_path, monkeypatch):
        import config as config_module

        entries = {"secret-a": "super-secret-plaintext-value"}
        expected_old_path = tmp_path / ".local/share/python_keyring/keyring_pass.cfg"
        expected_old_path.parent.mkdir(parents=True, exist_ok=True)
        self._seed_old_plaintext_backend(expected_old_path, entries)

        new_path = tmp_path / "new_keyring" / "keyring_crypt.cfg"
        pw_path = tmp_path / "pw" / "keyring.key"
        monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(config_module, "_ENCRYPTED_KEYRING_FILE", new_path)
        monkeypatch.setattr(config_module, "_ENCRYPTED_KEYRING_PASSWORD_FILE", pw_path)

        count, mismatches = config_module.secrets_migrate_encryption()
        assert mismatches == []
        assert count > 0

        raw_new_content = new_path.read_text()
        assert "super-secret-plaintext-value" not in raw_new_content

    def test_rerun_is_idempotent(self, tmp_path, monkeypatch):
        import config as config_module

        entries = {"secret-a": "value-a"}
        expected_old_path = tmp_path / ".local/share/python_keyring/keyring_pass.cfg"
        expected_old_path.parent.mkdir(parents=True, exist_ok=True)
        self._seed_old_plaintext_backend(expected_old_path, entries)

        new_path = tmp_path / "new_keyring" / "keyring_crypt.cfg"
        pw_path = tmp_path / "pw" / "keyring.key"
        monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(config_module, "_ENCRYPTED_KEYRING_FILE", new_path)
        monkeypatch.setattr(config_module, "_ENCRYPTED_KEYRING_PASSWORD_FILE", pw_path)

        count1, mismatches1 = config_module.secrets_migrate_encryption()
        password_after_first_run = pw_path.read_text()

        count2, mismatches2 = config_module.secrets_migrate_encryption()
        password_after_second_run = pw_path.read_text()

        assert mismatches1 == [] and mismatches2 == []
        assert count1 == count2
        # Re-run must not regenerate the master password.
        assert password_after_first_run == password_after_second_run


class TestKeyringBackendStatus:
    """Real end-to-end checks of keyring_backend_status() against actual
    plaintext-keyring files in tmp_path, no mocks of the status function
    itself (only Path.home()/module path constants are monkeypatched so
    tests never touch the real machine's keyring).
    """

    def _seed_old_plaintext_backend(self, old_path, entries: dict[str, str]):
        from keyrings.alt.file import PlaintextKeyring

        old_path.parent.mkdir(parents=True, exist_ok=True)
        backend = PlaintextKeyring()
        backend.file_path = str(old_path)
        for k, v in entries.items():
            backend.set_password("atc", k, v)
        index = ",".join(sorted(entries.keys()))
        backend.set_password("atc", "__index__", index)

    def test_upgrade_available_when_plaintext_has_secrets_and_no_encrypted_backend(
        self, tmp_path, monkeypatch
    ):
        import config as config_module
        import keyring as keyring_module
        from keyrings.alt.file import PlaintextKeyring

        old_path = tmp_path / ".local/share/python_keyring/keyring_pass.cfg"
        self._seed_old_plaintext_backend(old_path, {"secret-a": "value-a"})

        active_plaintext = PlaintextKeyring()
        active_plaintext.file_path = str(old_path)
        monkeypatch.setattr(keyring_module, "get_keyring", lambda: active_plaintext)
        monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            config_module, "_ENCRYPTED_KEYRING_FILE", tmp_path / "does-not-exist" / "x.cfg"
        )
        monkeypatch.setattr(
            config_module, "_ENCRYPTED_KEYRING_PASSWORD_FILE", tmp_path / "does-not-exist" / "y.key"
        )

        status = config_module.keyring_backend_status()

        assert status["backend"] == "keyrings.alt.file.PlaintextKeyring"
        assert status["encrypted"] is False
        assert status["upgrade_available"] is True
        assert status["plaintext_has_secrets"] is True

    def test_no_upgrade_needed_when_encrypted_backend_already_active(self, tmp_path, monkeypatch):
        import config as config_module
        import keyring as keyring_module

        new_path = tmp_path / "new_keyring" / "keyring_crypt.cfg"
        pw_path = tmp_path / "pw" / "keyring.key"
        new_path.parent.mkdir(parents=True)
        pw_path.parent.mkdir(parents=True)
        new_path.write_text("dummy")
        pw_path.write_text("dummy-password")

        FastCryptFileKeyring = config_module._fast_cryptfile_keyring_class()
        active_encrypted = FastCryptFileKeyring()

        monkeypatch.setattr(keyring_module, "get_keyring", lambda: active_encrypted)
        monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(config_module, "_ENCRYPTED_KEYRING_FILE", new_path)
        monkeypatch.setattr(config_module, "_ENCRYPTED_KEYRING_PASSWORD_FILE", pw_path)

        status = config_module.keyring_backend_status()

        assert status["encrypted"] is True
        assert status["upgrade_available"] is False

    def test_no_upgrade_needed_when_plaintext_is_empty(self, tmp_path, monkeypatch):
        import config as config_module
        import keyring as keyring_module
        from keyrings.alt.file import PlaintextKeyring

        old_path = tmp_path / ".local/share/python_keyring/keyring_pass.cfg"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        # No secrets set — file may not even exist yet.

        active_plaintext = PlaintextKeyring()
        active_plaintext.file_path = str(old_path)
        monkeypatch.setattr(keyring_module, "get_keyring", lambda: active_plaintext)
        monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            config_module, "_ENCRYPTED_KEYRING_FILE", tmp_path / "does-not-exist" / "x.cfg"
        )
        monkeypatch.setattr(
            config_module, "_ENCRYPTED_KEYRING_PASSWORD_FILE", tmp_path / "does-not-exist" / "y.key"
        )

        status = config_module.keyring_backend_status()

        assert status["upgrade_available"] is False
        assert status["plaintext_has_secrets"] is False
