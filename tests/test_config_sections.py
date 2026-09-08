"""Tests for lib/config_sections.py — the declarative registry behind `atc init`."""
from __future__ import annotations

from lib.config_sections import (
    SECTIONS,
    build_list_section,
    build_scalar_section,
    merge_config,
    resolve_secret_key,
    section_by_key,
)


class TestRegistryIntegrity:
    def test_all_sections_have_unique_keys(self):
        keys = [s.key for s in SECTIONS]
        assert len(keys) == len(set(keys))

    def test_every_section_has_at_least_one_field(self):
        for s in SECTIONS:
            assert s.fields, f"{s.key} has no fields"

    def test_only_proxmox_is_required_for_core(self):
        required = [s.key for s in SECTIONS if s.required_for_core]
        assert required == ["proxmox"]

    def test_pihole_and_tls_are_list_sections(self):
        assert section_by_key("pihole").is_list
        assert section_by_key("tls").is_list

    def test_most_sections_are_scalar(self):
        scalar = [s for s in SECTIONS if not s.is_list]
        assert len(scalar) >= 15

    def test_secret_fields_have_a_secret_key_template(self):
        for s in SECTIONS:
            for f in s.fields:
                if f.secret:
                    assert f.secret_key, f"{s.key}.{f.name} is secret but has no secret_key"

    def test_field_kinds_are_valid(self):
        valid = {"str", "int", "bool"}
        for s in SECTIONS:
            for f in s.fields:
                assert f.kind in valid, f"{s.key}.{f.name} has invalid kind {f.kind!r}"


class TestSectionByKey:
    def test_finds_existing_section(self):
        s = section_by_key("proxmox")
        assert s is not None
        assert s.title.startswith("Proxmox")

    def test_returns_none_for_unknown_key(self):
        assert section_by_key("nonexistent") is None


class TestResolveSecretKey:
    def test_scalar_section_key_unchanged(self):
        spec = section_by_key("beszel")
        api_key_field = next(f for f in spec.fields if f.name == "api_key")
        assert resolve_secret_key(api_key_field) == "beszel-api-key"

    def test_list_section_key_substitutes_name(self):
        spec = section_by_key("pihole")
        pw_field = next(f for f in spec.fields if f.name == "password")
        assert resolve_secret_key(pw_field, "Primary") == "pihole-primary-password"

    def test_list_section_key_lowercases_and_dashes_spaces(self):
        spec = section_by_key("pihole")
        pw_field = next(f for f in spec.fields if f.name == "password")
        assert resolve_secret_key(pw_field, "Living Room DNS") == "pihole-living-room-dns-password"

    def test_no_instance_name_leaves_template_literal(self):
        spec = section_by_key("pihole")
        pw_field = next(f for f in spec.fields if f.name == "password")
        assert resolve_secret_key(pw_field, "") == "pihole-{name}-password"


class TestBuildScalarSection:
    def test_builds_dict_from_answers(self):
        spec = section_by_key("beszel")
        answers = {
            "host": "beszel.local", "port": 443, "https": True,
            "api_key": "keychain:beszel-api-key", "verify_ssl": False,
        }
        result = build_scalar_section(spec, answers)
        assert result["host"] == "beszel.local"
        assert result["api_key"] == "keychain:beszel-api-key"

    def test_omits_empty_optional_fields(self):
        spec = section_by_key("radarr")
        answers = {
            "host": "r.local", "port": 7878, "https": False,
            "api_key": "keychain:radarr-api-key", "verify_ssl": False,
            "base_path": "",
        }
        result = build_scalar_section(spec, answers)
        assert "base_path" not in result

    def test_keeps_empty_required_fields_if_present(self):
        # required fields shouldn't normally be empty (validated earlier in
        # the wizard), but build_scalar_section itself doesn't filter them
        spec = section_by_key("beszel")
        answers = {"host": "", "port": 443, "https": True,
                   "api_key": "keychain:x", "verify_ssl": False}
        result = build_scalar_section(spec, answers)
        assert result["host"] == ""

    def test_ignores_answers_not_in_spec(self):
        spec = section_by_key("beszel")
        answers = {"host": "h", "bogus_field": "x"}
        result = build_scalar_section(spec, answers)
        assert "bogus_field" not in result


class TestBuildListSection:
    def test_builds_list_of_dicts(self):
        spec = section_by_key("pihole")
        instances = [
            {"name": "Primary", "host": "ph1.local", "port": 80, "https": False,
             "password": "keychain:pihole-primary-password", "primary": True, "verify_ssl": False},
            {"name": "Secondary", "host": "ph2.local", "port": 80, "https": False,
             "password": "keychain:pihole-secondary-password", "primary": False, "verify_ssl": False},
        ]
        result = build_list_section(spec, instances)
        assert len(result) == 2
        assert result[0]["name"] == "Primary"
        assert result[1]["name"] == "Secondary"

    def test_empty_instances_gives_empty_list(self):
        spec = section_by_key("pihole")
        assert build_list_section(spec, []) == []


class TestMergeConfig:
    def test_adds_new_section(self):
        existing = {"proxmox": {"host": "pve.local"}}
        updates = {"beszel": {"host": "beszel.local"}}
        merged = merge_config(existing, updates)
        assert merged["proxmox"] == {"host": "pve.local"}
        assert merged["beszel"] == {"host": "beszel.local"}

    def test_replaces_existing_section_wholesale(self):
        existing = {"beszel": {"host": "old.local", "port": 443}}
        updates = {"beszel": {"host": "new.local", "port": 8443}}
        merged = merge_config(existing, updates)
        assert merged["beszel"] == {"host": "new.local", "port": 8443}

    def test_leaves_untouched_keys_alone(self):
        existing = {"proxmox": {"host": "pve.local"}, "scripts_dir": "../../"}
        updates = {"beszel": {"host": "beszel.local"}}
        merged = merge_config(existing, updates)
        assert merged["scripts_dir"] == "../../"

    def test_does_not_mutate_existing_dict(self):
        existing = {"proxmox": {"host": "pve.local"}}
        updates = {"beszel": {"host": "beszel.local"}}
        merge_config(existing, updates)
        assert "beszel" not in existing
