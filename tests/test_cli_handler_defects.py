"""Regression tests for the CLI command-handler defects.

Every test here was written to fail against the code as it shipped and pass
after the fix in ``tahuti/__main__.py``. They are grouped by the defect they
pin, in the order the defects were reported:

1. ``daemon configure-channel`` was a write-only stub that silently succeeded
   and then POSTed to a hardcoded localhost webhook.
2. ``daemon start --once`` computed alerts, delivered nothing, and exited 0.
3. the session write had two owners, so ``login --temp`` persisted a reusable
   session cookie (the flag is gone; the split now has one owner).
4. ``logout`` resolved the account with inverted precedence and cleared the
   wrong cache directory / keychain entry.
5. ``download`` reported success when the detail fetch had failed.
6. ``daemon stop`` / ``daemon status`` always exited 0.
7. A ``--pages``-limited crawl marked every unseen task as deleted, persistently.
8. ``--cache-ttl`` did not gate the snapshot cache, and ``meta.details`` was
   reported true on the cached path.
9. ``daemon stop --pid-file`` reported the config's pid path as its own.
10. ``download`` fetched unvalidated URLs from a scraped detail page.

Nothing here touches ``~/.config/tahuti``: every state path is redirected to
``tmp_path`` (including the response cache, whose module-level default is
import-time).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tahuti.daemon import DEFAULT_WEBHOOK_URL
from tahuti.__main__ import (
    _snapshot_path,
    build_parser,
    cmd_daemon_configure_channel,
    cmd_daemon_start,
    cmd_list,
    cmd_logout,
    main,
    merge_snapshot,
)

DOWNLOAD_HOST = "https://myschool.managebac.cn"


# ── Shared harness ──────────────────────────────────────────────────────


def _isolate_state(tmp_path, monkeypatch):
    """Point every state path (including the response cache) at tmp_path.

    ``DEFAULT_CACHE_DIR``, ``DEFAULT_SNAPSHOT_PATH``, ``DEFAULT_PID_PATH``,
    ``DEFAULT_LOG_PATH`` and ``DEFAULT_DAEMON_PATH`` are all computed from
    ``config_dir()`` at *module import time*, so no ``MANAGEBAC_*`` variable
    and no ``HOME`` override can reach them — they have to be patched in place.
    ``daemon/__init__.py`` additionally re-binds the pid/log defaults via
    ``from .system import ...``, giving a second independent binding that
    ``load_daemon_config`` reads, so both are patched.
    """
    for var in (
        "MANAGEBAC_KEYCHAIN",
        "MANAGEBAC_PASSWORD",
        "MANAGEBAC_COOKIE",
        "MANAGEBAC_CREDS_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))
    monkeypatch.setenv("MANAGEBAC_CREDS_PATH", str(tmp_path / "creds.json"))
    monkeypatch.setattr("tahuti.cache.DEFAULT_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("tahuti.__main__.DEFAULT_SNAPSHOT_PATH", tmp_path / "snapshot.json")
    for module in ("tahuti.daemon.system", "tahuti.daemon"):
        monkeypatch.setattr(f"{module}.DEFAULT_PID_PATH", tmp_path / "daemon.pid")
        monkeypatch.setattr(f"{module}.DEFAULT_LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("tahuti.daemon.DEFAULT_DAEMON_PATH", tmp_path / "daemon.json")
    monkeypatch.setattr(
        "tahuti.daemon.DEFAULT_SNAPSHOT_PATH", tmp_path / "snapshot.json"
    )
    return tmp_path


class TestStatePathsAreIsolated:
    """The import-time constants are the trap these tests could fall into.

    ``daemon/__init__.py`` does ``from .system import DEFAULT_LOG_PATH,
    DEFAULT_PID_PATH``, so patching only ``daemon.system`` leaves the
    package-level name that ``load_daemon_config`` actually reads pointing at
    the operator's real home. Both bindings have to move together.
    """

    def test_load_daemon_config_resolves_into_tmp_path(self, tmp_path, monkeypatch):
        _isolate_state(tmp_path, monkeypatch)
        from tahuti.daemon import load_daemon_config

        config = load_daemon_config()
        assert config["pid_file"] == str(tmp_path / "daemon.pid")
        assert config["log_file"] == str(tmp_path / "daemon.log")
        assert config["snapshot_file"] != str(Path.home() / ".config" / "tahuti" / "snapshot.json")

    def test_no_state_file_lands_outside_tmp_path(self, tmp_path, monkeypatch):
        """End to end: a full `logout` must not touch the real state dir."""
        _isolate_state(tmp_path, monkeypatch)
        _write_state(tmp_path, "profile@example.com", "other@example.com")
        real = Path.home() / ".config" / "tahuti"
        before = {p for p in real.glob("*")} if real.is_dir() else set()
        with patch("builtins.print"):
            with pytest.raises(SystemExit):
                main(["logout", "--format", "json"])
        after = {p for p in real.glob("*")} if real.is_dir() else set()
        assert after - before == set(), f"logout wrote outside tmp_path: {after - before}"


def _capture_payload():
    captured: dict = {}

    def _capture(payload, output, fmt):
        captured["payload"] = payload
        captured["output"] = output
        captured["fmt"] = fmt

    return captured, _capture


class _DaemonArgs:
    """The argparse namespace ``daemon start`` actually receives."""

    def __init__(self, **overrides):
        self.background = False
        self.profile = None
        self.config = None
        self.session_file = None
        self.school = "myschool"
        self.domain = "managebac.cn"
        self.email = "student@example.com"
        self.password = None
        self.cookie = None
        self.daemon_config = None
        self.webhook_url = None
        self.secret = None
        self.channel_id = None
        self.recipient = None
        self.poll_interval = None
        self.interval = None
        self.active_hours_start = None
        self.active_hours_end = None
        self.dry_run = False
        self.once = False
        self.no_verify_tls = False
        self.pid_file = None
        self.log_file = None
        self.output = None
        self.format = "json"
        self.temp = False
        self.retry = 3
        self.refresh = False
        self.reauth = False
        self.keychain = None
        self.cache_ttl = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _ListArgs:
    """The argparse namespace ``list`` actually receives."""

    def __init__(self, **overrides):
        self.pages = None
        self.details = None
        self.view = None
        self.subject = None
        self.refresh = False
        self.cache_ttl = None
        self.graded = None
        self.submitted = None
        self.grade = None
        self.tag = None
        self.completed = None
        self.todo = None
        self.deleted = False
        self.output = None
        self.format = "json"
        self.profile = None
        self.config = None
        self.session_file = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _LogoutArgs:
    def __init__(self, **overrides):
        self.profile = None
        self.config = None
        self.session_file = None
        self.all = False
        self.keep_cache = False
        self.keep_credentials = False
        self.output = None
        self.format = "json"
        for key, value in overrides.items():
            setattr(self, key, value)


def _task(link=f"{DOWNLOAD_HOST}/student/classes/1/core_tasks/1000099"):
    return {
        "id": "1000099",
        "title": "Homework 1",
        "link": link,
        "class_name": "Math",
        "due_date": "2099-01-01",
    }


def _task_list_state(tmp_path, domain="managebac.cn"):
    state = MagicMock()
    state.active_profile = "default"
    state.domain = domain
    state.profile.default_view = "all"
    state.profile.default_subject = ""
    state.profile.default_details = False
    state.profile.default_pages = 10
    state.profile.default_cache_ttl = 900
    state.config_path = tmp_path / "config" / "config.json"
    return state


# ── Defect 1 — configure-channel is a write-only stub ───────────────────


class TestConfigureChannelFailsLoudly:
    def test_configure_channel_does_not_silently_succeed(self, tmp_path, capsys):
        """It used to exit 0 and print a config back with delivery.mode set."""
        cfg = tmp_path / "daemon.json"
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "daemon",
                    "configure-channel",
                    "qq",
                    "123456789",
                    "--daemon-config",
                    str(cfg),
                    "--format",
                    "json",
                ]
            )
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "channel_delivery_not_implemented"
        # Nothing was written: the old body persisted a key nothing reads.
        assert not cfg.exists()

    def test_configure_channel_error_names_the_silent_fallback(self, capsys):
        rc = cmd_daemon_configure_channel(
            MagicMock(
                channel_id="qq",
                recipient="123456789",
                daemon_config=None,
                output=None,
                format="json",
            )
        )
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert DEFAULT_WEBHOOK_URL in payload["error"]["message"]

    def test_daemon_run_channel_flags_are_refused(self):
        """`daemon run --channel-id` used to write delivery.mode and POST localhost."""
        args = build_parser().parse_args(
            ["daemon", "run", "--channel-id", "qq", "--recipient", "123456789"]
        )
        from tahuti.exceptions import CommandError

        with patch("tahuti.__main__._build_client") as build:
            with pytest.raises(CommandError) as exc_info:
                from tahuti.__main__ import cmd_daemon_run

                cmd_daemon_run(args)
        assert exc_info.value.code == "channel_delivery_not_implemented"
        build.assert_not_called()


# ── Defect 2 — `daemon start --once` never delivers ─────────────────────


class TestDaemonStartOnceReportsTruthfully:
    def _start_once(self, loop_result, **arg_overrides):
        state = MagicMock()
        state.active_profile = "default"
        client = MagicMock()
        captured, capture = _capture_payload()
        with (
            patch(
                "tahuti.__main__._build_client",
                return_value=(state, client, "student@example.com"),
            ),
            patch("tahuti.__main__._authenticate_client"),
            patch("tahuti.__main__.load_daemon_config", return_value={}),
            patch("tahuti.__main__.start_loop", return_value=loop_result) as loop,
            patch("tahuti.__main__.print_payload", side_effect=capture),
        ):
            rc = cmd_daemon_start(_DaemonArgs(once=True, **arg_overrides))
        return rc, captured, loop

    def test_once_with_undelivered_alerts_exits_nonzero(self):
        """The shipped `once` branch returns `delivered: False` literally."""
        rc, captured, _ = self._start_once(
            {
                "alerts": [{"type": "new_overdue", "message": "Task is overdue"}],
                "alert_count": 1,
                "detail_fetches": 1,
                "delivered": False,
                "snapshot_file": "/tmp/snap.json",
            },
            webhook_url="https://hooks.example.com/x",
        )
        assert rc == 1
        payload = captured["payload"]
        assert payload["ok"] is False
        assert payload["error"]["code"] == "delivery_failed"
        # The evidence has to survive the failure report.
        assert payload["data"]["alert_count"] == 1

    def test_once_with_delivered_alerts_exits_zero(self):
        rc, captured, _ = self._start_once(
            {
                "alerts": [{"type": "new_overdue", "message": "overdue"}],
                "alert_count": 1,
                "delivered": True,
            }
        )
        assert rc == 0
        assert captured["payload"]["ok"] is True

    def test_once_with_no_alerts_is_a_successful_quiet_cycle(self):
        rc, captured, _ = self._start_once(
            {"alerts": [], "alert_count": 0, "delivered": False}
        )
        assert rc == 0
        assert captured["payload"]["ok"] is True

    def test_dry_run_may_deliver_nothing(self):
        """`--dry-run` exists to not deliver, so it must not be a failure."""
        rc, captured, _ = self._start_once(
            {
                "alerts": [{"type": "new_overdue", "message": "overdue"}],
                "alert_count": 1,
                "delivered": False,
            },
            dry_run=True,
        )
        assert rc == 0
        assert captured["payload"]["ok"] is True
        assert captured["payload"]["data"]["dry_run"] is True
        assert captured["payload"]["data"]["delivered"] is False


# ── Defect 3 — the session write had two owners ─────────────────────────
#
# `login --temp` was meant to leave nothing on disk, but `build_client` saved
# the session on its own path while `_authenticate_client` saved it again from
# outside behind a `persist` switch — so the flag was overruled by whichever
# owner ran last. The redesign deletes the switch and the second owner: the
# session *is* the persistent session, so it is always written, and the two new
# flags (`--keep-credentials`, `--no-remember-me`) govern the password and the
# `remember_me` field and nothing else. These pin that at CLI level, with only
# the HTTP layer mocked.


class TestLoginAlwaysPersistsTheSession:
    def _run_login(self, tmp_path, monkeypatch, capsys, extra_argv=()):
        _isolate_state(tmp_path, monkeypatch)
        argv = [
            "login",
            "--school",
            "myschool",
            "-d",
            "managebac.cn",
            "-e",
            "student@example.com",
            "-p",
            "not-a-real-password",
            "--format",
            "json",
            *extra_argv,
        ]
        # Only the HTTP layer is mocked; build_client and cmd_login run for real
        # so the flags have to survive both of them.
        with patch("tahuti.auth.ManageBacClient") as client_cls:
            client = client_cls.return_value
            client.login.return_value = True
            client.session.cookies.get.return_value = "reusable-cookie"
            client.school = "myschool"
            client.domain = "managebac.cn"
            client.base = DOWNLOAD_HOST
            with pytest.raises(SystemExit) as exc_info:
                main(argv)
        return exc_info.value.code, json.loads(capsys.readouterr().out), client_cls

    def test_login_persists_the_session_cookie(self, tmp_path, monkeypatch, capsys):
        """The point of a persistent session: no password prompt next time."""
        code, _, _ = self._run_login(tmp_path, monkeypatch, capsys)
        assert code == 0
        session_file = Path(os.environ["MANAGEBAC_SESSION"])
        assert session_file.exists()
        saved = json.loads(session_file.read_text())
        assert saved["cookie"] == "reusable-cookie"

    def test_no_password_is_saved_without_the_flag(self, tmp_path, monkeypatch, capsys):
        code, payload, _ = self._run_login(tmp_path, monkeypatch, capsys)
        assert code == 0
        assert payload["data"]["credentials_saved"] is False
        assert not Path(os.environ["MANAGEBAC_CREDS_PATH"]).exists()

    @pytest.mark.parametrize(
        "flag", ["--keep-credentials", "--no-remember-me", "--keep-credentials --no-remember-me"]
    )
    def test_neither_new_flag_suppresses_the_session(
        self, tmp_path, monkeypatch, capsys, flag
    ):
        """The old `--temp` suppressed all four things at once; these are scoped."""
        code, _, _ = self._run_login(tmp_path, monkeypatch, capsys, flag.split())
        assert code == 0
        session_file = Path(os.environ["MANAGEBAC_SESSION"])
        assert session_file.exists()
        saved = json.loads(session_file.read_text())
        assert saved["cookie"] == "reusable-cookie"

    def test_keep_credentials_saves_the_password(self, tmp_path, monkeypatch, capsys):
        _, payload, _ = self._run_login(
            tmp_path, monkeypatch, capsys, ["--keep-credentials"]
        )
        assert payload["data"]["credentials_saved"] is True
        creds = json.loads(Path(os.environ["MANAGEBAC_CREDS_PATH"]).read_text())
        assert creds["password"] == "not-a-real-password"


# ── Defect 4 — `logout` resolved the account with inverted precedence ───


def _write_state(tmp_path, profile_email, session_email, active_profile="default"):
    config = {
        "version": 1,
        "active_profile": active_profile,
        "profiles": {
            active_profile: {
                "school": "myschool",
                "domain": "managebac.cn",
                "email": profile_email,
                "defaults": {},
            }
        },
    }
    session = {
        "version": 1,
        "active_profile": active_profile,
        "profiles": {
            active_profile: {
                "school": "myschool",
                "domain": "managebac.cn",
                "email": session_email,
                "base_url": DOWNLOAD_HOST,
                "cookie": "a-session-cookie",
                "logged_in_at": "2026-09-19T00:00:00",
            }
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "session.json").write_text(json.dumps(session))


def _cache_dir_for(email):
    digest = hashlib.sha256(email.encode()).hexdigest()[:16]
    return digest


class TestLogoutClearsTheRightAccount:
    def test_logout_clears_the_profile_email_cache_not_the_session_one(
        self, tmp_path, monkeypatch, capsys
    ):
        """Inverted precedence hashed the *session* email's cache directory.

        login keys the cache on the profile email, so clearing the other
        directory left the grade pages and hub JWT in place while deleting a
        different profile's entries.
        """
        _isolate_state(tmp_path, monkeypatch)
        _write_state(tmp_path, "profile@example.com", "other@example.com")
        cache_root = tmp_path / "cache"
        mine = cache_root / _cache_dir_for("profile@example.com")
        theirs = cache_root / _cache_dir_for("other@example.com")
        for d in (mine, theirs):
            d.mkdir(parents=True)
            (d / "entry.json").write_text("{}")

        with pytest.raises(SystemExit) as exc_info:
            main(["logout", "--format", "json"])

        assert exc_info.value.code == 0
        assert list(mine.glob("*.json")) == [], "logout missed the live account"
        assert list(theirs.glob("*.json")), "logout deleted another profile's cache"

    def test_logout_deletes_the_keychain_entry_for_the_profile_email(
        self, tmp_path, monkeypatch
    ):
        _isolate_state(tmp_path, monkeypatch)
        _write_state(tmp_path, "profile@example.com", "other@example.com")
        with patch("tahuti.keychain.delete", return_value=True) as delete:
            with patch("builtins.print"):
                with pytest.raises(SystemExit):
                    main(["logout", "--format", "json"])
        assert delete.call_args[0][0] == "profile@example.com"

    def test_logout_removes_the_creds_file_login_wrote(
        self, tmp_path, monkeypatch
    ):
        _isolate_state(tmp_path, monkeypatch)
        _write_state(tmp_path, "profile@example.com", "other@example.com")
        creds = Path(os.environ["MANAGEBAC_CREDS_PATH"])
        creds.write_text(json.dumps({"email": "profile@example.com", "password": "x"}))
        with patch("builtins.print"):
            with pytest.raises(SystemExit):
                main(["logout", "--format", "json"])
        assert not creds.exists()


# ── Defect 5 — `view` reported success on a failed detail fetch ──────────


class TestViewDetailFailure:
    def test_view_error_shaped_detail_is_not_a_success(self):
        from tahuti.__main__ import cmd_view

        for target, ident in (
            (None, "1000099"),  # the id branch
            (f"{DOWNLOAD_HOST}/student/classes/1/core_tasks/1000099", None),
        ):
            client = MagicMock()
            client.base = DOWNLOAD_HOST
            client.get_task_detail.return_value = {"error": "Session expired"}
            client.find_task_by_id.return_value = None
            state = MagicMock()
            state.active_profile = "default"
            args = MagicMock(
                target=target,
                id=ident,
                url=None,
                subject=None,
                pages=10,
                refresh=False,
                output=None,
                format="json",
            )
            captured, capture = _capture_payload()
            with (
                patch(
                    "tahuti.__main__._build_client",
                    return_value=(state, client, "student@example.com"),
                ),
                patch("tahuti.__main__._authenticate_client"),
                patch("tahuti.__main__.load_snapshot", return_value={}),
                patch("tahuti.__main__.find_task_by_id", return_value=_task()),
                patch("tahuti.__main__.print_payload", side_effect=capture),
            ):
                rc = cmd_view(args)
            assert rc == 1, f"view branch {target!r} reported success"
            payload = captured["payload"]
            assert payload["ok"] is False
            assert payload["error"]["code"] == "detail_fetch_failed"
            assert "Session expired" in payload["error"]["message"]


# ── Defect 7 — a partial crawl marked everything unseen as deleted ──────


class TestPartialCrawlDoesNotInferDeletions:
    def _snapshot(self, ids):
        return {
            "student_name": "Student",
            "school": "myschool",
            "base_url": DOWNLOAD_HOST,
            "crawled_at": "2026-09-19T00:00:00",
            "upcoming": [
                {
                    "id": tid,
                    "title": f"Task {tid}",
                    "link": f"{DOWNLOAD_HOST}/student/classes/1/core_tasks/{tid}",
                    "class_name": "Math",
                    "due_date": "2099-01-01",
                }
                for tid in ids
            ],
            "past": [],
            "overdue": [],
        }

    def _crawl(self, ids):
        snap = self._snapshot(ids)
        snap["summary"] = {"upcoming_count": len(ids)}
        return snap

    def test_merge_snapshot_marks_deletions_only_for_a_full_crawl(self):
        old = self._snapshot(["1", "2", "3"])
        new = self._crawl(["1"])
        merged = merge_snapshot(old, new)
        deleted = {
            t["id"] for t in merged["upcoming"] if t.get("deleted_from_server")
        }
        assert deleted == {"2", "3"}

    def test_merge_snapshot_skips_deletions_for_a_partial_crawl(self):
        old = self._snapshot(["1", "2", "3"])
        new = self._crawl(["1"])
        merged = merge_snapshot(old, new, partial=True)
        deleted = {
            t["id"] for t in merged["upcoming"] if t.get("deleted_from_server")
        }
        assert deleted == set()

    def test_list_with_pages_one_keeps_the_tasks_it_did_not_crawl(
        self, tmp_path, capsys
    ):
        """`list --pages 1` used to flag 200+ tasks deleted and persist it."""
        state = _task_list_state(tmp_path)
        snapshot_path = state.config_path.parent / "snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(self._snapshot(["1", "2", "3"])))

        client = MagicMock()
        client.domain = "managebac.cn"
        client.crawl_all.return_value = self._crawl(["1"])

        captured, capture = _capture_payload()
        with (
            patch(
                "tahuti.__main__._build_client",
                return_value=(state, client, "student@example.com"),
            ),
            patch("tahuti.__main__._authenticate_client"),
            patch("tahuti.__main__.print_payload", side_effect=capture),
        ):
            rc = cmd_list(_ListArgs(pages=1))

        assert rc == 0
        assert client.crawl_all.call_args.kwargs["max_pages"] == 1
        # They survive the filter…
        shown = {t["id"] for t in captured["payload"]["data"]["tasks"]["upcoming"]}
        assert shown == {"1", "2", "3"}
        # …and the deletion is not written to disk either.
        saved = json.loads(snapshot_path.read_text())
        persisted = {
            t["id"] for t in saved["upcoming"] if t.get("deleted_from_server")
        }
        assert persisted == set()

    def test_list_without_pages_still_detects_real_deletions(self, tmp_path):
        """Control: a full crawl must still be able to infer a deletion."""
        state = _task_list_state(tmp_path)
        snapshot_path = state.config_path.parent / "snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(self._snapshot(["1", "2", "3"])))

        client = MagicMock()
        client.domain = "managebac.cn"
        client.crawl_all.return_value = self._crawl(["1"])

        captured, capture = _capture_payload()
        with (
            patch(
                "tahuti.__main__._build_client",
                return_value=(state, client, "student@example.com"),
            ),
            patch("tahuti.__main__._authenticate_client"),
            patch("tahuti.__main__.print_payload", side_effect=capture),
        ):
            rc = cmd_list(_ListArgs())

        assert rc == 0
        shown = {t["id"] for t in captured["payload"]["data"]["tasks"]["upcoming"]}
        assert shown == {"1"}
        saved = json.loads(snapshot_path.read_text())
        persisted = {
            t["id"] for t in saved["upcoming"] if t.get("deleted_from_server")
        }
        assert persisted == {"2", "3"}


# ── Defect 8 — `--cache-ttl` did not gate the snapshot ──────────────────


class TestCacheTtlGatesTheSnapshot:
    def _fresh_snapshot(self, tmp_path, age_seconds=300):
        """A snapshot crawled *age_seconds* ago — stale under a 30s TTL, fresh
        under the old hardcoded 900."""
        from datetime import datetime, timedelta

        state = _task_list_state(tmp_path)
        snapshot_path = state.config_path.parent / "snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(
                {
                    "student_name": "Student",
                    "school": "myschool",
                    "base_url": DOWNLOAD_HOST,
                    "crawled_at": (
                        datetime.now() - timedelta(seconds=age_seconds)
                    ).isoformat(),
                    "upcoming": [
                        {
                            "id": "1",
                            "title": "Cached task",
                            "link": f"{DOWNLOAD_HOST}/student/classes/1/core_tasks/1",
                            "class_name": "Math",
                            "due_date": "2099-01-01",
                        }
                    ],
                    "past": [],
                    "overdue": [],
                }
            )
        )
        return state

    def _run_list(self, tmp_path, state, args):
        client = MagicMock()
        client.domain = "managebac.cn"
        client.crawl_all.return_value = {
            "student_name": "Fresh",
            "school": "myschool",
            "base_url": DOWNLOAD_HOST,
            "crawled_at": "2026-09-19T00:00:00",
            "upcoming": [],
            "past": [],
            "overdue": [],
        }
        captured, capture = _capture_payload()
        with (
            patch(
                "tahuti.__main__._build_client",
                return_value=(state, client, "student@example.com"),
            ),
            patch("tahuti.__main__._authenticate_client"),
            patch("tahuti.__main__.print_payload", side_effect=capture),
        ):
            rc = cmd_list(args)
        return rc, captured, client

    def test_short_cache_ttl_forces_a_recrawl(self, tmp_path):
        """A 30s TTL must expire a snapshot crawled five minutes ago."""
        state = self._fresh_snapshot(tmp_path)
        rc, captured, client = self._run_list(
            tmp_path, state, _ListArgs(cache_ttl=30)
        )
        assert rc == 0
        client.crawl_all.assert_called_once()

    def test_long_cache_ttl_reuses_the_snapshot(self, tmp_path):
        state = self._fresh_snapshot(tmp_path, age_seconds=300)
        rc, captured, client = self._run_list(
            tmp_path, state, _ListArgs(cache_ttl=3600)
        )
        assert rc == 0
        client.crawl_all.assert_not_called()
        assert captured["payload"]["data"]["meta"]["snapshot_source"] == "cache"

    def test_cached_payload_does_not_claim_a_detail_fetch(self, tmp_path):
        """`meta.details` was computed from the flag, not from what happened."""
        state = self._fresh_snapshot(tmp_path, age_seconds=300)
        rc, captured, client = self._run_list(
            tmp_path, state, _ListArgs(cache_ttl=3600, details=True)
        )
        meta = captured["payload"]["data"]["meta"]
        assert meta["details"] is False, "cached path claimed a detail fetch"

    def test_crawling_payload_reports_the_detail_fetch(self, tmp_path):
        state = self._fresh_snapshot(tmp_path)
        rc, captured, client = self._run_list(
            tmp_path, state, _ListArgs(cache_ttl=30, details=True)
        )
        meta = captured["payload"]["data"]["meta"]
        assert meta["details"] is True
        assert meta["snapshot_source"] == "crawl"

