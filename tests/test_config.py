"""Tests for tahuti.config."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tahuti.cache import DEFAULT_TTL
from tahuti.config import (
    DEFAULT_CACHE_TTL,
    AppState,
    ProfileConfig,
    SessionConfig,
    clear_session,
    load_state,
    resolve_config_path,
    resolve_session_path,
    save_profile,
    save_session,
)


class TestResolveConfigPath:
    def test_explicit_path(self):
        assert resolve_config_path("/my/path") == Path("/my/path")

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", "/env/config.json")
        assert resolve_config_path(None) == Path("/env/config.json")

    def test_default(self, monkeypatch):
        monkeypatch.delenv("MANAGEBAC_CONFIG", raising=False)
        result = resolve_config_path(None)
        assert result.name == "config.json"
        assert "tahuti" in str(result)


class TestResolveSessionPath:
    def test_explicit_path(self):
        assert resolve_session_path("/my/session") == Path("/my/session")

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_SESSION", "/env/session.json")
        assert resolve_session_path(None) == Path("/env/session.json")

    def test_default(self, monkeypatch):
        monkeypatch.delenv("MANAGEBAC_SESSION", raising=False)
        result = resolve_session_path(None)
        assert result.name == "session.json"
        assert "tahuti" in str(result)


class TestLoadState:
    def test_default_when_no_files(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))
        state = load_state()
        assert state.active_profile == "default"
        assert state.profile.domain == "managebac.com"
        assert state.session.cookie is None

    def test_loads_from_existing_files(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {
                    "active_profile": "test",
                    "profiles": {
                        "test": {
                            "school": "myschool",
                            "domain": "managebac.cn",
                            "email": "test@example.com",
                            "defaults": {
                                "view": "upcoming",
                                "pages": 5,
                                "subject": "Math",
                                "details": True,
                                "format": "json",
                                "cache_ttl": 600,
                            },
                        }
                    },
                }
            )
        )
        session_path.write_text(
            json.dumps(
                {
                    "active_profile": "test",
                    "profiles": {
                        "test": {
                            "school": "myschool",
                            "domain": "managebac.cn",
                            "email": "test@example.com",
                            "base_url": "https://myschool.managebac.cn",
                            "cookie": "session_cookie_123",
                            "logged_in_at": "2026-04-29T12:00:00",
                        }
                    },
                }
            )
        )
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state()
        assert state.active_profile == "test"
        assert state.profile.school == "myschool"
        assert state.profile.email == "test@example.com"
        assert state.profile.default_view == "upcoming"
        assert state.profile.default_pages == 5
        assert state.profile.default_subject == "Math"
        assert state.profile.default_details is True
        assert state.profile.default_cache_ttl == 600
        assert state.session.cookie == "session_cookie_123"
        assert state.session.base_url == "https://myschool.managebac.cn"

    def test_profile_name_override(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {"profiles": {"alpha": {"school": "s1"}, "beta": {"school": "s2"}}}
            )
        )
        session_path.write_text(
            json.dumps(
                {"profiles": {"alpha": {"cookie": "c1"}, "beta": {"cookie": "c2"}}}
            )
        )
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state(profile_name="beta")
        assert state.active_profile == "beta"
        assert state.profile.school == "s2"
        assert state.session.cookie == "c2"


class TestSaveProfile:
    def test_creates_and_writes_profile(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state()
        state.profile.school = "myschool"
        state.profile.email = "me@example.com"
        save_profile(state)

        data = json.loads(config_path.read_text())
        assert data["profiles"]["default"]["school"] == "myschool"
        assert data["profiles"]["default"]["email"] == "me@example.com"
        assert data["version"] == 1

    def test_preserves_other_profiles(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "existing": {"school": "old_school"},
                        "default": {},
                    }
                }
            )
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state()
        state.profile.school = "new_school"
        save_profile(state)

        data = json.loads(config_path.read_text())
        assert data["profiles"]["existing"]["school"] == "old_school"
        assert data["profiles"]["default"]["school"] == "new_school"


class TestSaveSession:
    def test_creates_and_writes_session(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state()
        state.session.cookie = "my_cookie"
        state.session.base_url = "https://myschool.managebac.cn"
        save_session(state)

        data = json.loads(session_path.read_text())
        assert data["profiles"]["default"]["cookie"] == "my_cookie"
        assert data["profiles"]["default"]["base_url"] == "https://myschool.managebac.cn"


class TestClearSession:
    def test_clear_single_profile(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        session_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "default": {"cookie": "c1"},
                        "other": {"cookie": "c2"},
                    }
                }
            )
        )
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state()
        clear_session(state)

        data = json.loads(session_path.read_text())
        assert "default" not in data["profiles"]
        assert data["profiles"]["other"]["cookie"] == "c2"

    def test_clear_all_profiles(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        session_path.write_text(
            json.dumps(
                {"profiles": {"default": {"cookie": "c1"}, "other": {"cookie": "c2"}}}
            )
        )
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state()
        clear_session(state, all_profiles=True)
        assert not session_path.exists()

    def test_clear_last_profile_removes_file(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        session_path.write_text(json.dumps({"profiles": {"default": {"cookie": "c1"}}}))
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(session_path))

        state = load_state()
        clear_session(state)
        assert not session_path.exists()


def test_write_json_is_atomic_and_0600(tmp_path):
    """Credential/session files must never exist world-readable."""
    from tahuti.config import _write_json
    target = tmp_path / "creds.json"
    _write_json(target, {"email": "a@b.c", "password": "s3cret"})
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)
    # No temp files left behind.
    assert list(tmp_path.glob(".*tmp")) == []


def test_write_json_replaces_existing_content(tmp_path):
    from tahuti.config import _write_json
    target = tmp_path / "session.json"
    _write_json(target, {"v": 1})
    _write_json(target, {"v": 2})
    import json
    assert json.loads(target.read_text()) == {"v": 2}
    assert target.stat().st_mode & 0o777 == 0o600


class TestCacheTTLCoercion:
    """A `cache_ttl` key present with a null value must not become None.

    `dict.get(key, default)` returns the default only when the key is absent,
    so `"cache_ttl": null` used to flow straight through as None and
    ResponseCache(ttl=None) raised TypeError on its first get(), after put()
    had already written the entry.
    """

    def _state_with(self, tmp_path, ttl_value):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "active_profile": "test",
                    "profiles": {
                        "test": {
                            "school": "myschool",
                            "domain": "managebac.cn",
                            "defaults": {"cache_ttl": ttl_value},
                        }
                    },
                }
            )
        )
        return load_state("test", config_path=config_path)

    def test_null_ttl_becomes_the_default(self, tmp_path):
        state = self._state_with(tmp_path, None)
        assert state.profile.default_cache_ttl == DEFAULT_CACHE_TTL

    def test_missing_key_becomes_the_default(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "active_profile": "test",
                    "profiles": {"test": {"school": "myschool"}},
                }
            )
        )
        state = load_state("test", config_path=config_path)
        assert state.profile.default_cache_ttl == DEFAULT_CACHE_TTL

    def test_a_real_value_survives(self, tmp_path):
        state = self._state_with(tmp_path, 600)
        assert state.profile.default_cache_ttl == 600

    def test_a_garbage_value_falls_back_rather_than_crashing_later(
        self, tmp_path
    ):
        state = self._state_with(tmp_path, "not-a-number")
        assert state.profile.default_cache_ttl == DEFAULT_CACHE_TTL

    def test_a_bool_is_not_treated_as_an_int(self, tmp_path):
        """`True` is an int in Python, and a TTL of 1 second is never meant."""
        state = self._state_with(tmp_path, True)
        assert state.profile.default_cache_ttl == DEFAULT_CACHE_TTL

    def test_the_two_defaults_agree(self):
        """config cannot import .cache (that module imports config for
        config_dir), so the constant is duplicated. Pin them together."""
        from tahuti.cache import DEFAULT_TTL

        assert DEFAULT_CACHE_TTL == DEFAULT_TTL
