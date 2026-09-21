"""`logout --purge`, and a domain that can actually be absent.

Two defects, one file, because the second is what made the first incomplete.

**1. `logout` left the school behind.** ``cmd_logout`` cleared the session, the
response cache and the credential files, but never the ``profiles.<name>``
entry in ``config.json`` — so a machine that had just been logged out still
remembered which school it belonged to, and the next command silently
re-authenticated against it. ``--purge`` now removes that entry wholesale:
school, domain, email *and* the ``defaults`` block. ``--purge
--keep-credentials`` is refused rather than silently doing one or the other.

**2. A domain was never absent.** ``ProfileConfig.domain`` and
``SessionConfig.domain`` both defaulted to the string ``"managebac.com"``, so
the "only ask for what is unknown" rule in ``_prompt_login_setup`` could never
fire for the domain and every interactive ``login`` re-asked it. Both now
default to ``None``; ``auth.build_client`` is the one place the string default
lives; and an absent key in ``config.json`` and an explicit ``null`` are the
same thing.

Isolation: the autouse ``isolated_user_state`` sandbox in ``conftest.py``
redirects ``$HOME`` and every ``MANAGEBAC_*`` path into ``tmp_path``, so nothing
here can reach the operator's real ``~/.config/tahuti``. The ``state_dir``
fixture below additionally clears ``MANAGEBAC_CREDS_PATH``, because the
per-profile credential filenames are part of what ``logout`` is under test for.
"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from io import StringIO
from unittest.mock import patch

import pytest

from tahuti import __main__ as m
from tahuti import auth
from tahuti import client as client_module
from tahuti import keychain
from tahuti.__main__ import build_parser, main
from tahuti.config import (
    DEFAULT_PROFILE_NAME,
    load_state,
    resolve_config_path,
    resolve_creds_path,
    resolve_session_path,
    save_creds,
    save_profile,
    save_session,
)

#: Both spellings of every path variable, so neither a current one nor a leaked
#: pre-rename one from the developer's shell can reach these tests.
_ALL_PATH_ENV = (
    "MANAGEBAC_CONFIG",
    "MANAGEBAC_SESSION",
    "MANAGEBAC_CREDS_PATH",
    "MB_CRAWLER_CONFIG",
    "MB_CRAWLER_SESSION",
    "MB_CRAWLER_CREDS_PATH",
)

SCHOOL = "myschool"
EMAIL = "student@example.com"
PASSWORD = "not-a-real-password"

#: A login page carrying the one field ``login()`` needs from it.
LOGIN_PAGE = (
    '<html><body><form><input name="authenticity_token" value="csrf-token">'
    "</form></body></html>"
)


class _Response:
    """The attributes ``login()`` reads off a response."""

    def __init__(self, url: str, text: str = "", status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.history: list = []


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point every persisted path at *tmp_path*, including the config directory.

    ``MANAGEBAC_CREDS_PATH`` is deliberately *not* set: the per-profile
    filenames are part of what is under test, and the autouse sandbox sets that
    variable, which would override them.
    """
    for var in _ALL_PATH_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _client_classes() -> tuple[type, ...]:
    """Every distinct class object currently named ``ManageBacClient``.

    ``tests/test_client.py`` reloads ``tahuti.client`` at import time, and in a
    full run ``tahuti.client`` is already in ``sys.modules`` by then — so the
    reload rebinds the module attribute to a *new* class object while
    ``tahuti.auth`` keeps the one it bound when it was first imported. The name
    then refers to two different classes. Patching only the one imported here
    leaves the class ``build_client`` actually instantiates talking to the real
    network — invisible when this file runs alone, fatal in a full run, because
    ``test_client.py`` sorts first. This is the same hazard, and the same fix,
    that ``tests/test_session_lifecycle.py`` documents.
    """
    classes: list[type] = []
    for module in (client_module, auth, m):
        candidate = getattr(module, "ManageBacClient", None)
        if isinstance(candidate, type) and candidate not in classes:
            classes.append(candidate)
    return tuple(classes)


@pytest.fixture()
def managebac():
    """Patch the HTTP layer so ``build_client`` logs in for real, offline."""
    posts: list[dict] = []

    def _request(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/login"):
            return _Response(url, text=LOGIN_PAGE)
        if method == "POST":
            posts.append(kwargs.get("data") or {})
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
        yield posts


def _write_config(*profiles: str, domain: str | None = None, active: str = "default"):
    """Persist a real config.json, going through ``save_profile`` for each."""
    for name in profiles:
        state = load_state(name)
        state.profile.school = SCHOOL
        state.profile.domain = domain
        state.profile.email = f"{name}@example.com"
        save_profile(state)
    if active is not None:
        path = resolve_config_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["active_profile"] = active
        path.write_text(json.dumps(data), encoding="utf-8")


def _write_session(profile: str = "default", cookie: str = "live-cookie") -> None:
    resolve_session_path().write_text(
        json.dumps(
            {
                "version": 1,
                "active_profile": profile,
                "profiles": {
                    profile: {
                        "school": SCHOOL,
                        "domain": None,
                        "email": EMAIL,
                        "base_url": f"https://{SCHOOL}.managebac.com",
                        "cookie": cookie,
                        "logged_in_at": "2026-09-19T00:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _config_json() -> dict:
    return json.loads(resolve_config_path().read_text(encoding="utf-8"))


def _logout(*argv: str):
    """Run `logout`, returning the SystemExit code and the parsed payload."""
    buffer = StringIO()
    with patch.object(sys, "stdout", buffer):
        with pytest.raises(SystemExit) as exc_info:
            main(["logout", "--format", "json", *argv])
    return exc_info.value.code, json.loads(buffer.getvalue())


# ── 1. `logout --purge` removes the profile entry ─────────────────────────


class TestLogoutPurge:
    def test_plain_logout_leaves_the_profile_entry_intact(self, state_dir):
        """The base form clears credentials, not the school the profile names."""
        _write_config("default", active="default")
        _write_session("default")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        code, payload = _logout()
        assert code == 0
        assert payload["data"]["credentials_removed"] is True
        assert payload["data"]["credential_files_removed"] == [
            str(resolve_creds_path())
        ]
        assert payload["data"]["credentials_kept"] is False
        assert not resolve_creds_path().exists()

        cfg = _config_json()
        assert cfg.get("school") == SCHOOL or (cfg.get("profiles") and "default" in cfg["profiles"])

    def test_keep_credentials_clears_neither_but_still_clears_the_session(
        self, state_dir, capsys
    ):
        _write_config("school", active="school")
        _write_session("school")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        code, payload = _logout("--keep-credentials")
        assert code == 0
        assert payload["data"]["credentials_removed"] is False
        assert payload["data"]["credential_files_removed"] == []
        assert payload["data"]["credentials_kept"] is True
        assert payload["data"]["keychain_entry_removed"] is False
        assert resolve_creds_path().exists(), "the password was deleted"

        # The session is the base of every form, including this one.
        assert not resolve_session_path().exists()
        assert payload["data"]["logged_out"] is True

        # And the profile entry is untouched.
        cfg = _config_json()
        assert cfg.get("school") == SCHOOL or "school" in cfg.get("profiles", {})

    def test_purge_removes_the_whole_entry_from_config_json(self, state_dir):
        """The key is gone, not merely emptied — school, domain, email, defaults."""
        _write_config("school", active="school")
        _write_session("school")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        before = _config_json()
        assert before.get("school") == SCHOOL or before["profiles"]["school"]["school"] == SCHOOL

        code, payload = _logout("--purge")
        assert code == 0

        after = _config_json()
        assert "school" not in after
        assert "email" not in after
        assert "defaults" not in after
        assert not after.get("profiles")

        # Credentials went too, because --purge implies them.
        assert not resolve_creds_path().exists()
        assert payload["data"]["credentials_removed"] is True

    def test_purge_drops_the_active_profile_pointer(self, state_dir):
        """Otherwise the next command would still resolve to the deleted name."""
        _write_config("school", active="school")
        _write_session("school")

        _logout("--purge")

        data = _config_json()
        assert data.get("active_profile") != "school"
        # Falling back to the default profile name is the documented outcome.
        assert load_state().active_profile == DEFAULT_PROFILE_NAME

    def test_purge_clears_the_keychain_entry(self, state_dir):
        """`--purge` implies the credential deletion, keychain included.

        `keychain.delete` shells out to the OS helper, so it is stubbed — but
        the *call* and the reported outcome are the real ones.
        """
        _write_config("school", active="school")
        _write_session("school")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        with patch.object(keychain, "delete", return_value=True) as deleted:
            code, payload = _logout("--purge")

        assert code == 0
        # `_login_email` resolves profile-then-session, and the profile's email
        # is the one the keychain item is filed under.
        deleted.assert_called_once_with("school@example.com")
        assert payload["data"]["keychain_entry_removed"] is True

    def test_purge_reports_what_it_removed(self, state_dir):
        _write_config("school", active="school")
        _write_session("school")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)

        code, payload = _logout("--purge")
        assert code == 0
        data = payload["data"]
        # Existing keys keep their meaning.
        assert data["logged_out"] is True
        assert data["all_profiles"] is False
        assert data["cache_entries_removed"] is not None
        assert data["credentials_removed"] is True
        assert data["credential_files_removed"] == [str(resolve_creds_path())]
        assert data["keychain_entry_removed"] is False
        assert data["credentials_kept"] is False
        # New keys describe what --purge did.
        assert data["profile_purged"] is True
        assert data["profile_entry_removed"] is True
        assert data["profiles_purged"] == ["school"]

    def test_purge_without_a_saved_entry_reports_it_honestly(self, state_dir):
        """`--purge` on a profile that was never configured is not a failure."""
        _write_session("default")

        code, payload = _logout("--purge")
        assert code == 0
        assert payload["data"]["profile_purged"] is True

    def test_a_dangling_active_profile_pointer_is_cleaned_up(self, state_dir):
        """A pointer to a profile that is already gone must not be left behind.

        Otherwise the next command resolves to the missing name and reports an
        empty profile as though it were configured.
        """
        path = resolve_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"version": 1, "active_profile": "ghost", "profiles": {}}
            ),
            encoding="utf-8",
        )

        code, payload = _logout("--purge")
        assert code == 0
        data = _config_json()
        assert "active_profile" not in data
        assert load_state().active_profile == DEFAULT_PROFILE_NAME

    def test_purge_and_keep_credentials_is_refused(self, state_dir):
        """The flags contradict each other; one must not silently win."""
        _write_config("school", active="school")
        _write_session("school")
        save_creds(resolve_creds_path(), EMAIL, PASSWORD)
        config_before = _config_json()

        code, payload = _logout("--purge", "--keep-credentials")
        assert code == 1
        assert payload["ok"] is False
        assert payload["command"] == "logout"
        assert payload["error"]["code"] == "conflicting_flags"
        assert "--purge" in payload["error"]["message"]
        assert "--keep-credentials" in payload["error"]["message"]

        # Nothing was touched: the refusal happens before any clearing.
        assert _config_json() == config_before
        assert resolve_creds_path().exists()

    def test_purge_cleans_legacy_profiles_config(self, state_dir):
        """Legacy profiles map is removed on purge, leaving a clean loadable config."""
        path = resolve_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "version": 1,
                "profiles": {
                    "default": {"school": "s1", "email": "e1@example.com"},
                    "other": {"school": "s2", "email": "e2@example.com"},
                },
                "active_profile": "default",
            }),
            encoding="utf-8",
        )
        save_creds(resolve_creds_path(), "e1@example.com", "pw")

        code, payload = _logout("--purge")
        assert code == 0

        data = _config_json()
        assert "profiles" not in data
        assert "active_profile" not in data

        state = load_state()
        assert state.active_profile == DEFAULT_PROFILE_NAME
        assert state.profile.school is None
        assert not resolve_creds_path().exists()


# ── 2. a domain that can be absent ────────────────────────────────────────


class TestDomainUnset:
    def test_no_files_means_no_domain(self, state_dir):
        """`None`, not the string default — that is the whole point."""
        state = load_state()
        assert state.profile.domain is None
        assert state.session.domain is None

    def test_an_explicit_null_is_the_same_as_an_absent_key(self, state_dir):
        """`dict.get(key, default)` only substitutes for an *absent* key.

        A hand-edited or schema-defaulted config can carry `"domain": null`,
        which must reach `ProfileConfig` as `None` exactly like a missing key
        does — otherwise "unset" stops being representable.
        """
        path = resolve_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_profile": "default",
                    "profiles": {
                        "default": {
                            "school": SCHOOL,
                            "domain": None,
                            "email": EMAIL,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        state = load_state()
        assert state.profile.domain is None
        # The session falls back to the profile, and both are None.
        assert state.session.domain is None

    def test_save_profile_writes_a_null_and_it_round_trips(self, state_dir):
        state = load_state()
        save_profile(state)

        stored = _config_json()
        assert stored.get("domain") is None
        assert "domain" in stored, "the key was dropped rather than stored as null"

        assert load_state().profile.domain is None

    def test_save_session_writes_a_null_and_it_round_trips(self, state_dir):
        """`_write_json` is a plain json.dumps, so None serialises as null.

        Nothing downstream chokes on it: `load_state` reads the key back as
        None, which is the "unset" the whole change is about.
        """
        state = load_state()
        save_session(state)

        session = json.loads(resolve_session_path().read_text(encoding="utf-8"))
        assert session.get("domain") is None
        assert "domain" in session
        assert load_state().session.domain is None

    def test_a_saved_domain_survives_the_round_trip(self, state_dir):
        state = load_state()
        state.profile.domain = "managebac.cn"
        save_profile(state)
        assert _config_json()["domain"] == "managebac.cn"
        assert load_state().profile.domain == "managebac.cn"

    def test_session_domain_falls_back_to_the_profile(self, state_dir):
        """`config.py`'s session read falls back to the profile, and still works.

        Note *when* that fallback can fire: ``save_session`` writes the
        ``domain`` key unconditionally, so a session file this package wrote
        always carries one and the ``dict.get("domain", profile.domain)``
        default is reached only by a file that predates the key. That is the
        same shape as the config read and is left as it was.
        """
        state = load_state()
        state.profile.domain = "managebac.cn"
        state.profile.school = SCHOOL
        save_profile(state)

        # A session file with no domain key at all.
        resolve_session_path().parent.mkdir(parents=True, exist_ok=True)
        resolve_session_path().write_text(
            json.dumps({"version": 1, "profiles": {"default": {"cookie": "c"}}}),
            encoding="utf-8",
        )
        assert load_state().session.domain == "managebac.cn"

    def test_both_domains_unset_stays_unset(self, state_dir):
        """The fallback must not resurrect a default when the profile has none."""
        state = load_state()
        save_profile(state)
        resolve_session_path().parent.mkdir(parents=True, exist_ok=True)
        resolve_session_path().write_text(
            json.dumps({"version": 1, "profiles": {"default": {"cookie": "c"}}}),
            encoding="utf-8",
        )
        loaded = load_state()
        assert loaded.profile.domain is None
        assert loaded.session.domain is None

    def test_build_client_resolves_the_builtin_default(self, state_dir, managebac):
        """auth.py:243 is the single place the string default lives."""
        assert load_state().profile.domain is None, "no domain was configured"
        state, client, _email = auth.build_client(
            school=SCHOOL, email=EMAIL, password=PASSWORD
        )
        assert client.domain == "managebac.com"
        assert client.base == "https://myschool.managebac.com"
        # The resolved default is what gets persisted, so the next run — and the
        # next `login` prompt — sees a real domain rather than asking again.
        assert state.profile.domain == "managebac.com"

    def test_a_saved_domain_still_wins_over_the_builtin_default(
        self, state_dir, managebac
    ):
        _write_config("school", domain="managebac.cn", active="school")
        state, client, _email = auth.build_client(
            school=SCHOOL, email=EMAIL, password=PASSWORD, profile="school"
        )
        assert client.domain == "managebac.cn"
        assert client.base == "https://myschool.managebac.cn"

    def test_an_explicit_flag_still_wins_over_everything(self, state_dir, managebac):
        _write_config("school", domain="managebac.com", active="school")
        _state, client, _email = auth.build_client(
            school=SCHOOL,
            domain="managebac.cn",
            email=EMAIL,
            password=PASSWORD,
            profile="school",
        )
        assert client.domain == "managebac.cn"

    def test_no_domain_cannot_reach_the_client_unresolved(self, state_dir, managebac):
        """`_validate_school_domain` raises invalid_domain on None.

        The only construction site is auth.py:274, behind the auth.py:243
        resolution — so a None domain is unreachable there, which is what this
        asserts by building a client with nothing configured at all.
        """
        from tahuti.exceptions import CommandError

        with pytest.raises(CommandError) as exc_info:
            client_module.ManageBacClient(SCHOOL, domain=None)
        assert exc_info.value.code == "invalid_domain"

        # ...and the real path never gets there.
        _state, client, _email = auth.build_client(
            school=SCHOOL, email=EMAIL, password=PASSWORD
        )
        assert client.domain in ("managebac.com", "managebac.cn")


# ── `login` asks for the domain only when nothing supplies one ────────────


class TestLoginDomainPrompt:
    def _login_args(self, *extra):
        return build_parser().parse_args(["login", "--format", "json", *extra])

    def _run(self, args, answers):
        """Drive the prompts with *answers*, returning the sequence asked."""
        asked = []

        def fake_input(prompt=""):
            asked.append(prompt)
            return answers[len(asked) - 1] if len(asked) <= len(answers) else ""

        with (
            patch.object(m, "_stdin_is_interactive", return_value=True),
            patch("builtins.input", side_effect=fake_input),
        ):
            m._prompt_login_setup(args)
        return asked

    def test_a_configured_profile_is_not_asked_for_the_domain(self, state_dir):
        """The defect: a machine that already knows its domain was asked anyway."""
        _write_config("school", domain="managebac.cn", active="school")
        _write_session("school")

        args = self._login_args()
        asked = self._run(args, [])
        assert asked == [], f"prompted for {asked!r} despite a saved domain"
        assert args.domain is None

    def test_a_fresh_device_is_still_asked_for_the_domain(self, state_dir):
        """Nothing supplies one, so the question is the only way to get it."""
        args = self._login_args()
        asked = self._run(args, ["", "myschool", "student@example.com"])
        assert asked == [
            "Base domain [managebac.com]: ",
            "School subdomain (e.g. myschool): ",
            "Email: ",
        ]
        assert args.domain == "managebac.com"  # empty means the default

    def test_a_session_only_domain_suppresses_the_prompt(self, state_dir):
        """The session file carries the domain too; it counts as known."""
        resolve_session_path().parent.mkdir(parents=True, exist_ok=True)
        resolve_session_path().write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_profile": "default",
                    "profiles": {
                        "default": {
                            "school": SCHOOL,
                            "domain": "managebac.cn",
                            "email": EMAIL,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        args = self._login_args()
        asked = self._run(args, [])
        assert asked == []

    def test_an_explicit_flag_suppresses_the_prompt(self, state_dir):
        args = self._login_args("--domain", "managebac.cn")
        asked = self._run(args, ["myschool", "student@example.com"])
        assert asked == ["School subdomain (e.g. myschool): ", "Email: "]
        assert args.domain == "managebac.cn"

    def test_empty_input_adopts_the_shown_default(self, state_dir):
        """Where a value exists, empty means "keep it" — see the module docstring.

        Under the new gate the prompt only fires when *nothing* supplies a
        domain, so the value on screen is always the built-in default and
        "keep it" and "take the default" are the same answer. The expression is
        kept because it is what makes an empty line a valid answer rather than
        an error, and because a future second source of a domain would land
        here without a rewrite.
        """
        args = self._login_args()
        asked = self._run(args, ["", "myschool", "student@example.com"])
        assert asked[0] == "Base domain [managebac.com]: "
        assert args.domain == "managebac.com"

    def test_a_profile_without_a_domain_is_still_prompted_for_it(self, state_dir):
        """Partial config prompts the gap, not the whole questionnaire."""
        _write_config("school", domain=None, active="school")
        _write_session("school")
        args = self._login_args()
        asked = self._run(args, [""])
        assert asked == ["Base domain [managebac.com]: "]
        assert args.domain == "managebac.com"

    def test_non_interactive_stdin_prompts_nothing(self, state_dir):
        args = self._login_args()
        with (
            patch.object(m, "_stdin_is_interactive", return_value=False),
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
        ):
            m._prompt_login_setup(args)
        assert args.school is None
        assert args.domain is None
        assert args.email is None


# ── the `--domain None` crash the task brief warned about ─────────────────


def _daemon_child_argv(*argv: str) -> list[str]:
    """Run `daemon start -b` and return the argv the detached child would get.

    ``cmd_daemon_start`` builds ``extra_args`` by hand, one ``if`` per flag, so
    the only way to know what the child is handed is to run the real builder
    with the spawn itself stubbed out.
    """
    captured: dict = {}

    class _FakeManager:
        def __init__(self, *a, **kw):
            pass

        def start_background(self, extra_args=None, env=None):
            captured["extra_args"] = list(extra_args or [])
            return {"started": True}

    buffer = StringIO()
    with (
        patch.object(m, "ServiceManager", _FakeManager),
        patch.object(m, "_warn_if_daemon_cannot_renew", return_value=False),
        patch.object(sys, "stdout", buffer),
        pytest.raises(SystemExit) as exc_info,
    ):
        main(["daemon", "start", "--background", "--format", "json", *argv])
    assert exc_info.value.code == 0
    return captured["extra_args"]


class TestDomainForwardingToTheDaemon:
    def test_daemon_start_does_not_forward_an_absent_domain(self):
        """`--domain None` must never reach the detached child's argv.

        `--domain` has no argparse default, so it is ``None`` when unset. The
        falsy guard in front of the extend is what keeps it out: a literal
        ``--domain None`` would reach ``_validate_school_domain`` in the child
        and fail as an unsupported domain, taking the daemon down at startup
        over a flag the operator never typed.
        """
        argv = _daemon_child_argv("--school", SCHOOL)
        assert "--domain" not in argv
        assert "None" not in argv
        # The school it *was* given still goes through, so this is not a
        # blanket suppression of the whole forwarding block.
        assert argv[argv.index("--school") + 1] == SCHOOL

    def test_a_real_domain_is_still_forwarded(self):
        argv = _daemon_child_argv("--school", SCHOOL, "-d", "managebac.cn")
        assert argv[argv.index("--domain") + 1] == "managebac.cn"

    def test_an_empty_string_domain_is_not_forwarded(self):
        """`--domain ""` is as unusable as None and must be dropped too."""
        argv = _daemon_child_argv("--school", SCHOOL, "--domain", "")
        assert "--domain" not in argv
