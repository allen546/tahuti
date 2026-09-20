"""Tests for tahuti.daemon."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as rm

# Ensure local src takes precedence over editable installs
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tahuti.daemon import (
    DEFAULT_WEBHOOK_URL,
    _diff_snapshots_full,
    _is_tahuti_pid,
    configure_webhook,
    load_daemon_config,
    run_daemon_once,
    save_daemon_config,
    start_loop,
    stop_daemon,
)


class TestLoadDaemonConfig:
    def test_default_when_no_file(self, tmp_path: Path):
        config = load_daemon_config(str(tmp_path / "nonexistent.json"))
        assert config["delivery"]["webhook_url"] == DEFAULT_WEBHOOK_URL
        assert config["verify_tls"] is True
        assert "active_windows" in config

    def test_loads_existing_file(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        path.write_text(
            json.dumps(
                {
                    "delivery": {
                        "mode": "webhook",
                        "webhook_url": "http://custom:9999/webhook",
                    },
                    "active_windows": [["09:00", "17:00"]],
                }
            )
        )
        config = load_daemon_config(str(path))
        assert config["delivery"]["webhook_url"] == "http://custom:9999/webhook"
        assert config["active_windows"] == [["09:00", "17:00"]]


class TestSaveDaemonConfig:
    def test_creates_file(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        data = {"webhook_url": "http://localhost:8080/webhook"}
        save_daemon_config(data, str(path))
        loaded = json.loads(path.read_text())
        assert loaded["webhook_url"] == "http://localhost:8080/webhook"

    def test_returns_path(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        result = save_daemon_config({"x": 1}, str(path))
        assert result == path


class TestDiffSnapshots:
    def test_new_overdue_alert(self, make_crawl_result):
        old = make_crawl_result(upcoming=[], overdue=[])
        new = make_crawl_result(
            upcoming=[],
            overdue=[{"id": "1", "title": "Overdue HW", "class_name": "Math"}],
        )
        alerts = _diff_snapshots_full(old, new)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "new_overdue"
        assert alerts[0]["severity"] == "high"
        assert "Overdue HW" in alerts[0]["message"]

    def test_new_upcoming_alert(self, make_crawl_result):
        old = make_crawl_result(upcoming=[], overdue=[])
        new = make_crawl_result(
            upcoming=[
                {
                    "id": "2",
                    "title": "New Task",
                    "due_date": "May 1",
                    "class_name": "Eng",
                }
            ],
            overdue=[],
        )
        alerts = _diff_snapshots_full(old, new)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "new_upcoming"
        assert alerts[0]["severity"] == "medium"

    def test_new_grade_alert(self, make_crawl_result, sample_task):
        old_task = {**sample_task, "grade_letter": None, "grade_score": None}
        new_task = {**sample_task, "grade_letter": "A", "grade_score": "95/100"}
        old = make_crawl_result(upcoming=[old_task])
        new = make_crawl_result(upcoming=[new_task])
        alerts = _diff_snapshots_full(old, new)
        grade_alerts = [a for a in alerts if a["type"] == "task_graded"]
        assert len(grade_alerts) == 1
        assert "A" in grade_alerts[0]["message"]

    def test_no_alerts_when_same(self, make_crawl_result, sample_task):
        old = make_crawl_result(upcoming=[sample_task])
        new = make_crawl_result(upcoming=[sample_task])
        alerts = _diff_snapshots_full(old, new)
        assert alerts == []

    def test_no_alert_for_existing_overdue(self, make_crawl_result):
        task = {"id": "1", "title": "Old overdue", "class_name": "Math"}
        old = make_crawl_result(overdue=[task])
        new = make_crawl_result(overdue=[task])
        alerts = _diff_snapshots_full(old, new)
        assert alerts == []


def test_canonical_snapshot_io_and_diff(tmp_path: Path):
    from tahuti.__main__ import load_snapshot, save_snapshot
    from tahuti.daemon import _diff_snapshots_full, diff_index

    # Snapshot save & load
    snap_file = tmp_path / "snap.json"
    save_snapshot(snap_file, {"upcoming": [{"id": "10", "title": "Math"}]})
    loaded = load_snapshot(snap_file)
    assert loaded["upcoming"][0]["id"] == "10"

    # diff_index detects new_upcoming
    old = {"upcoming": []}
    new = {"upcoming": [{"id": "10", "title": "Math"}]}
    alerts, changed_ids = diff_index(old, new)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "new_upcoming"
    assert changed_ids == ["10"]
    assert _diff_snapshots_full(old, new) == alerts

    # diff_index detects new_overdue
    old_ov = {"overdue": []}
    new_ov = {"overdue": [{"id": "11", "title": "History", "class_name": "Hist"}]}
    alerts_ov, changed_ov = diff_index(old_ov, new_ov)
    assert len(alerts_ov) == 1
    assert alerts_ov[0]["type"] == "new_overdue"
    assert changed_ov == ["11"]
    assert _diff_snapshots_full(old_ov, new_ov) == alerts_ov

    # diff_index detects task_graded
    old_gr = {"upcoming": [{"id": "12", "title": "Science", "grade_letter": None}]}
    new_gr = {
        "upcoming": [
            {
                "id": "12",
                "title": "Science",
                "grade_letter": "A",
                "grade_score": "98",
            }
        ]
    }
    alerts_gr, changed_gr = diff_index(old_gr, new_gr)
    assert len(alerts_gr) == 1
    assert alerts_gr[0]["type"] == "task_graded"
    assert changed_gr == ["12"]
    assert _diff_snapshots_full(old_gr, new_gr) == alerts_gr

    # diff_index detects newly created task with released score -> emits task_graded, suppresses new_upcoming
    old_new_gr = {"upcoming": []}
    new_new_gr = {
        "upcoming": [
            {
                "id": "15",
                "title": "Chinese Quiz",
                "grade_letter": "A",
                "grade_score": "90 / 100 pts",
            }
        ]
    }
    alerts_ng, changed_ng = diff_index(old_new_gr, new_new_gr)
    assert len(alerts_ng) == 1
    assert alerts_ng[0]["type"] == "task_graded"
    assert "Grade posted: Chinese Quiz" in alerts_ng[0]["message"]
    assert changed_ng == ["15"]

    # diff_index detects new_notifications
    old_notif = {"notifications": {"unread_count": 1}}
    new_notif = {"notifications": {"unread_count": 3}}
    alerts_notif, changed_notif = diff_index(old_notif, new_notif)
    assert len(alerts_notif) == 1
    assert alerts_notif[0]["type"] == "new_notifications"
    assert "2 new notification(s)" in alerts_notif[0]["message"]
    assert changed_notif == []
    assert _diff_snapshots_full(old_notif, new_notif) == alerts_notif


class TestConfigureWebhook:
    def test_saves_url(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        config = configure_webhook("http://new:8080/hook", str(path))
        assert config["delivery"]["webhook_url"] == "http://new:8080/hook"
        loaded = json.loads(path.read_text())
        assert loaded["delivery"]["webhook_url"] == "http://new:8080/hook"


class TestRunDaemonOnce:
    def test_dry_run_no_webhook(self, tmp_path: Path, make_crawl_result):
        snapshot_path = tmp_path / "snapshot.json"
        daemon_config = {
            "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result(
            upcoming=[
                {"id": "1", "title": "T1", "class_name": "Math", "due_date": "May 1"}
            ],
        )

        result = run_daemon_once(mock_client, daemon_config, dry_run=True)
        assert result["delivered"] is False
        assert result["alert_count"] >= 0

    def test_with_alerts_posts_webhook(self, tmp_path: Path, make_crawl_result):
        snapshot_path = tmp_path / "snapshot.json"
        daemon_config = {
            "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result(
            overdue=[{"id": "1", "title": "Overdue!", "class_name": "Math"}],
        )

        with rm.Mocker() as m:
            m.post("http://localhost:9999/webhook", status_code=200)
            result = run_daemon_once(mock_client, daemon_config, dry_run=False)
            assert result["delivered"] is True
            assert result["alert_count"] == 1

    def test_saves_snapshot(self, tmp_path: Path, make_crawl_result):
        snapshot_path = tmp_path / "snapshot.json"
        daemon_config = {
            "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        crawl_data = make_crawl_result(upcoming=[{"id": "1", "title": "T1"}])
        mock_client = MagicMock()
        mock_client.crawl_all.return_value = crawl_data

        with rm.Mocker() as m:
            m.post("http://localhost:9999/webhook", status_code=200)
            run_daemon_once(mock_client, daemon_config, dry_run=False)
            assert snapshot_path.exists()
            saved = json.loads(snapshot_path.read_text())
            assert saved["upcoming"][0]["id"] == "1"

    def test_dry_run_leaves_the_snapshot_baseline_alone(
        self, tmp_path: Path, make_crawl_result
    ):
        """A dry run must not advance the baseline the next real run diffs against.

        `save_snapshot` used to run unconditionally, before the `dry_run` check.
        The dry run then reported the alerts it found while leaving a snapshot
        claiming they had already been seen — so the next real run diffed against
        the dry run's own output, found nothing, and delivered nothing. The dry
        run ate the delivery it was only meant to preview.
        """
        snapshot_path = tmp_path / "snapshot.json"
        # A pre-existing baseline, as there always is on a real deployment.
        snapshot_path.write_text(json.dumps({"upcoming": [], "past": [], "overdue": []}))
        daemon_config = {
            "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result(
            upcoming=[{"id": "1", "title": "T1", "class_name": "Math"}],
        )

        with rm.Mocker() as m:
            # No POST is registered: a dry run that tried to deliver would fail
            # the request rather than silently succeed.
            result = run_daemon_once(mock_client, daemon_config, dry_run=True)

        # The alerts were still computed — a dry run has to show the work.
        assert result["delivered"] is False
        assert result["alert_count"] >= 1
        # ...but the baseline is byte-for-byte what it was before.
        assert json.loads(snapshot_path.read_text()) == {
            "upcoming": [],
            "past": [],
            "overdue": [],
        }

        # The payoff: the next *real* run still sees the alert.
        with rm.Mocker() as m:
            m.post("http://localhost:9999/webhook", status_code=200)
            real = run_daemon_once(mock_client, daemon_config, dry_run=False)
        assert real["delivered"] is True
        assert real["alert_count"] >= 1

    def test_the_daemon_still_fetches_notifications(
        self, tmp_path: Path, make_crawl_result
    ):
        """`run_daemon_once` must not opt out of the notification fetch.

        `diff_index` reads ``new["notifications"]["unread_count"]`` to raise the
        ``new_notifications`` alert, so passing ``fetch_notifications=False``
        here would zero that count and silence the alert entirely.  Every other
        test in this class hands `crawl_all` a mocked return value, so nothing
        else in the suite would notice — the call signature is the only place
        this is observable, which is why it is asserted exactly rather than by
        "the kwarg is absent".

        The keyword is deliberately *not* forwarded, so ``crawl_all``'s default
        of ``True`` stays in force for this one caller.
        """
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(json.dumps({"upcoming": [], "past": [], "overdue": []}))
        daemon_config = {
            "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result(
            upcoming=[{"id": "1", "title": "T1", "class_name": "Math"}],
        )

        run_daemon_once(mock_client, daemon_config, dry_run=True)
        assert mock_client.crawl_all.call_args.kwargs == {
            "max_pages": 10,
            "fetch_details": False,
        }

    def test_a_rising_unread_count_still_raises_the_alert(
        self, tmp_path: Path, make_crawl_result
    ):
        """The behavioural half of the guarantee the test above pins.

        With the notification payload present in the crawl result, `diff_index`
        must still raise ``new_notifications`` — proving the daemon's alerting
        really does depend on the key `run_daemon_once` asks `crawl_all` for.
        """
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps({"upcoming": [], "past": [], "overdue": [], "notifications": {"unread_count": 1}})
        )
        daemon_config = {
            "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        crawl = make_crawl_result(upcoming=[{"id": "1", "title": "T1", "class_name": "Math"}])
        crawl["notifications"] = {"unread_count": 3, "items": []}

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = crawl

        result = run_daemon_once(mock_client, daemon_config, dry_run=True)
        types = [a["type"] for a in result["alerts"]]
        assert "new_notifications" in types
        assert any("2 new notification(s)" in a["message"] for a in result["alerts"])


class TestStartLoop:
    def _make_daemon_config(self, tmp_path: Path):
        return {
            "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
            "snapshot_file": str(tmp_path / "snapshot.json"),
            "pid_file": str(tmp_path / "daemon.pid"),
            "log_file": str(tmp_path / "daemon.log"),
            "active_windows": [["00:00", "23:59"]],
        }

    def test_once_mode(self, tmp_path: Path, make_crawl_result):
        daemon_config = self._make_daemon_config(tmp_path)
        mock_client = MagicMock()
        mock_client.crawl_index.return_value = make_crawl_result()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        with (
            patch("tahuti.daemon._next_active_window", return_value=now),
            patch("tahuti.daemon._time_until", return_value=0.0),
        ):
            result = start_loop(mock_client, daemon_config, dry_run=True, once=True)
        assert "alerts" in result
        assert result["alert_count"] == 0

    def test_once_mode_dry_run_persists_nothing(self, tmp_path: Path):
        """A dry run must not consume the deliveries it only previewed.

        This replaced an earlier test that asserted a *snapshot* baseline survived
        a dry run. That test encoded a contract the daemon-lifecycle fix
        deliberately removed: `start_loop`'s `once` branch used to diff a
        snapshot and return before `DaemonService` existed, so
        `daemon start --once --webhook-url` exited 0 having sent nothing. The
        `once` branch now runs a real check cycle, and there is no snapshot to
        advance.

        The invariant that still matters moved with it: a dry run must persist
        nothing, because `mark_notification_processed` followed by `save()` is
        exactly what makes an event invisible to the *next* run — so one dry run
        would silently swallow the real delivery it only previewed.

        The guarantee cannot depend on who built the state manager, so this
        asserts on the flag `run_check_cycle` and `save()` actually consult.
        """
        daemon_config = self._make_daemon_config(tmp_path)

        for dry_run, expected_persist in ((True, False), (False, True)):
            seen = {}

            def capture(path=None, *, persist=True, _seen=seen):
                _seen["persist"] = persist
                return MagicMock()

            with (
                patch("tahuti.daemon.service.DaemonStateManager", side_effect=capture),
                patch("tahuti.daemon.service.MNNHubProvider"),
                patch("tahuti.daemon._next_active_window"),
                patch("tahuti.daemon._time_until", return_value=0.0),
            ):
                result = start_loop(
                    MagicMock(), daemon_config, dry_run=dry_run, once=True
                )

            assert "alert_count" in result, "the once path must still report"
            assert seen.get("persist") is expected_persist, (
                f"dry_run={dry_run} must build a "
                f"{'non-' if expected_persist is False else ''}persisting state manager"
            )

    def test_once_mode_cleans_pid(self, tmp_path: Path, make_crawl_result):
        pid_path = tmp_path / "daemon.pid"
        daemon_config = self._make_daemon_config(tmp_path)
        daemon_config["pid_file"] = str(pid_path)
        mock_client = MagicMock()
        mock_client.crawl_index.return_value = make_crawl_result()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        with (
            patch("tahuti.daemon._next_active_window", return_value=now),
            patch("tahuti.daemon._time_until", return_value=0.0),
        ):
            start_loop(mock_client, daemon_config, dry_run=True, once=True)
        assert not pid_path.exists()

    def test_cleans_pid_on_exception(self, tmp_path: Path, make_crawl_result):
        pid_path = tmp_path / "daemon.pid"
        daemon_config = self._make_daemon_config(tmp_path)
        daemon_config["pid_file"] = str(pid_path)
        mock_client = MagicMock()
        mock_client.crawl_index.return_value = make_crawl_result()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        with (
            patch("tahuti.daemon._next_active_window", return_value=now),
            patch("tahuti.daemon._time_until", return_value=0.0),
        ):
            start_loop(mock_client, daemon_config, dry_run=True, once=True)
        assert not pid_path.exists()
        assert (tmp_path / "daemon.log").exists()


class TestStopDaemon:
    def test_no_pid_file(self, tmp_path: Path):
        """`stop` on a config whose pid file is absent must report, not crash.

        This had no assertion at all, and built a `daemon_config` dict it then
        ignored — `stop_daemon` reads its config from the path it is given, so
        the local dict never reached it. Passing a nonexistent config path made
        `load_daemon_config` fall back to `DEFAULT_PID_PATH`, i.e. the real
        ~/.config/tahuti/daemon.pid, so the test also probed the operator's
        home directory. Writing the config makes the pid path explicit.
        """
        missing_pid = tmp_path / "nonexistent.pid"
        assert not missing_pid.exists()

        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(missing_pid)}))

        result = stop_daemon(str(config_path))

        assert result["stopped"] is False
        assert result["reason"] == "pid_file_missing"
        assert Path(result["pid_file"]) == missing_pid, (
            "stop_daemon reported a different pid file than the config names"
        )
        # Nothing to kill, and the missing pid file must not be conjured up.
        assert not missing_pid.exists()

    def test_no_pid_file_is_reported_when_config_is_absent(self, tmp_path: Path):
        """Same outcome when daemon.json itself is missing.

        `load_daemon_config` substitutes defaults, so this must still resolve to
        a pid path inside the test sandbox rather than the operator's home.
        """
        result = stop_daemon(str(tmp_path / "absent.json"))

        assert result["stopped"] is False
        assert result["reason"] == "pid_file_missing"
        assert tmp_path in Path(result["pid_file"]).parents, (
            f"{result['pid_file']} escaped the test sandbox"
        )

    def test_invalid_pid_content(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("not_a_number")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        result = stop_daemon(str(config_path))
        assert result["stopped"] is False
        assert result["reason"] == "invalid_pid"
        assert not pid_path.exists()

    def test_zero_pid(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("0")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        result = stop_daemon(str(config_path))
        assert result["stopped"] is False
        assert result["reason"] == "invalid_pid"

    def test_non_tahuti_process(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("99999")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        with patch("tahuti.daemon._is_tahuti_pid", return_value=False):
            result = stop_daemon(str(config_path))
            assert result["stopped"] is False
            assert result["reason"] == "not_tahuti_process"

    def test_valid_process_kills(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("12345")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        calls: list[tuple] = []

        def _fake_kill(pid, sig):
            calls.append((pid, sig))
            if sig == signal.SIGTERM:
                # The daemon exits on SIGTERM, as a healthy one does.
                raise ProcessLookupError
            raise AssertionError("SIGKILL must not be needed when SIGTERM works")

        with (
            patch("tahuti.daemon._is_tahuti_pid", return_value=True),
            patch("tahuti.daemon.os.kill", side_effect=_fake_kill),
        ):
            result = stop_daemon(str(config_path))

        assert result["stopped"] is True
        assert result["pid"] == 12345
        # SIGTERM is what gets sent to the right pid ...
        assert (12345, signal.SIGTERM) in calls
        # ... and nothing heavier, because the process was gone by the time the
        # liveness re-check ran. `stop_daemon` used to report success without
        # checking at all.
        assert not any(sig == signal.SIGKILL for _pid, sig in calls)
        assert not pid_path.exists()

    def test_stop_daemon_waits_for_the_process_to_actually_exit(self, tmp_path: Path):
        """A wedged daemon must not be reported stopped while it still runs."""
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("12345")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        with (
            patch("tahuti.daemon._is_tahuti_pid", return_value=True),
            patch(
                "tahuti.daemon.terminate_pid",
                return_value={"exited": False, "escalated": True},
            ) as term,
        ):
            result = stop_daemon(str(config_path))

        term.assert_called_once_with(12345)
        assert result["stopped"] is False
        assert result["reason"] == "did_not_exit"
        assert result["escalated_to_sigkill"] is True
        # Still running, so its pid file has to survive for the next attempt.
        assert pid_path.exists()


class TestIsTahutiPid:
    """The package-level guard mirrors the one in daemon/system.py."""

    @staticmethod
    def _ps(cmdline: str, returncode: int = 0):
        return SimpleNamespace(returncode=returncode, stdout=cmdline + "\n")

    @pytest.mark.parametrize(
        "cmdline",
        [
            f"{sys.executable} -m tahuti daemon run",
            "/opt/homebrew/bin/tahuti daemon run",
            "/usr/bin/python -m mb_crawler daemon run",
        ],
    )
    def test_accepts_a_tahuti_daemon(self, cmdline: str):
        with patch("tahuti.daemon.subprocess.run", return_value=self._ps(cmdline)):
            assert _is_tahuti_pid(4242) is True, cmdline

    @pytest.mark.parametrize(
        "cmdline", ["/lib/systemd/systemd --user", "/usr/sbin/cfprefsd daemon"]
    )
    def test_rejects_unrelated_processes(self, cmdline: str):
        with patch("tahuti.daemon.subprocess.run", return_value=self._ps(cmdline)):
            assert _is_tahuti_pid(4242) is False, cmdline
