"""Tests for the credential-handling overhaul.

Covers five behaviours that were previously either wrong or undocumented:

1. ``tahuti logout`` deletes the stored password (``creds.json``) by default.
2. The session/password split: the session cookie is saved on every login, the
   password only when ``--keep-credentials`` asks for it. (The old
   ``login --temp`` promised "writes nothing to disk" while implementing neither
   that nor the ``remember_me`` half; the flag-pair tests now live in
   ``tests/test_session_lifecycle.py``.)
3. Loose file permissions on credential-bearing state files are reported.
4. ``MANAGEBAC_PASSWORD`` / ``MANAGEBAC_COOKIE`` (deprecated:
   ``MB_CRAWLER_*``) are readable, not just exported, plus the optional
   dependency-free OS keychain backend.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tahuti import __main__ as m
from tahuti import auth
from tahuti import keychain
from tahuti.auth import _load_creds, _store_password, build_client
from tahuti.config import (
    clear_creds,
    insecure_state_files,
    is_too_permissive,
    resolve_creds_path,
    warn_on_weak_permissions,
)

# Env vars every test here must control, so a developer's real ~/.config/tahuti
# (and any leaked MANAGEBAC_*/MB_CRAWLER_* from the shell) cannot influence the
# result. Both spellings are cleared; only the current ones are set.
_ISOLATE = (
    "MANAGEBAC_CONFIG",
    "MANAGEBAC_SESSION",
    "MANAGEBAC_CREDS_PATH",
    "MANAGEBAC_PASSWORD",
    "MANAGEBAC_COOKIE",
    "MANAGEBAC_KEYCHAIN",
    "MANAGEBAC_NO_PERM_WARN",
    "MB_CRAWLER_CONFIG",
    "MB_CRAWLER_SESSION",
    "MB_CRAWLER_CREDS_PATH",
    "MB_CRAWLER_PASSWORD",
    "MB_CRAWLER_COOKIE",
    "MB_CRAWLER_KEYCHAIN",
    "MB_CRAWLER_NO_PERM_WARN",
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every state path at tmp_path and clear all credential env vars."""
    for var in _ISOLATE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))
    monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(tmp_path / "creds.json"))
    # Pretend a credential helper exists so backend-selection is deterministic
    # on any machine. Tests that need "no helper" patch _tool to None instead.
    monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/fake-helper"])
    yield tmp_path


def _write_creds(path: Path, email="student@example.com", password="s3cret") -> None:
    path.write_text(json.dumps({"email": email, "password": password, "version": 1}))


def _write_session(path: Path, email="student@example.com", cookie="cookieval") -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_profile": "default",
                "profiles": {
                    "default": {
                        "school": "myschool",
                        "domain": "managebac.com",
                        "email": email,
                        "base_url": "https://myschool.managebac.com",
                        "cookie": cookie,
                        "logged_in_at": "2026-09-18T00:00:00",
                    }
                },
            }
        )
    )


def _logout_argv(*extra):
    return ["logout", "--format", "json", *extra]


# ── Task 2.1 — logout deletes the stored password ────────────────────────


class TestLogoutDeletesCredentials:
    def test_logout_removes_creds_json(self, isolated_env, capsys):
        """The whole point: `logout` must not leave the password on disk."""
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        assert creds.exists()

        with patch("tahuti.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv()))

        assert not creds.exists(), "logout left creds.json (the password) on disk"

    def test_logout_reports_removal(self, isolated_env, capsys):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        with patch("tahuti.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv()))
        payload = json.loads(capsys.readouterr().out)
        data = payload["data"]
        assert data["credentials_removed"] is True
        assert data["credentials_kept"] is False

    def test_keep_credentials_preserves_password(self, isolated_env):
        """`--keep-credentials` is the documented opt-out for silent re-login."""
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        with patch("tahuti.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(
                m.build_parser().parse_args(_logout_argv("--keep-credentials"))
            )
        assert creds.exists(), "--keep-credentials should keep creds.json"
        assert json.loads(creds.read_text())["password"] == "s3cret"

    def test_keep_credentials_reports_kept(self, isolated_env, capsys):
        with patch("tahuti.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(
                m.build_parser().parse_args(_logout_argv("--keep-credentials"))
            )
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["credentials_kept"] is True
        assert payload["data"]["credentials_removed"] is False

    def test_logout_without_creds_is_not_an_error(self, isolated_env, capsys):
        """Nothing to delete must not raise or claim a false removal."""
        with patch("tahuti.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            assert m.cmd_logout(m.build_parser().parse_args(_logout_argv())) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["credentials_removed"] is False

    def test_logout_deletes_keychain_entry(self, isolated_env):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        session = Path(os.environ["MANAGEBAC_SESSION"])
        _write_session(session)
        with (
            patch("tahuti.cache.ResponseCache") as cache_cls,
            patch.object(keychain, "delete", return_value=True) as delete,
        ):
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv()))
        delete.assert_called_once_with("student@example.com")

    def test_keep_credentials_leaves_keychain_entry(self, isolated_env):
        session = Path(os.environ["MANAGEBAC_SESSION"])
        _write_session(session)
        with (
            patch("tahuti.cache.ResponseCache") as cache_cls,
            patch.object(keychain, "delete", return_value=True) as delete,
        ):
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(
                m.build_parser().parse_args(_logout_argv("--keep-credentials"))
            )
        delete.assert_not_called()

    def test_logout_removes_creds(self, isolated_env):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        with patch("tahuti.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv()))
        assert not creds.exists()


class TestClearCreds:
    def test_removes_file_and_reports_true(self, tmp_path):
        p = tmp_path / "creds.json"
        _write_creds(p)
        assert clear_creds(p) is True
        assert not p.exists()

    def test_missing_file_returns_false(self, tmp_path):
        assert clear_creds(tmp_path / "nope.json") is False


# ── Task 2.2 — the password is opt-in, the session is not ────────────────
#
# `--temp` is gone. It promised "writes nothing to disk" while the session file
# was still written, and it gated the password, the response cache, the
# `remember_me` field and the session write off one boolean. The replacement is
# two independent flags; the full matrix lives in
# tests/test_session_lifecycle.py. What is pinned here is the part that is about
# *credential handling*: nothing writes a password unless asked.


class TestPasswordIsOptIn:
    """A login saves the session; it does not save the password by default."""

    def _build(self, client=None, **kwargs):
        client = client or MagicMock()
        client.login.return_value = True
        client.session.cookies.get.return_value = "newcookie"
        client.school = "myschool"
        client.domain = "managebac.com"
        client.base = "https://myschool.managebac.com"
        with patch("tahuti.auth.ManageBacClient", return_value=client):
            return build_client(
                school="myschool",
                email="student@example.com",
                password="s3cret",
                **kwargs,
            )

    def test_default_login_writes_no_password(self, isolated_env):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        self._build()
        assert not creds.exists(), "a password was saved without --keep-credentials"

    def test_keep_credentials_writes_the_password(self, isolated_env):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        self._build(keep_credentials=True)
        assert creds.exists()
        assert json.loads(creds.read_text())["password"] == "s3cret"

    def test_remember_me_does_not_decide_the_password(self, isolated_env):
        """`--no-remember-me` is a server-side cookie setting, nothing more."""
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        self._build(remember_me=None, keep_credentials=True)
        assert creds.exists(), "--no-remember-me must not silently drop the password"

    def test_default_login_saves_the_session(self, isolated_env, tmp_path):
        """Control: withholding the password must not withhold the session."""
        session = Path(os.environ["MANAGEBAC_SESSION"])
        self._build()
        assert session.exists()
        saved = json.loads(session.read_text())
        cookie = saved["cookie"]
        assert cookie == "newcookie", "the session cookie was not persisted"

    def test_the_response_cache_no_longer_follows_the_credential_flags(self, isolated_env):
        """`enabled=not refresh`, and neither new flag is in that expression."""
        with patch("tahuti.auth.ManageBacClient") as client_cls:
            client_cls.return_value.login.return_value = True
            client_cls.return_value.session.cookies.get.return_value = "c"
            client_cls.return_value.school = "myschool"
            client_cls.return_value.domain = "managebac.com"
            client_cls.return_value.base = "https://myschool.managebac.com"
            build_client(
                school="myschool",
                email="student@example.com",
                password="s3cret",
                remember_me=None,
                keep_credentials=True,
            )
        assert client_cls.call_args.kwargs["cache"].enabled is True


# ── Task 2.3 — warn on weak file permissions ─────────────────────────────


class TestWeakPermissionWarning:
    def _chmod(self, path: Path, mode: int) -> None:
        path.write_text("{}")
        os.chmod(path, mode)

    @pytest.mark.parametrize("mode", [0o644, 0o664, 0o666, 0o777, 0o604])
    def test_flags_looser_than_0600(self, tmp_path, mode):
        p = tmp_path / "creds.json"
        self._chmod(p, mode)
        assert is_too_permissive(p) is True

    @pytest.mark.parametrize("mode", [0o600, 0o400, 0o000])
    def test_accepts_0600_or_tighter(self, tmp_path, mode):
        p = tmp_path / "creds.json"
        self._chmod(p, mode)
        assert is_too_permissive(p) is False

    def test_missing_file_is_not_permissive(self, tmp_path):
        assert is_too_permissive(tmp_path / "absent.json") is False

    def test_warns_about_world_readable_creds(self, isolated_env, tmp_path):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        messages = warn_on_weak_permissions(stream=open(os.devnull, "w"))
        assert len(messages) == 1
        assert "creds.json" in messages[0]
        assert "0644" in messages[0]
        assert "chmod 600" in messages[0]

    def test_warns_about_config_json(self, isolated_env, tmp_path):
        config = tmp_path / "config.json"
        config.write_text("{}")
        os.chmod(config, 0o644)
        messages = warn_on_weak_permissions(stream=open(os.devnull, "w"))
        assert any("config.json" in msg for msg in messages)

    def test_warns_about_session_json(self, isolated_env, tmp_path):
        """session.json holds a live cookie, so it is checked too."""
        session = tmp_path / "session.json"
        _write_session(session)
        os.chmod(session, 0o666)
        messages = warn_on_weak_permissions(stream=open(os.devnull, "w"))
        assert any("session.json" in msg for msg in messages)

    def test_no_warning_when_all_0600(self, isolated_env, tmp_path):
        for name in ("config.json", "session.json", "creds.json"):
            p = tmp_path / name
            p.write_text("{}")
            os.chmod(p, 0o600)
        assert warn_on_weak_permissions(stream=open(os.devnull, "w")) == []

    def test_warning_goes_to_stderr(self, isolated_env, capsys):
        """Must not contaminate `--format json` on stdout."""
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        warn_on_weak_permissions()
        captured = capsys.readouterr()
        assert "creds.json" in captured.err
        assert captured.out == ""

    def test_env_var_suppresses_output_but_not_detection(self, isolated_env, monkeypatch, capsys):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        monkeypatch.setenv("MB_CRAWLER_NO_PERM_WARN", "1")
        messages = warn_on_weak_permissions()
        assert messages, "detection should still happen for programmatic callers"
        assert capsys.readouterr().err == ""

    def test_insecure_state_files_lists_every_loose_file(self, isolated_env, tmp_path):
        loose = tmp_path / "creds.json"
        _write_creds(loose)
        os.chmod(loose, 0o644)
        tight = tmp_path / "session.json"
        tight.write_text("{}")
        os.chmod(tight, 0o600)
        found = {p.name for p in insecure_state_files()}
        assert "creds.json" in found
        assert "session.json" not in found

    def test_main_warns_on_startup(self, isolated_env, capsys):
        """The warning has to actually reach a user running a normal command."""
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        session = Path(os.environ["MANAGEBAC_SESSION"])
        _write_session(session)
        # `logout` needs no network, so it exercises main() end to end.
        with patch("tahuti.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            with pytest.raises(SystemExit) as exc:
                m.main(["logout", "--format", "json"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "creds.json" in captured.err
        # stdout stays valid JSON despite the stderr warning
        assert json.loads(captured.out)["data"]["logged_out"] is True


# ── Task 2.4 — optional, dependency-free OS keychain ─────────────────────


def _op(argv: list) -> str:
    """Classify a credential-helper invocation as ``store``/``lookup``/``delete``.

    The verb sits at ``argv[1]`` for ``secret-tool`` and ``security``, which
    keeps an account name containing the word "store" from being mistaken for
    the operation. The longer verbs are matched first so
    ``find-generic-password`` is not read as the ``store`` verb just because
    both contain ``password``; Windows has no verb at all and is told apart by
    the script it renders.
    """
    if len(argv) > 1 and argv[1] in ("lookup", "store", "clear"):
        return {"lookup": "lookup", "store": "store", "clear": "delete"}[argv[1]]
    if "find-generic-password" in argv:
        return "lookup"
    if "add-generic-password" in argv:
        return "store"
    if "delete-generic-password" in argv:
        return "delete"
    script = argv[-1] if argv else ""
    if ".Add(" in script:  # PowerShell _PS_STORE
        return "store"
    if "RetrievePassword" in script:  # PowerShell _PS_LOOKUP
        return "lookup"
    return "delete"


class _FakeKeychain:
    """In-memory stand-in for the OS credential helper.

    Models the contract the three real helpers share, because getting that
    contract wrong is exactly how the credential-stranding bug survived:

    - the secret is persisted, and the read-back is *not* an afterthought —
      ``store`` must be able to recover it;
    - ``secret-tool`` and ``security`` terminate their output with one newline
      the secret does not contain, while the Windows ``PasswordVault`` read is
      byte-exact and base64-wrapped;
    - ``drop=True`` models the dangerous case: the helper exits 0 and persists
      nothing. That is what a locked Secret Service collection looks like from
      the caller's side, and it must not be mistaken for success.
    """

    def __init__(self, monkeypatch, *, drop: bool = False, store_rc: int = 0):
        self.calls: list[dict] = []
        self.stored: bytes | None = None
        self.drop = drop
        self.store_rc = store_rc
        monkeypatch.setattr(keychain, "_run", self)

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append({"argv": argv, "kwargs": kwargs})
        operation = _op(argv)
        if operation == "store":
            if self.store_rc:
                return subprocess.CompletedProcess(
                    argv, self.store_rc, b"", b"collection is locked"
                )
            if self.drop:
                # Exit 0, write nothing: the false-positive success.
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            # macOS is the one backend that cannot take the secret on stdin.
            secret = kwargs.get("stdin")
            if secret is None:
                secret = argv[-1].encode("utf-8")
            self.stored = secret
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if operation == "lookup":
            if self.drop or self.stored is None:
                # Exit 0 with nothing to show — the false-positive success.
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if "RetrievePassword" in " ".join(argv):
                return subprocess.CompletedProcess(
                    argv, 0, base64.b64encode(self.stored), b""
                )
            return subprocess.CompletedProcess(argv, 0, self.stored + b"\n", b"")
        self.stored = None  # delete
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class TestKeychainModule:
    def test_no_helper_means_unavailable(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: None)
        assert keychain.available() is False

    def test_disabled_by_default(self, isolated_env):
        assert keychain.enabled() is False

    def test_env_var_enables(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        assert keychain.enabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_env_var_truthy_values(self, isolated_env, monkeypatch, value):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", value)
        assert keychain.enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", ""])
    def test_env_var_falsy_values(self, isolated_env, monkeypatch, value):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", value)
        assert keychain.enabled() is False

    def test_explicit_flag_overrides_env(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "0")
        assert keychain.enabled(True) is True
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        assert keychain.enabled(False) is False

    def test_explicit_flag_cannot_enable_without_helper(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: None)
        assert keychain.enabled(True) is False

    def test_argv_shape_on_linux(self, monkeypatch):
        """secret-tool takes the secret on stdin, never argv."""
        monkeypatch.setattr(keychain.sys, "platform", "linux")
        monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/secret-tool"])
        seen = _FakeKeychain(monkeypatch)
        assert keychain.store("a@b.c", "pw") is True
        argv = seen.calls[0]["argv"]
        assert argv[0].endswith("secret-tool")
        assert "store" in argv
        assert "pw" not in argv, "secret leaked into argv"
        assert seen.calls[0]["kwargs"].get("stdin") == b"pw"

    def test_argv_shape_on_macos(self, monkeypatch):
        monkeypatch.setattr(keychain.sys, "platform", "darwin")
        monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/security"])
        seen = _FakeKeychain(monkeypatch)
        assert keychain.store("a@b.c", "pw") is True
        argv = seen.calls[0]["argv"]
        assert argv[0].endswith("security")
        assert "add-generic-password" in argv
        assert "-U" in argv, "re-login must update in place, not fail"

    def test_store_failure_returns_false(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b"", b"locked")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "pw") is False

    def test_store_handles_missing_helper(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: None)
        assert keychain.store("a@b.c", "pw") is False

    def test_store_handles_oserror(self, monkeypatch):
        def boom(argv, **kwargs):
            raise OSError("exec failed")

        monkeypatch.setattr(keychain, "_run", boom)
        assert keychain.store("a@b.c", "pw") is False

    def test_lookup_returns_secret(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"the-password\n", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") == "the-password"

    def test_lookup_missing_item_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 44, b"", b"not found")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") is None

    def test_delete_reports_success(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.delete("a@b.c") is True
        # The verb is per-platform: macOS `security delete-generic-password`,
        # Linux `secret-tool clear`. Assert the one this platform actually
        # issues — hardcoding the macOS verb made the suite Darwin-only.
        expected = {
            "darwin": "delete-generic-password",
            "win32": _PS_DELETE_MARKER,
        }.get(sys.platform, "clear")
        assert expected in seen["argv"]


# ── store() must prove the write landed, on every platform ────────────────
#
# `auth._store_password` unlinks creds.json whenever `store()` returns True, so
# a "success" that persisted nothing destroys the only copy of the password.
# That read-back verification used to be gated on `sys.platform == "win32"`,
# which left Linux exposed — and a locked Secret Service collection that lets
# `secret-tool` exit 0 is an ordinary condition on a headless box.

#: Passwords whose bytes must survive the round trip untouched. Each has broken
#: something before: the trailing newlines defeated the old ``strip("\n")``
#: normalisation (so such a password could never be verified as stored), and
#: the rest are shell / PowerShell / JSON metacharacters that a naive
#: implementation would mangle on the way through a child process.
HOSTILE_SECRETS = [
    "pw\n",  # a trailing newline — the normalisation trap
    "pw\n\n",  # more than one
    "\n",  # nothing but a newline
    "quote'd\"double",  # both quote styles
    "$(whoami) `id` ${HOME}",  # shell and PowerShell expansion
    "back\\slash",  # a backslash, and an escape-looking pair
    "pä§§wörd — 日本語 🔐",  # non-ASCII
    "tab\there",
]


class TestStoreVerifiesTheWrite:
    """A ``True`` from ``store`` must mean the password can be read back."""

    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    def test_store_reports_false_when_the_helper_persists_nothing(
        self, monkeypatch, platform
    ):
        """Exiting 0 without storing is a failure, not a success.

        Reproduces the defect: a ``secret-tool`` that exits 0 against a locked
        Secret Service collection used to be believed on Linux and macOS.
        """
        monkeypatch.setattr(keychain.sys, "platform", platform)
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])
        helper = _FakeKeychain(monkeypatch, drop=True)
        assert keychain.store("a@b.c", "s3cret") is False
        assert helper.stored is None, "the double must really have dropped it"

    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    @pytest.mark.parametrize("secret", HOSTILE_SECRETS)
    def test_store_round_trips_hostile_passwords(
        self, monkeypatch, platform, secret
    ):
        """store() then lookup() must return the password byte for byte."""
        monkeypatch.setattr(keychain.sys, "platform", platform)
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])
        _FakeKeychain(monkeypatch)
        assert keychain.store("a@b.c", secret) is True, f"{platform} lost {secret!r}"
        assert keychain.lookup("a@b.c") == secret

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_lookup_preserves_a_password_ending_in_a_newline(
        self, monkeypatch, platform
    ):
        """A newline the user typed is data, not the helper's terminator.

        The old ``strip("\\n")`` turned ``"pw\\n"`` into ``"pw"``, so the stored
        value could never match and ``store`` failed on every attempt —
        permanently forcing the cleartext fallback for such passwords.
        """
        monkeypatch.setattr(keychain.sys, "platform", platform)
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])
        _FakeKeychain(monkeypatch)
        assert keychain.store("a@b.c", "s3cret\n") is True
        assert keychain.lookup("a@b.c") == "s3cret\n"

    def test_lookup_strips_only_the_helpers_own_newline(self, monkeypatch):
        """One trailing newline is the terminator; the rest belong to the user."""
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"pw\n\n", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") == "pw\n"

    def test_lookup_still_tolerates_a_helper_that_adds_no_newline(
        self, monkeypatch
    ):
        """A backend that writes the secret bare must not be off by one."""
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"pw", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") == "pw"

    def test_store_rejects_a_corrupted_read_back(self, monkeypatch):
        """A vault that returns the wrong bytes must not be reported as stored."""
        monkeypatch.setattr(keychain.sys, "platform", "linux")
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])

        def fake_run(argv, **kwargs):
            if argv[-1] == "store":
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"something-else\n", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "s3cret") is False


# ── Windows keychain — WinRT PasswordVault via powershell.exe ────────────
#
# Exercised on macOS/CI by faking `sys.platform` and `shutil.which`, so the
# win32 branches are reachable here. No test below launches a real PowerShell;
# they assert on the argv / script / stdin the module would hand the child.

#: Distinguishes the Windows delete path in ``delete()``'s argv: unlike macOS
#: and Linux it is expressed as a rendered PowerShell script, not a verb flag.
_PS_DELETE_MARKER = "$v.Remove("

#: Stands in for the prefix `keychain._tool()` builds on Windows.
_WIN_TOOL = [
    r"C:\WINDOWS\System32\powershell.exe",
    "-NoProfile",
    "-NoLogo",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
]


#: The genuine `_tool` probe, captured before the autouse fixture stubs it out.
_REAL_TOOL = keychain._tool


def _only_these_exist(monkeypatch, *names):
    """Make `shutil.which` find exactly *names*, probing with the real `_tool`.

    Returns the list of program names probed, in order. The autouse fixture
    stubs ``keychain._tool`` out so backend selection is deterministic; the
    discovery tests need the genuine probe back.
    """
    asked = []

    def which(name, *args, **kwargs):
        asked.append(name)
        return "/usr/bin/" + name if name in names else None

    monkeypatch.setattr(keychain.shutil, "which", which)
    monkeypatch.setattr(keychain, "_tool", _REAL_TOOL)
    return asked


def _win32(monkeypatch, tool=None):
    """Pretend to be Windows, with *tool* as the credential-helper prefix."""
    monkeypatch.setattr(keychain.sys, "platform", "win32")
    monkeypatch.setattr(
        keychain, "_tool", lambda: _WIN_TOOL if tool is None else tool
    )


class TestWindowsKeychainBackend:
    """Windows must be a real credential store, not a silent cleartext fallback.

    Three properties matter: the secret never reaches argv or the child's
    environment, the vault can be read back (the daemon needs silent re-login),
    and any failure degrades to creds.json instead of dropping the password.
    """

    def _script(self, monkeypatch, fn, *args, **kwargs):
        """Run *fn* on a fake Windows and return what the child would have got.

        ``script`` / ``stdin`` / ``argv`` describe the *first* invocation, the
        operation under test; ``store`` may follow it with a verifying lookup.
        """
        seen = {"calls": []}

        def fake_run(argv, **run_kwargs):
            seen["calls"].append({"argv": argv, "kwargs": run_kwargs})
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        fn(*args, **kwargs)
        first = seen["calls"][0]
        seen["argv"] = first["argv"]
        seen["script"] = first["argv"][-1]
        seen["stdin"] = first["kwargs"].get("stdin")
        seen["kwargs"] = first["kwargs"]
        return seen

    # ── helper discovery ─────────────────────────────────────────────────

    def test_tool_probes_powershell_first(self, monkeypatch):
        asked = _only_these_exist(monkeypatch, "powershell")
        monkeypatch.setattr(keychain.sys, "platform", "win32")
        assert keychain._tool() == [
            "/usr/bin/powershell",
            "-NoProfile",
            "-NoLogo",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
        ]
        assert asked == ["powershell"], "pwsh must only be tried when 5.1 is absent"

    def test_tool_falls_back_to_powershell_7(self, monkeypatch):
        asked = _only_these_exist(monkeypatch, "pwsh")
        monkeypatch.setattr(keychain.sys, "platform", "win32")
        assert keychain._tool()[0] == "/usr/bin/pwsh"
        assert asked == ["powershell", "pwsh"]

    def test_tool_absent_without_powershell(self, monkeypatch):
        _only_these_exist(monkeypatch)
        monkeypatch.setattr(keychain.sys, "platform", "win32")
        assert keychain._tool() is None
        assert keychain.available() is False

    def test_win32_never_falls_through_to_secret_tool(self, monkeypatch):
        """`secret-tool` is a Linux binary; the win32 branch must win over it."""
        _only_these_exist(monkeypatch, "secret-tool", "powershell")
        monkeypatch.setattr(keychain.sys, "platform", "win32")
        tool = keychain._tool()
        assert tool is not None, "Windows must not report 'no helper'"
        assert tool[0] == "/usr/bin/powershell"

    @pytest.mark.parametrize(
        "platform, expected",
        [("linux", "secret-tool"), ("darwin", "security")],
    )
    def test_other_platforms_never_pick_up_powershell(
        self, monkeypatch, platform, expected
    ):
        """A developer with PowerShell installed on macOS must be unaffected."""
        _only_these_exist(
            monkeypatch, "powershell", "pwsh", "security", "secret-tool"
        )
        monkeypatch.setattr(keychain.sys, "platform", platform)
        assert keychain._tool()[0].endswith(expected)

    def test_enabled_on_windows_with_powershell(self, monkeypatch):
        _only_these_exist(monkeypatch, "powershell")
        monkeypatch.setattr(keychain.sys, "platform", "win32")
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        assert keychain.enabled() is True

    # ── store ────────────────────────────────────────────────────────────

    def test_store_keeps_the_secret_out_of_argv(self, monkeypatch):
        seen = self._script(monkeypatch, keychain.store, "a@b.c", "s3cret-pw")
        assert "s3cret-pw" not in " ".join(seen["argv"]), "secret leaked into argv"
        assert seen["stdin"] == b"s3cret-pw", "secret must travel on stdin"

    def test_store_keeps_the_secret_out_of_the_child_environment(self, monkeypatch):
        """Guards against 'fixing' argv leakage by smuggling it through env."""
        seen = self._script(monkeypatch, keychain.store, "a@b.c", "s3cret-pw")
        assert "env" not in seen["kwargs"], "the child must inherit env unchanged"

    def test_store_script_drives_the_vault(self, monkeypatch):
        seen = self._script(monkeypatch, keychain.store, "a@b.c", "pw")
        script = seen["script"]
        assert "PasswordVault" in script
        assert "$v.Remove($v.Retrieve('tahuti','a@b.c'))" in script, (
            "a re-login must update in place"
        )
        assert "$v.Add(" in script

    def test_store_adds_a_passwordcredential(self, monkeypatch):
        """`Add`/`Remove` take a PasswordCredential; only `Retrieve` takes a pair."""
        seen = self._script(monkeypatch, keychain.store, "a@b.c", "pw")
        assert "::new('tahuti','a@b.c',$pw)" in seen["script"]
        assert "$v.Add('tahuti','a@b.c',$pw)" not in seen["script"]

    def test_store_reads_the_existing_item_before_removing_it(self, monkeypatch):
        """`Remove` takes the credential object, so `Retrieve` must run first."""
        seen = self._script(monkeypatch, keychain.store, "a@b.c", "pw")
        assert "$v.Remove($v.Retrieve('tahuti','a@b.c'))" in seen["script"]
        assert seen["script"].index("$v.Remove(") < seen["script"].index("$v.Add(")

    def test_store_quotes_the_account_name(self, monkeypatch):
        """The account comes from the CLI and must not be able to inject script."""
        seen = self._script(monkeypatch, keychain.store, "o'brien@x.com", "pw")
        assert "'o''brien@x.com'" in seen["script"], "quote not doubled"
        assert "'o'brien@x.com'" not in seen["script"]

    def test_store_substitutes_the_account_last(self, monkeypatch):
        """Braces in the account must survive verbatim, not hit a placeholder."""
        seen = self._script(
            monkeypatch, keychain.store, "a{target}{notfound}@x.com", "pw"
        )
        assert "'a{target}{notfound}@x.com'" in seen["script"]

    def test_store_nonzero_rc_returns_false(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b"", b"WinRT type not found")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "pw") is False

    def test_store_reports_false_when_the_item_cannot_be_read_back(self, monkeypatch):
        """`auth` deletes creds.json on True, so a write-only vault must not pass."""

        def fake_run(argv, **kwargs):
            if ".Add(" in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 1, b"", b"vault unreadable")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "pw") is False

    def test_store_verifies_by_reading_the_item_back(self, monkeypatch):
        scripts = []

        def fake_run(argv, **kwargs):
            scripts.append(argv[-1])
            if ".Add(" in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(
                argv, 0, base64.b64encode(b"pw"), b""
            )

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "pw") is True
        assert len(scripts) == 2, "store must read the item back before claiming success"

    # ── lookup ───────────────────────────────────────────────────────────

    def test_lookup_calls_retrieve_password(self, monkeypatch):
        """`Retrieve` leaves `.Password` empty until `RetrievePassword()` runs."""
        seen = self._script(monkeypatch, keychain.lookup, "a@b.c")
        assert "$c.RetrievePassword()" in seen["script"]

    def test_lookup_decodes_base64(self, monkeypatch):
        def fake_run(argv, **kwargs):
            out = base64.b64encode("line1\nline2".encode())
            return subprocess.CompletedProcess(argv, 0, out, b"")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        # A password with a newline proves the base64 hop: PowerShell's text
        # output would have reflowed or CRLF-mangled it.
        assert keychain.lookup("a@b.c") == "line1\nline2"

    def test_lookup_missing_item_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, keychain._PS_NOT_FOUND, b"", b"")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") is None

    def test_lookup_unhandled_powershell_error_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b"", b"Unable to cast")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") is None

    def test_lookup_undecodable_output_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"not base64 !!", b"")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") is None

    def test_lookup_non_utf8_output_returns_none(self, monkeypatch):
        """Undecodable bytes must degrade, not raise out of the daemon's path."""

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, base64.b64encode(b"\xff\xfe not utf-8"), b""
            )

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") is None

    def test_lookup_survives_a_hung_powershell(self, monkeypatch):
        """The daemon calls this on every silent re-login; it must not raise."""

        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 15)

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", boom)
        assert keychain.lookup("a@b.c") is None

    def test_store_without_powershell_returns_false(self, monkeypatch):
        """No helper means `auth` writes creds.json rather than losing the password."""
        _only_these_exist(monkeypatch)
        monkeypatch.setattr(keychain.sys, "platform", "win32")
        assert keychain.available() is False
        assert keychain.store("a@b.c", "pw") is False

    # ── delete ───────────────────────────────────────────────────────────

    def test_delete_removes_from_the_vault(self, monkeypatch):
        seen = self._script(monkeypatch, keychain.delete, "a@b.c")
        assert "$v.Remove($v.Retrieve('tahuti','a@b.c'))" in seen["script"]
        assert seen["stdin"] is None, "delete needs no secret on stdin"

    def test_delete_missing_item_returns_false(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, keychain._PS_NOT_FOUND, b"", b"")

        _win32(monkeypatch=monkeypatch)
        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.delete("a@b.c") is False


class TestKeychainWiring:
    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    def test_a_lying_store_costs_a_fallback_not_the_password(
        self, isolated_env, monkeypatch, platform
    ):
        """End to end: creds.json must survive a store that never landed.

        ``_store_password`` deletes the cleartext copy on a ``True`` from
        ``keychain.store``. Before the read-back ran outside Windows, a
        ``secret-tool`` that exited 0 against a locked Secret Service
        collection unlinked the file and left nothing in the vault — the next
        invocation then failed with ``missing_credentials`` and the password
        was unrecoverable.
        """
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        monkeypatch.setattr(keychain.sys, "platform", platform)
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds, password="s3cret")  # an earlier non-keychain login
        helper = _FakeKeychain(monkeypatch, drop=True)

        assert _store_password("student@example.com", "s3cret") == "file"
        assert helper.stored is None
        assert creds.exists(), "creds.json was deleted for a store that never landed"
        assert json.loads(creds.read_text())["password"] == "s3cret"

    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    def test_a_real_store_still_drops_the_cleartext_copy(
        self, isolated_env, monkeypatch, platform
    ):
        """The verification must not become so strict that it always falls back."""
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        monkeypatch.setattr(keychain.sys, "platform", platform)
        monkeypatch.setattr(keychain, "_tool", lambda: ["/fake/helper"])
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds, password="s3cret")
        _FakeKeychain(monkeypatch)

        assert _store_password("student@example.com", "s3cret") == "keychain"
        assert not creds.exists(), "password left in cleartext creds.json too"

    def test_store_prefers_keychain_and_drops_cleartext(self, isolated_env):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        _write_creds(creds)  # leftover from a previous non-keychain login
        with (
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=True) as store,
        ):
            backend = _store_password("student@example.com", "s3cret")
        assert backend == "keychain"
        store.assert_called_once_with("student@example.com", "s3cret")
        assert not creds.exists(), "password left in cleartext creds.json too"

    def test_store_falls_back_to_file_when_keychain_fails(self, isolated_env):
        """A locked keychain must not silently lose the credential."""
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        with (
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=False),
        ):
            backend = _store_password("student@example.com", "s3cret")
        assert backend == "file"
        assert creds.exists()
        assert json.loads(creds.read_text())["password"] == "s3cret"

    def test_store_uses_file_when_keychain_off(self, isolated_env):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        with (
            patch.object(keychain, "enabled", return_value=False),
            patch.object(keychain, "store") as store,
        ):
            assert _store_password("student@example.com", "s3cret") == "file"
        store.assert_not_called()
        assert creds.exists()

    def test_load_falls_back_to_keychain_when_file_has_no_password(self, isolated_env, tmp_path):
        tmp_path / "creds.json"
        with (
            patch.object(keychain, "available", return_value=True),
            patch.object(keychain, "lookup", return_value="from-keychain") as lookup,
        ):
            creds = _load_creds("student@example.com")
        lookup.assert_called_once_with("student@example.com")
        assert creds == {"email": "student@example.com", "password": "from-keychain"}

    def test_file_password_wins_over_keychain(self, isolated_env, tmp_path):
        _write_creds(tmp_path / "creds.json")
        with (
            patch.object(keychain, "available", return_value=True),
            patch.object(keychain, "lookup", return_value="stale") as lookup,
        ):
            creds = _load_creds("student@example.com")
        lookup.assert_not_called()
        assert creds["password"] == "s3cret"

    def test_load_returns_none_without_either_backend(self, isolated_env):
        with (
            patch.object(keychain, "available", return_value=True),
            patch.object(keychain, "lookup", return_value=None),
        ):
            assert _load_creds("student@example.com") is None

    def test_login_keychain_flag_threads_through_build_client(self, isolated_env):
        args = m.build_parser().parse_args(["login", "--keychain", "--keep-credentials"])
        assert args.keychain is True
        assert args.keep_credentials is True
        with patch("tahuti.auth.ManageBacClient") as client_cls:
            client_cls.return_value.login.return_value = True
            client_cls.return_value.session.cookies.get.return_value = "newcookie"
            client_cls.return_value.school = "myschool"
            client_cls.return_value.domain = "managebac.com"
            client_cls.return_value.base = "https://myschool.managebac.com"
            build_client(
                school="myschool",
                email="student@example.com",
                password="s3cret",
                keep_credentials=getattr(args, "keep_credentials", False),
                use_keychain=getattr(args, "keychain", None),
            )

    def test_keychain_enabled_login_writes_no_cleartext(self, isolated_env):
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        with (
            patch("tahuti.auth.ManageBacClient") as client_cls,
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=True),
        ):
            client_cls.return_value.login.return_value = True
            client_cls.return_value.session.cookies.get.return_value = "newcookie"
            client_cls.return_value.school = "myschool"
            client_cls.return_value.domain = "managebac.com"
            client_cls.return_value.base = "https://myschool.managebac.com"
            build_client(
                school="myschool",
                email="student@example.com",
                password="s3cret",
                keep_credentials=True,
                use_keychain=True,
            )
        assert not creds.exists()


# ── Task 3 — MB_CRAWLER_PASSWORD / MB_CRAWLER_COOKIE are readable ────────


class TestCredentialEnvVars:
    """These were write-only: exported into the daemon child, never read back."""

    def _parse(self, *argv):
        return m.build_parser().parse_args(list(argv))

    def test_password_read_from_environment(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "env-password")
        captured = {}
        args = self._parse("list", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        assert captured["password"] == "env-password"

    def test_cookie_read_from_environment(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_COOKIE", "env-cookie")
        captured = {}
        args = self._parse("list", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        assert captured["cookie"] == "env-cookie"

    def test_explicit_password_beats_environment(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "env-password")
        captured = {}
        args = self._parse("list", "--password", "flag-password", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        assert captured["password"] == "flag-password"

    def test_environment_avoids_the_interactive_prompt(self, isolated_env, monkeypatch):
        """The point of the env var: no TTY needed in CI."""
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "env-password")
        args = self._parse("list", "--format", "json")
        with (
            patch.object(m, "build_client", side_effect=SystemExit(0)),
            patch.object(m, "load_state") as load_state,
            patch.object(m.getpass, "getpass") as getpass,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        getpass.assert_not_called()

    def test_prompt_still_used_without_environment(self, isolated_env):
        args = self._parse("list", "--format", "json")
        with (
            patch.object(m, "build_client", side_effect=SystemExit(0)),
            patch.object(m, "load_state") as load_state,
            patch.object(m.getpass, "getpass", return_value="typed") as getpass,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        getpass.assert_called_once()

    def test_empty_environment_value_falls_through(self, isolated_env, monkeypatch):
        """An exported-but-empty var must not look like a supplied password."""
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "")
        args = self._parse("list", "--format", "json")
        with (
            patch.object(m, "build_client", side_effect=SystemExit(0)),
            patch.object(m, "load_state") as load_state,
            patch.object(m.getpass, "getpass", return_value="typed") as getpass,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        getpass.assert_called_once()

    def test_daemon_start_still_exports_for_the_child(self, isolated_env):
        """Round-trip: the parent exports, and the child can now read it back."""
        args = self._parse(
            "daemon", "start", "-b", "--password", "pw123", "--format", "json"
        )
        with patch(
            "tahuti.daemon.system.ServiceManager.start_background",
            return_value={"started": True},
        ) as start:
            m.cmd_daemon_start(args)
        _, kwargs = start.call_args
        assert kwargs["env"]["MANAGEBAC_PASSWORD"] == "pw123"
        assert "pw123" not in " ".join(kwargs["extra_args"])

    def test_daemon_run_child_reads_the_exported_password(self, isolated_env, monkeypatch):
        """The child `mb daemon run` sees MB_CRAWLER_PASSWORD and uses it."""
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "pw123")
        captured = {}
        args = self._parse("daemon", "run", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "daemon")
        assert captured["password"] == "pw123"


# ── config-level helpers ────────────────────────────────────────────────


class TestResolveCredsPath:
    def test_explicit_wins(self, tmp_path):
        assert resolve_creds_path(str(tmp_path / "x.json")) == tmp_path / "x.json"

    def test_env_var(self, isolated_env):
        assert resolve_creds_path() == Path(os.environ["MANAGEBAC_CREDS_PATH"])

    def test_default_lives_in_config_dir(self, monkeypatch):
        for var in ("MANAGEBAC_CREDS_PATH", "MB_CRAWLER_CREDS_PATH"):
            monkeypatch.delenv(var, raising=False)
        path = resolve_creds_path()
        assert path.name == "creds.json"
        assert path.parent.name == "tahuti"

    def test_default_is_mode_guarded(self, isolated_env):
        """Sanity: the path we resolve is the one we write 0600."""
        assert resolve_creds_path().name == "creds.json"


# ── the creds path is resolved once, per call, everywhere ─────────────────
#
# `auth` used to capture the path in a module-level constant at import *and*
# re-resolve it in `_creds_path()`. Two code paths could therefore name two
# different files: one writes the password to `~/.config/tahuti/creds.json`
# while the other goes looking in `~/.config/mb-crawler/creds.json` and reports
# `missing_credentials`. There is a live instance of that on the destination
# host — an older `mb` install still using the pre-rename directory.


class TestCredsPathResolvesOnce:
    def test_no_import_time_constant_can_go_stale(self):
        """The contract: nothing in `auth` may freeze the path at import."""
        assert not hasattr(auth, "_CREDS_PATH")
        assert not hasattr(auth, "_CREDS_PATH_ENV")

    def test_auth_reads_no_credential_env_var_at_module_scope(self):
        """Guards against reintroducing the constant in a new spelling.

        `auth` used to capture ``MB_CRAWLER_CREDS_PATH`` at import *and*
        re-resolve it per call, leaving two answers to one question. The
        captured one was never read, so the split stayed latent — but a frozen
        path is exactly the kind of thing a later change starts relying on, and
        under pytest the snapshot would have pinned the *first* test's
        ``tmp_path`` for the whole session.
        """
        source = Path(auth.__file__).read_text(encoding="utf-8")
        module_scope = source.split("def _creds_path")[0]
        assert "environ" not in module_scope, (
            "auth must not read the environment at import time"
        )

    def test_every_path_follows_an_env_var_set_after_import(self, isolated_env, monkeypatch):
        """Storing, reading, deleting and reporting must agree on one file."""
        first = isolated_env / "first.json"
        second = isolated_env / "second.json"

        monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(first))
        assert auth._creds_path() == str(first)

        # Changed *after* import — the old constant would still have said `first`.
        monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(second))
        assert auth._creds_path() == str(second)

        with patch.object(keychain, "enabled", return_value=False):
            assert auth._store_password("student@example.com", "s3cret") == "file"
        assert second.exists(), "the password was written to the stale path"
        assert not first.exists()
        assert json.loads(second.read_text())["password"] == "s3cret"

        assert auth._load_creds()["password"] == "s3cret"

        monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(first))
        assert auth._load_creds() is None, "read and write resolved different files"

    def test_delete_follows_the_same_resolution(self, isolated_env, monkeypatch):
        """`_store_password` must clear the file it just replaced, not a stale one."""
        target = isolated_env / "creds.json"
        monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(target))
        _write_creds(target, password="old")

        with (
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=True),
        ):
            assert auth._store_password("student@example.com", "new") == "keychain"
        assert not target.exists(), "the cleartext copy was left behind"
        assert not (isolated_env / "creds.json.other").exists()

    def test_state_paths_follow_home_set_after_import(self, isolated_env, monkeypatch):
        """`config_dir()` reads $HOME per call; a constant would freeze it."""
        import tahuti.config as config

        # Drop the fixture's redirects so the defaults (not the env vars) apply.
        for var in (
            "MANAGEBAC_CREDS_PATH",
            "MANAGEBAC_CONFIG",
            "MANAGEBAC_SESSION",
            "MB_CRAWLER_CREDS_PATH",
            "MB_CRAWLER_CONFIG",
            "MB_CRAWLER_SESSION",
        ):
            monkeypatch.delenv(var, raising=False)
        # Also drop the autouse fixture's in-place patches of the legacy names.
        # `config_dir()` resolves dynamically, but the module-level names are
        # real attributes once patched, and a real attribute short-circuits
        # `__getattr__` — so leaving them patched would pin the fixture's
        # tmp_path and defeat the assertion this test exists to make.
        for name in ("CONFIG_DIR", "DEFAULT_CONFIG_PATH", "DEFAULT_SESSION_PATH",
                     "DEFAULT_CREDS_PATH"):
            monkeypatch.delattr(config, name, raising=False)
        monkeypatch.setenv("HOME", "/tmp/some-other-home")
        other = Path("/tmp/some-other-home/.config/tahuti")
        assert config.config_dir() == other
        assert config.resolve_creds_path() == other / "creds.json"
        assert config.resolve_config_path() == other / "config.json"
        assert config.resolve_session_path() == other / "session.json"
        # The legacy importable names must not be stale snapshots either.
        assert config.CONFIG_DIR == other
        assert config.DEFAULT_CREDS_PATH == other / "creds.json"

    def test_legacy_path_names_stay_importable(self):
        """Removing the constants must not break an out-of-tree importer."""
        from tahuti.config import CONFIG_DIR, DEFAULT_CONFIG_PATH, DEFAULT_CREDS_PATH

        assert CONFIG_DIR == Path.home() / ".config" / "tahuti"
        assert DEFAULT_CONFIG_PATH == CONFIG_DIR / "config.json"
        assert DEFAULT_CREDS_PATH == CONFIG_DIR / "creds.json"

    def test_unknown_attribute_still_raises(self):
        import tahuti.config as config

        with pytest.raises(AttributeError):
            config.THIS_NAME_DOES_NOT_EXIST


# ── `login` walks a fresh device through domain, school, email ────────────


class TestLoginInteractiveSetup:
    """`_prompt_login_setup`: ask only for what is unknown, and only for login."""

    def _login_args(self, *extra):
        return m.build_parser().parse_args(["login", "--format", "json", *extra])

    def _write_profile(self, school=None, domain=None, email=None, cookie="cookieval"):
        """Persist real state so `_prompt_login_setup` sees known values.

        Goes through the actual save functions rather than hand-writing JSON:
        the saved domain lives in *both* config.json and session.json, so a
        session-only file would leave the profile reporting "unset" and make the
        prompt fire when it should not. `domain=None` means genuinely unset and
        is stored as null — `ProfileConfig.domain` no longer defaults it to
        "managebac.com", which is what makes "unknown" observable here at all.
        """
        state = m.load_state(None, None, None)
        state.profile.school = school
        state.profile.domain = domain
        state.profile.email = email
        state.session.school = school
        state.session.domain = domain
        state.session.email = email
        state.session.cookie = cookie
        m.save_profile(state)
        m.save_session(state)

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

    def test_fresh_device_is_asked_in_use_order(self, isolated_env):
        """domain, then school, then email — the order they are actually used."""
        args = self._login_args()
        asked = self._run(args, ["", "myschool", "student@example.com"])
        assert asked == [
            "Base domain [managebac.com]: ",
            "School subdomain (e.g. myschool): ",
            "Email: ",
        ]
        assert args.domain == "managebac.com"  # empty means the default
        assert args.school == "myschool"
        assert args.email == "student@example.com"

    def test_configured_device_is_asked_for_nothing(self, isolated_env):
        """School, email and domain are all known, so nothing is re-asked.

        The domain included. It used to be exempted from the "only if unknown"
        rule — always confirmed, on the grounds that it always had a value — and
        that exemption was the defect: a ``managebac.cn`` operator was asked to
        re-confirm their domain on every single interactive login. Now that
        "unset" is representable (``ProfileConfig.domain`` defaults to ``None``),
        a saved domain is a known value like any other and the question is
        skipped. The flag is still the override when it *is* wanted — see
        ``test_explicit_domain_flag_suppresses_the_prompt`` for the mirror case.
        """
        self._write_profile(
            school="myschool", domain="managebac.cn", email="student@example.com"
        )
        args = self._login_args()
        asked = self._run(args, [])
        assert asked == []
        assert args.domain is None
        assert args.school is None
        assert args.email is None

    def test_domain_prompt_offers_the_builtin_default(self, isolated_env):
        """A fresh device shows managebac.com rather than failing or guessing."""
        args = self._login_args()
        asked = self._run(args, ["", "myschool", "student@example.com"])
        assert asked[0] == "Base domain [managebac.com]: "
        assert args.domain == "managebac.com"

    def test_explicit_domain_flag_suppresses_the_prompt(self, isolated_env):
        """`--domain` is the override; it must not be second-guessed."""
        args = self._login_args("--domain", "managebac.cn")
        asked = self._run(args, ["myschool", "student@example.com"])
        assert asked == ["School subdomain (e.g. myschool): ", "Email: "]
        assert args.domain == "managebac.cn"

    def test_only_the_missing_field_is_asked(self, isolated_env):
        """Partial config prompts the gap, not the whole questionnaire."""
        args = self._login_args("--school", "myschool")
        asked = self._run(args, ["", "student@example.com"])
        assert asked == ["Base domain [managebac.com]: ", "Email: "]

    def test_flags_count_as_known_and_suppress_every_prompt(self, isolated_env):
        args = self._login_args(
            "--school", "myschool", "--domain", "managebac.cn", "--email", "a@b.c"
        )
        asked = self._run(args, [])
        assert asked == []

    def test_non_interactive_stdin_prompts_nothing(self, isolated_env):
        """The daemon and CI reach this path; they must never block on input."""
        args = self._login_args()
        with (
            patch.object(m, "_stdin_is_interactive", return_value=False),
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
        ):
            m._prompt_login_setup(args)
        assert args.school is None
        assert args.domain is None
        assert args.email is None

    def test_closed_stdin_is_treated_as_non_interactive(self, isolated_env):
        """isatty() can raise on a detached fd; that must not crash login."""
        args = self._login_args()
        real = sys.stdin
        try:
            m.sys.stdin = None  # None.isatty() raises AttributeError
            assert m._stdin_is_interactive() is False
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                m._prompt_login_setup(args)
        finally:
            m.sys.stdin = real
        assert args.domain is None

    def test_cookie_login_skips_the_questionnaire(self, isolated_env):
        """A cookie needs no email or domain walk-through."""
        args = self._login_args("--cookie", "abc123")
        with (
            patch.object(m, "_stdin_is_interactive", return_value=True),
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
        ):
            m._prompt_login_setup(args)
        assert args.domain is None

    def test_empty_school_is_re_asked(self, isolated_env):
        """An empty school would build a URL with no host, so keep asking."""
        args = self._login_args("--domain", "managebac.com")
        asked = self._run(args, ["", "   ", "myschool", "student@example.com"])
        assert asked == [
            "School subdomain (e.g. myschool): ",
            "School subdomain (e.g. myschool): ",
            "School subdomain (e.g. myschool): ",
            "Email: ",
        ]
        assert args.school == "myschool"

    def test_unreadable_state_file_still_prompts(self, isolated_env, monkeypatch):
        """A corrupt state file must not turn into a traceback on the way in."""
        Path(os.environ["MANAGEBAC_SESSION"]).write_text("{not json")
        args = self._login_args()
        asked = self._run(args, ["", "myschool", "student@example.com"])
        assert asked[0] == "Base domain [managebac.com]: "

    def test_other_commands_never_prompt(self, isolated_env):
        """The gate: only `login` may block waiting on a human."""
        args = m.build_parser().parse_args(["list", "--format", "json"])
        with (
            patch.object(m, "build_client", side_effect=SystemExit(0)),
            patch.object(m.getpass, "getpass", return_value="typed"),
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
        ):
            with pytest.raises(SystemExit):
                m._build_client(args, "list")

    def test_login_still_prompts_for_the_password_last(self, isolated_env):
        """Order guarantee: the setup questions precede the credential one."""
        order = []
        args = self._login_args()
        with (
            patch.object(m, "_stdin_is_interactive", return_value=True),
            patch.object(m.getpass, "getpass", lambda *a, **k: order.append("password") or "pw"),
            patch.object(m, "build_client", side_effect=SystemExit(0)),
        ):
            def fake_input(prompt=""):
                order.append(prompt)
                return {"Base domain [managebac.com]: ": "",
                        "School subdomain (e.g. myschool): ": "myschool",
                        "Email: ": "student@example.com"}[prompt]

            with patch("builtins.input", side_effect=fake_input):
                with pytest.raises(SystemExit):
                    m._build_client(args, "login")
        assert order == [
            "Base domain [managebac.com]: ",
            "School subdomain (e.g. myschool): ",
            "Email: ",
            "password",
        ]
