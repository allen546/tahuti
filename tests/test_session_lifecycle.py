"""The session/credential lifecycle: what is saved, when, and per profile.

One boolean, ``remember``, used to gate four unrelated things — the
``remember_me`` form field, the response cache, password storage and the session
write — and persistence had two owners, ``auth.build_client`` and
``__main__._authenticate_client``. That is how ``login --temp`` came to promise
"writes nothing to disk" while ``_relogin_from_creds`` rewrote ``session.json``
unconditionally.

The redesign: **the session is saved, the password is not, unless you ask.**
``--keep-credentials`` and ``--no-remember-me`` are two independent flags, the
cache follows ``--refresh`` alone, ``build_client`` is the only writer, and the
password file is per-profile. These tests are that contract.

Isolation: everything under ``tmp_path``, environment set inside Python via
``monkeypatch`` (a ``VAR=x`` command prefix is not usable here), and the
``remember_me`` cases assert on the POST **body** rather than on anything a
mock returned.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from tahuti import auth, keychain
from tahuti import client as client_module
from tahuti import __main__ as main_module
from tahuti.auth import _is_session_alive, build_client
from tahuti.__main__ import build_parser, main
from tahuti.config import (
    config_dir,
    load_creds,
    resolve_config_path,
    resolve_creds_path,
    resolve_session_path,
    save_creds,
    warn_on_weak_permissions,
)
from tahuti.exceptions import CommandError

# Both spellings of every path variable, so neither a current one nor a leaked
# pre-rename one from the developer's shell can reach these tests.
_ALL_PATH_ENV = (
    "MANAGEBAC_CONFIG",
    "MANAGEBAC_SESSION",
    "MANAGEBAC_CREDS_PATH",
    "MB_CRAWLER_CONFIG",
    "MB_CRAWLER_SESSION",
    "MB_CRAWLER_CREDS_PATH",
)

#: A login page carrying the one field ``login()`` needs from it.
LOGIN_PAGE = (
    '<html><body><form><input name="authenticity_token" value="csrf-token">'
    "</form></body></html>"
)

SCHOOL = "myschool"
EMAIL = "student@example.com"
PASSWORD = "not-a-real-password"


class _Response:
    """The attributes ``login()`` reads off a response."""

    def __init__(self, url: str, text: str = "", status_code: int = 200, history=()):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.history = list(history)


class FakeManageBac:
    """A ManageBac that always authenticates and records every POST body.

    Only ``_request_with_retry`` is replaced, so ``login()`` itself — the part
    that decides whether ``remember_me`` is in the body — runs for real.
    """

    def __init__(self, base: str = f"https://{SCHOOL}.managebac.com"):
        self.base = base
        self.posts: list[dict] = []
        self.reject_login = False

    @property
    def last_body(self) -> dict:
        assert self.posts, "no login POST was made"
        return self.posts[-1]

    def reject_next_login(self) -> None:
        """Make the next login POST come back rejected.

        ``login()`` reads a rejected attempt as "landed on /sessions with no
        redirect", so this is the shape a wrong password actually produces.
        """
        self.reject_login = True


def _client_classes() -> tuple[type, ...]:
    """Every distinct class object currently named ``ManageBacClient``.

    ``tests/test_client.py`` reloads ``tahuti.client`` at import time so the
    worktree's ``src`` wins over an editable install. A reload rebinds the
    module attribute to a *new* class object while ``tahuti.auth`` keeps the one
    it bound when it was first imported, so in a full run the name refers to two
    different classes. Patching only the one imported here would leave the class
    ``build_client`` instantiates talking to the real network — invisible when
    this file runs on its own, fatal in a full run, because ``test_client.py``
    sorts first. Patch every object that carries the name instead.
    """
    classes: list[type] = []
    for module in (client_module, auth, main_module):
        candidate = getattr(module, "ManageBacClient", None)
        if isinstance(candidate, type) and candidate not in classes:
            classes.append(candidate)
    return tuple(classes)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point every persisted path at *tmp_path*, including the config directory.

    ``MANAGEBAC_CREDS_PATH`` is deliberately *not* set: the per-profile
    filenames are part of what is under test, and the autouse sandbox in
    ``conftest.py`` sets that variable (under both spellings), which would
    override them. ``HOME`` is redirected to the same place by that fixture, so
    ``config_dir()`` lands inside the sandbox either way.
    """
    for var in _ALL_PATH_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def managebac():
    """Patch the HTTP layer and hand back the recorder."""
    fake = FakeManageBac()

    def _request(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/login"):
            return _Response(url, text=LOGIN_PAGE)
        if method == "POST":
            fake.posts.append(kwargs.get("data") or {})
            if fake.reject_login:
                # No Set-Cookie, and the final URL is /sessions itself.
                return _Response(f"{self.base}/sessions", status_code=200)
            # What a real server does via Set-Cookie. The domain has to match
            # the one `set_cookie` used, or requests sees two cookies with one
            # name and refuses to answer which is meant.
            self.session.cookies.set(
                "_managebac_session",
                "fresh-cookie",
                domain=f"{self.school}.{self.domain}",
            )
            return _Response(f"{self.base}/student", status_code=302)
        raise AssertionError(f"unexpected {method} {url}")

    with ExitStack() as stack:
        for cls in _client_classes():
            stack.enter_context(patch.object(cls, "_request_with_retry", _request))
        yield fake


def _write_session(cookie: str, email: str = EMAIL, profile: str = "default") -> None:
    resolve_session_path().write_text(
        json.dumps(
            {
                "version": 1,
                "active_profile": profile,
                "profiles": {
                    profile: {
                        "school": SCHOOL,
                        "domain": "managebac.com",
                        "email": email,
                        "base_url": f"https://{SCHOOL}.managebac.com",
                        "cookie": cookie,
                        "logged_in_at": "2026-09-19T00:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _write_config(*profiles: str) -> None:
    resolve_config_path().write_text(
        json.dumps(
            {
                "version": 1,
                "active_profile": "default",
                "profiles": {
                    name: {
                        "school": SCHOOL,
                        "domain": "managebac.com",
                        "email": f"{name}@example.com",
                    }
                    for name in profiles
                },
            }
        ),
        encoding="utf-8",
    )


def _saved_cookie() -> str | None:
    path = resolve_session_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("cookie"):
        return data["cookie"]
    for profile_data in (data.get("profiles") or {}).values():
        if profile_data.get("cookie"):
            return profile_data["cookie"]
    return None


def _cache_dir(email: str = EMAIL) -> Path:
    digest = hashlib.sha256(email.encode()).hexdigest()[:16]
    return Path.home() / ".config" / "tahuti" / "cache" / digest


def _no_creds_anywhere() -> bool:
    return not resolve_creds_path().exists()


# ── 1. the default: session saved, password not ───────────────────────────


class TestDefaultLogin:
    def test_writes_the_session_and_no_password(self, state_dir, managebac, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "login",
                    "--school",
                    SCHOOL,
                    "-e",
                    EMAIL,
                    "-p",
                    PASSWORD,
                    "--format",
                    "json",
                ]
            )
        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)

        # The session is the point: without it every command re-prompts.
        assert _saved_cookie() == "fresh-cookie"
        # The password is not the default thing to keep.
        assert _no_creds_anywhere()
        assert payload["data"]["credentials_saved"] is False
        assert payload["data"]["remember_me"] is True

    def test_a_cookie_login_has_nothing_to_keep(self, state_dir, managebac, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "login",
                    "--school",
                    SCHOOL,
                    "-e",
                    EMAIL,
                    "--cookie",
                    "cookie-from-elsewhere",
                    "--keep-credentials",
                    "--format",
                    "json",
                ]
            )
        assert exc_info.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["credentials_saved"] is False
        assert _no_creds_anywhere()


# ── 2. `--keep-credentials` ───────────────────────────────────────────────


class TestKeepCredentials:
    def test_writes_the_password(self, state_dir, managebac, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "login",
                    "--school",
                    SCHOOL,
                    "-e",
                    EMAIL,
                    "-p",
                    PASSWORD,
                    "--keep-credentials",
                    "--format",
                    "json",
                ]
            )
        assert exc_info.value.code == 0
        assert json.loads(capsys.readouterr().out)["data"]["credentials_saved"] is True
        assert load_creds(resolve_creds_path()) == {
            "email": EMAIL,
            "password": PASSWORD,
        }

    def test_prefers_the_keychain_when_selected(self, state_dir, managebac):
        with (
            patch.object(keychain, "enabled", return_value=True) as enabled,
            patch.object(keychain, "store", return_value=True) as store,
        ):
            build_client(
                school=SCHOOL,
                email=EMAIL,
                password=PASSWORD,
                keep_credentials=True,
                use_keychain=True,
            )
        enabled.assert_called_once_with(True)
        store.assert_called_once_with(EMAIL, PASSWORD)
        # No cleartext copy left behind, in this profile or any other.
        assert _no_creds_anywhere()

    def test_falls_back_to_the_file_when_the_keychain_will_not_store(
        self, state_dir, managebac
    ):
        """A locked keychain must not cost the user the password."""
        with (
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=False),
        ):
            build_client(
                school=SCHOOL,
                email=EMAIL,
                password=PASSWORD,
                keep_credentials=True,
                use_keychain=True,
            )
        assert load_creds(resolve_creds_path())["password"] == PASSWORD

    def test_it_decides_where_not_whether(self, state_dir, managebac):
        """`--keychain` / `--no-keychain` alone still keeps nothing."""
        for use_keychain in (True, False, None):
            build_client(
                school=SCHOOL,
                email=EMAIL,
                password=PASSWORD,
                keep_credentials=False,
                use_keychain=use_keychain,
            )
            assert _no_creds_anywhere(), f"--keychain={use_keychain} stored a password"


# ── 3. `--no-remember-me` — asserted on the POST body ─────────────────────


class TestNoRememberMe:
    def _login(self, managebac, *extra):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "login",
                    "--school",
                    SCHOOL,
                    "-e",
                    EMAIL,
                    "-p",
                    PASSWORD,
                    "--format",
                    "json",
                    *extra,
                ]
            )
        assert exc_info.value.code == 0

    def test_the_default_still_sends_remember_me_one(self, state_dir, managebac):
        self._login(managebac)
        assert managebac.last_body["remember_me"] == "1"

    def test_omits_the_field_entirely(self, state_dir, managebac):
        self._login(managebac, "--no-remember-me")
        assert "remember_me" not in managebac.last_body
        # The rest of the form is untouched.
        assert managebac.last_body["login"] == EMAIL
        assert managebac.last_body["password"] == PASSWORD

    def test_composes_with_keep_credentials(self, state_dir, managebac):
        """One flag is server-side, the other is local disk."""
        self._login(managebac, "--no-remember-me", "--keep-credentials")
        assert "remember_me" not in managebac.last_body
        assert load_creds(resolve_creds_path())["password"] == PASSWORD

    def test_alone_it_saves_no_password(self, state_dir, managebac):
        self._login(managebac, "--no-remember-me")
        assert _no_creds_anywhere()

    def test_never_touches_local_disk(self, state_dir, managebac):
        self._login(managebac, "--no-remember-me")
        assert _saved_cookie() == "fresh-cookie"

    def test_a_silent_renewal_honours_it_too(self, state_dir, managebac):
        """`_relogin_from_creds` used to hardcode ``remember=True``."""
        _write_session(cookie="dead-cookie")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        with patch("tahuti.auth._is_session_alive", return_value=False):
            build_client(school=SCHOOL, password=None, remember_me=None)

        assert "remember_me" not in managebac.last_body
        assert _saved_cookie() == "fresh-cookie"


# ── 4. a dead cookie with no password: error, no prompt, nothing written ──


class TestDeadCookieWithoutAPassword:
    def test_errors_and_writes_nothing(self, state_dir, managebac):
        _write_session(cookie="dead-cookie")
        before = resolve_session_path().read_text(encoding="utf-8")

        with (
            patch("tahuti.auth._is_session_alive", return_value=False),
            pytest.raises(CommandError) as exc_info,
        ):
            build_client(school=SCHOOL, password=None)

        assert exc_info.value.code == "missing_credentials"
        # Actionable: it names the command that makes renewal unattended.
        assert "tahuti login --keep-credentials" in exc_info.value.message
        # No prompt (getpass was never reached), no login attempt, no write.
        assert not managebac.posts, "a login was attempted with no password"
        assert resolve_session_path().read_text(encoding="utf-8") == before
        assert _no_creds_anywhere()

    def test_the_message_names_the_profile_it_looked_for(self, state_dir, managebac):
        _write_config("school")
        _write_session(cookie="dead-cookie", profile="school")

        with (
            patch("tahuti.auth._is_session_alive", return_value=False),
            pytest.raises(CommandError) as exc_info,
        ):
            build_client(school=SCHOOL, profile="school", password=None)

        assert "school" in exc_info.value.message
        assert str(resolve_creds_path()) in exc_info.value.message

    def test_the_daemon_reports_it_rather_than_dying_silently(
        self, state_dir, managebac, caplog
    ):
        """The daemon catches the error to keep running — it must still say why."""
        _write_session(cookie="dead-cookie")
        state, client, _email = build_client(school=SCHOOL, cookie="live-cookie")

        from tahuti.__main__ import _build_client  # noqa: F401  (import sanity)

        with patch("tahuti.auth._is_session_alive", return_value=False):
            with pytest.raises(CommandError) as exc_info:
                auth.refresh_session(client, state)
        assert "tahuti login --keep-credentials" in exc_info.value.message


# ── 5. a dead cookie with a password: renew, and save the new session ─────


class TestDeadCookieWithAPassword:
    def test_renews_and_persists_the_new_cookie(self, state_dir, managebac):
        _write_session(cookie="dead-cookie")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        with patch("tahuti.auth._is_session_alive", return_value=False):
            state, client, email = build_client(school=SCHOOL, password=None)

        assert email == EMAIL
        assert client.session.cookies.get("_managebac_session") == "fresh-cookie"
        assert _saved_cookie() == "fresh-cookie"
        assert state.session.email == EMAIL

    def test_the_daemons_refresh_entry_point_persists_too(self, state_dir, managebac):
        """The daemon holds its client and cannot call build_client mid-loop."""
        _write_session(cookie="dead-cookie")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        with patch("tahuti.auth._is_session_alive", return_value=False):
            state, client, _email = build_client(school=SCHOOL, password=None)
        _write_session(cookie="dead-again")  # another expiry, later

        auth.refresh_session(client, state)

        assert managebac.posts, "no re-login was attempted"
        assert _saved_cookie() == "fresh-cookie"

    def test_a_renewal_that_fails_never_writes_the_session_file(
        self, state_dir, managebac
    ):
        """Persistence has one owner, and that owner is reached only on success.

        ``_relogin_from_creds`` used to write ``session.json`` itself rather than
        reporting back to the caller that decides, so the file could be rewritten
        by a renewal that had not actually succeeded.
        """
        _write_session(cookie="still-current-cookie")
        save_creds(resolve_creds_path(), EMAIL, "wrong-password")
        # The server answers the POST by landing back on /sessions, which is how
        # `login()` reports rejected credentials.
        managebac.reject_next_login()

        with patch("tahuti.auth._is_session_alive", return_value=False):
            with patch("tahuti.auth.save_session") as save_session:
                with pytest.raises(CommandError) as exc_info:
                    build_client(school=SCHOOL, password=None)

        assert exc_info.value.code == "authentication_failed"
        save_session.assert_not_called()
        assert _saved_cookie() == "still-current-cookie"


# ── 6. the cache follows `--refresh` alone ────────────────────────────────


class TestCachePolicy:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"keep_credentials": True},
            {"remember_me": None},
            {"keep_credentials": True, "remember_me": None},
        ],
        ids=["default", "keep-credentials", "no-remember-me", "both"],
    )
    def test_enabled_whatever_the_new_flags_say(self, state_dir, managebac, kwargs):
        _state, client, _email = build_client(
            school=SCHOOL, email=EMAIL, password=PASSWORD, **kwargs
        )
        assert client.cache.enabled is True

    def test_refresh_still_disables_it(self, state_dir, managebac):
        _state, client, _email = build_client(
            school=SCHOOL, email=EMAIL, password=PASSWORD, refresh=True
        )
        assert client.cache.enabled is False

    def test_logout_clears_the_cache(self, state_dir, capsys):
        """The cache holds grade pages and the hub JWT, so logout empties it."""
        _write_session(cookie="live-cookie")
        cache = _cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "entry.json").write_text("{}", encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            main(["logout", "--format", "json"])
        assert exc_info.value.code == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["cache_entries_removed"] == 1
        assert list(cache.glob("*.json")) == []


# ── 7. credentials management ─────────────────────────────────────────────


class TestCredentialsManagement:
    def test_default_creds_path(self, state_dir):
        assert resolve_creds_path().name == "creds.json"
        assert resolve_creds_path() == config_dir() / "creds.json"

    def test_save_and_load_creds(self, state_dir):
        _write_config("default")
        creds = resolve_creds_path()
        save_creds(creds, "home@example.com", "home-pw")
        loaded = load_creds(creds)
        assert loaded is not None
        assert loaded["password"] == "home-pw"
        assert loaded["email"] == "home@example.com"

    def test_logging_out_clears_credentials(self, state_dir, capsys):
        _write_config("default")
        _write_session(cookie="live-cookie")
        creds = resolve_creds_path()
        save_creds(creds, "home@example.com", "home-pw")

        with pytest.raises(SystemExit) as exc_info:
            main(["logout", "--format", "json"])
        assert exc_info.value.code == 0

        assert not creds.exists(), "credentials survived logout"
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["credentials_removed"] is True

    def test_logout_clears_creds_at_custom_path(self, state_dir, monkeypatch, capsys):
        """MANAGEBAC_CREDS_PATH can name a file outside config_dir()."""
        elsewhere = state_dir / "elsewhere-creds.json"
        monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(elsewhere))
        save_creds(elsewhere, "elsewhere@example.com", "pw")

        with pytest.raises(SystemExit):
            main(["logout", "--format", "json"])
        assert not elsewhere.exists()

    def test_explicit_path_passed_to_resolve_creds_path(self, state_dir):
        custom = state_dir / "custom.json"
        assert resolve_creds_path(custom) == custom


# ── 8. credentials loading ────────────────────────────────────────────────


class TestCredentialsLoading:
    def test_load_creds_from_resolved_path(self, state_dir):
        creds = resolve_creds_path()
        save_creds(creds, "student@example.com", "pw123")
        loaded = auth._load_creds()
        assert loaded is not None
        assert loaded["email"] == "student@example.com"
        assert loaded["password"] == "pw123"

    def test_load_creds_returns_none_when_missing(self, state_dir):
        assert auth._load_creds() is None

    def test_load_creds_with_custom_env(self, state_dir, monkeypatch):
        custom = state_dir / "custom.json"
        monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(custom))
        save_creds(custom, "custom@example.com", "custom-pw")
        loaded = auth._load_creds()
        assert loaded is not None
        assert loaded["email"] == "custom@example.com"
        assert loaded["password"] == "custom-pw"


# ── 9. the MANAGEBAC_* rename ─────────────────────────────────────────────

_PATH_RESOLVERS = {
    "MANAGEBAC_CONFIG": resolve_config_path,
    "MANAGEBAC_SESSION": resolve_session_path,
    "MANAGEBAC_CREDS_PATH": resolve_creds_path,
}
_LEGACY_NAMES = {
    "MANAGEBAC_CONFIG": "MB_CRAWLER_CONFIG",
    "MANAGEBAC_SESSION": "MB_CRAWLER_SESSION",
    "MANAGEBAC_CREDS_PATH": "MB_CRAWLER_CREDS_PATH",
    "MANAGEBAC_KEYCHAIN": "MB_CRAWLER_KEYCHAIN",
    "MANAGEBAC_NO_PERM_WARN": "MB_CRAWLER_NO_PERM_WARN",
}


class _Stream:
    """A stdout/stderr stand-in that records what was written to it."""

    def __init__(self):
        self.text = ""

    def write(self, value: str) -> int:
        self.text += value
        return len(value)

    def flush(self) -> None:
        pass


class TestEnvVarRename:
    @pytest.mark.parametrize("new_name", sorted(_PATH_RESOLVERS))
    def test_the_new_name_wins_when_both_are_set(self, monkeypatch, tmp_path, new_name):
        resolver = _PATH_RESOLVERS[new_name]
        monkeypatch.setenv(new_name, str(tmp_path / "new.json"))
        monkeypatch.setenv(_LEGACY_NAMES[new_name], str(tmp_path / "old.json"))
        assert resolver() == tmp_path / "new.json"

    @pytest.mark.parametrize("new_name", sorted(_PATH_RESOLVERS))
    def test_the_deprecated_name_still_works_alone(self, monkeypatch, tmp_path, new_name):
        resolver = _PATH_RESOLVERS[new_name]
        monkeypatch.delenv(new_name, raising=False)
        monkeypatch.setenv(_LEGACY_NAMES[new_name], str(tmp_path / "old.json"))
        assert resolver() == tmp_path / "old.json"

    def test_the_new_keychain_name_wins(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/secret-tool"])
        monkeypatch.setenv("MANAGEBAC_KEYCHAIN", "0")
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        assert keychain.enabled() is False

    def test_the_deprecated_keychain_name_still_works(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/secret-tool"])
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        assert keychain.enabled() is True

    def test_the_new_permission_warning_name_wins(self, tmp_path, monkeypatch):
        _loose_session(tmp_path, monkeypatch)
        monkeypatch.setenv("MANAGEBAC_NO_PERM_WARN", "1")
        monkeypatch.setenv("MB_CRAWLER_NO_PERM_WARN", "0")
        stream = _Stream()
        # Detection still reports the file; only the printing is silenced.
        assert warn_on_weak_permissions(stream=stream)
        assert stream.text == ""

    def test_the_deprecated_permission_warning_name_still_works(
        self, tmp_path, monkeypatch
    ):
        _loose_session(tmp_path, monkeypatch)
        monkeypatch.setenv("MB_CRAWLER_NO_PERM_WARN", "1")
        stream = _Stream()
        assert warn_on_weak_permissions(stream=stream)
        assert stream.text == ""

    def test_neither_name_set_still_prints_the_warning(self, tmp_path, monkeypatch):
        _loose_session(tmp_path, monkeypatch)
        stream = _Stream()
        assert warn_on_weak_permissions(stream=stream)
        assert "is mode 0644" in stream.text

    @pytest.mark.parametrize(
        "new_name,legacy_name", [("MANAGEBAC_PASSWORD", "MB_CRAWLER_PASSWORD")]
    )
    def test_the_new_password_name_wins(self, state_dir, monkeypatch, new_name, legacy_name):
        captured = _read_secret_env(
            monkeypatch, {new_name: "env-new", legacy_name: "env-old"}
        )
        assert captured["password"] == "env-new"

    def test_the_deprecated_password_name_still_works(self, state_dir, monkeypatch):
        captured = _read_secret_env(
            monkeypatch, {"MB_CRAWLER_PASSWORD": "env-old"}
        )
        assert captured["password"] == "env-old"

    def test_the_deprecated_cookie_name_still_works(self, state_dir, monkeypatch):
        captured = _read_secret_env(monkeypatch, {"MB_CRAWLER_COOKIE": "env-old"})
        assert captured["cookie"] == "env-old"

    def test_the_new_cookie_name_wins(self, state_dir, monkeypatch):
        captured = _read_secret_env(
            monkeypatch,
            {"MANAGEBAC_COOKIE": "env-new", "MB_CRAWLER_COOKIE": "env-old"},
        )
        assert captured["cookie"] == "env-new"

    def test_the_daemon_child_receives_the_new_name(self, state_dir):
        from tahuti.__main__ import cmd_daemon_start

        args = build_parser().parse_args(
            ["daemon", "start", "-b", "--password", "pw123", "--cookie", "cookieval"]
        )
        with patch(
            "tahuti.daemon.system.ServiceManager.start_background",
            return_value={"started": True},
        ) as start:
            assert cmd_daemon_start(args) == 0
        _, kwargs = start.call_args
        assert kwargs["env"]["MANAGEBAC_PASSWORD"] == "pw123"
        assert kwargs["env"]["MANAGEBAC_COOKIE"] == "cookieval"
        # Never argv: `ps` would show it for the life of the daemon.
        assert "pw123" not in " ".join(kwargs["extra_args"])


def _loose_session(tmp_path, monkeypatch) -> Path:
    """A session file at 0644, the condition the warning exists for."""
    loose = tmp_path / "session.json"
    loose.write_text("{}", encoding="utf-8")
    os.chmod(loose, 0o644)
    monkeypatch.setenv("MANAGEBAC_SESSION", str(loose))
    monkeypatch.delenv("MANAGEBAC_CREDS_PATH", raising=False)
    return loose


def _read_secret_env(monkeypatch, values: dict) -> dict:
    """What ``_build_client`` resolves the password/cookie environment to."""
    from tahuti.__main__ import _build_client

    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in _LEGACY_NAMES.values():
        if name not in values and name.startswith("MB_CRAWLER_") and name not in (
            "MB_CRAWLER_KEYCHAIN",
            "MB_CRAWLER_NO_PERM_WARN",
        ):
            monkeypatch.delenv(name, raising=False)
    for name in ("MANAGEBAC_PASSWORD", "MANAGEBAC_COOKIE"):
        if name not in values:
            monkeypatch.delenv(name, raising=False)

    args = build_parser().parse_args(["list", "--format", "json"])
    captured: dict = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        raise SystemExit(0)

    state = MagicMock()
    state.session.cookie = None
    with (
        patch("tahuti.__main__.build_client", fake_build),
        patch("tahuti.__main__.load_state", return_value=state),
    ):
        with pytest.raises(SystemExit):
            _build_client(args, "list")
    return captured


# ── 10. an environment password is input, not a request to keep it ────────


class TestEnvPasswordIsNotPersisted:
    """The regression: ``MANAGEBAC_PASSWORD=... tahuti list`` used to write that
    password into the creds file, because it reached the one ``_store_password``
    call site. Under the new default the password is input, not a request to
    keep it."""

    @pytest.mark.parametrize(
        "env_var", ["MANAGEBAC_PASSWORD", "MB_CRAWLER_PASSWORD"]
    )
    def test_it_authenticates_but_is_never_written_to_disk(
        self, state_dir, managebac, capsys, monkeypatch, env_var
    ):
        monkeypatch.setenv(env_var, PASSWORD)
        with pytest.raises(SystemExit) as exc_info:
            main(["login", "--school", SCHOOL, "-e", EMAIL, "--format", "json"])
        assert exc_info.value.code == 0

        # It really was used — the POST body carries it.
        assert managebac.last_body["password"] == PASSWORD
        # And it went nowhere near disk.
        assert _no_creds_anywhere()
        assert json.loads(capsys.readouterr().out)["data"]["credentials_saved"] is False


# ── 5. the health check must be cheap ────────────────────────────────────


class TestHealthCheckUsesHEAD:
    """`_is_session_alive` reads a status and a Location, not a body.

    A GET of /student/dashboard costs ~275 KB of decompressed body on every
    invocation that reuses a saved cookie. HEAD answers the same question for
    zero bytes.
    """

    @staticmethod
    def _client(method_status):
        client = MagicMock()
        client.base = "https://myschool.managebac.cn"
        client.session.head.side_effect = lambda u, **kw: _resp(
            method_status.get("HEAD"), kw
        )
        client.session.get.side_effect = lambda u, **kw: _resp(
            method_status.get("GET"), kw
        )
        return client

    def test_it_issues_head_not_get(self, state_dir):
        client = self._client({"HEAD": 200})
        assert _is_session_alive(client) is True
        client.session.head.assert_called_once()
        client.session.get.assert_not_called()

    def test_head_is_not_redirected(self, state_dir):
        """The Location header is the whole answer, so redirects must not be
        followed — same contract the GET version had."""
        client = self._client({"HEAD": 302})
        client.session.head.side_effect = lambda u, **kw: _resp(
            302, kw, location="https://myschool.managebac.cn/login"
        )
        assert _is_session_alive(client) is False

    def test_head_302_not_to_login_counts_as_alive(self, state_dir):
        client = self._client({})
        client.session.head.side_effect = lambda u, **kw: _resp(
            302, kw, location="https://myschool.managebac.cn/student"
        )
        assert _is_session_alive(client) is True

    def test_head_401_means_dead(self, state_dir):
        client = self._client({"HEAD": 401})
        assert _is_session_alive(client) is False

    def test_head_403_means_dead(self, state_dir):
        client = self._client({"HEAD": 403})
        assert _is_session_alive(client) is False

    def test_an_unexpected_status_falls_back_to_get(self, state_dir):
        """A host without HEAD answers 405. One extra request beats a guess."""
        client = self._client({"HEAD": 405, "GET": 200})
        assert _is_session_alive(client) is True
        assert client.session.head.call_count == 1
        assert client.session.get.call_count == 1

    def test_a_head_transport_error_falls_back_to_get(self, state_dir):
        """A proxy that mishandles HEAD must not cost a needless re-login."""
        client = MagicMock()
        client.base = "https://myschool.managebac.cn"

        def flaky(url, **kw):
            raise requests.ConnectionError("proxy said no")

        client.session.head.side_effect = flaky
        client.session.get.side_effect = lambda u, **kw: _resp(200, kw)
        assert _is_session_alive(client) is True
        assert client.session.head.call_count == 1
        assert client.session.get.call_count == 1

    def test_a_get_transport_error_is_still_false(self, state_dir):
        client = MagicMock()
        client.base = "https://myschool.managebac.cn"
        client.session.head.side_effect = requests.ConnectionError("down")
        client.session.get.side_effect = requests.ConnectionError("down")
        assert _is_session_alive(client) is False

    def test_an_unreadable_get_status_is_still_alive(self, state_dir):
        """Pinned, not endorsed: an unreadable *GET* status counts as alive.

        Only the HEAD arm ever fell back, so a GET answering 405 reached the
        trailing `return True`. That is the verdict the loop gave and the
        unrolled version keeps it — but it is a guess, and the one thing here
        that is arguably wrong rather than merely undocumented. If a future
        change decides 405 from the GET means "dead", this is the test that
        has to change with it.
        """
        client = self._client({"HEAD": 405, "GET": 405})
        assert _is_session_alive(client) is True
        assert client.session.get.call_count == 1


# ── 11. the status sets are derived, not restated ────────────────────────


class TestStatusSetsAreDerived:
    """The redirect set used to be spelled three times and pinned nowhere.

    ``client._REDIRECT_STATUSES``, the same five statuses inlined inside
    ``auth._SESSION_ALIVE_STATUSES``, and the same five again as a bare tuple
    in the verdict. No test referenced any of them by name, so nothing noticed
    if they drifted — a status added to the client's set but not the other two
    would have made the health check read a redirect as "unreadable, try a GET"
    and then fall through to True without ever looking at ``Location``.

    ``_is_session_alive`` now derives both from the client's set, so drift
    inside auth.py is structurally impossible. These tests are what make it
    stay impossible: they fail if either constant is ever restated by hand.
    """

    def test_the_alive_set_is_the_redirect_set_plus_three(self):
        assert auth._SESSION_ALIVE_STATUSES == (
            client_module._REDIRECT_STATUSES | {200, 401, 403}
        )

    @pytest.mark.parametrize("status", sorted(client_module._REDIRECT_STATUSES))
    def test_every_redirect_status_is_read_as_a_redirect(self, status):
        """Each member of the set must actually drive the redirect verdict.

        This is the half the set comparison above cannot see: the sets can be
        equal while the *code* still ignores a member. A 3xx that is not in
        ``_SESSION_ALIVE_STATUSES`` never reaches the verdict at all — HEAD
        falls back to GET, and the GET's answer for the same status falls
        through to True — so a member missing from the alive set silently stops
        being checked against ``Location``.
        """
        assert status in auth._SESSION_ALIVE_STATUSES

        to_login = MagicMock()
        to_login.base = "https://myschool.managebac.cn"
        to_login.session.head.side_effect = lambda u, **kw: _resp(
            status, kw, location="https://myschool.managebac.cn/login?next=/x"
        )
        assert _is_session_alive(to_login) is False
        to_login.session.get.assert_not_called()

        elsewhere = MagicMock()
        elsewhere.base = "https://myschool.managebac.cn"
        elsewhere.session.head.side_effect = lambda u, **kw: _resp(
            status, kw, location="https://myschool.managebac.cn/student"
        )
        assert _is_session_alive(elsewhere) is True
        elsewhere.session.get.assert_not_called()

    @pytest.mark.parametrize("status", [200, 401, 403])
    def test_the_three_non_redirect_members_are_not_redirects(self, status):
        """The other direction: these three must not be read as redirects.

        If one of them ever joined ``_REDIRECT_STATUSES``, the verdict would
        start consulting a ``Location`` header that a 200/401/403 does not
        carry, and the answer would come from whatever happened to be there.
        """
        assert status not in client_module._REDIRECT_STATUSES

        client = MagicMock()
        client.base = "https://myschool.managebac.cn"
        # A Location header that would say "alive" if it were consulted.
        client.session.head.side_effect = lambda u, **kw: _resp(
            status, kw, location="https://myschool.managebac.cn/student"
        )
        assert _is_session_alive(client) is (status == 200)
        client.session.get.assert_not_called()


def _resp(status, kwargs, location=None):
    """A minimal stand-in for a requests.Response.

    `_is_session_alive` reads only status_code and headers, so that is all this
    needs to provide.
    """
    r = MagicMock()
    r.status_code = status
    r.headers = {"Location": location} if location else {}
    assert kwargs.get("allow_redirects") is False
    return r
