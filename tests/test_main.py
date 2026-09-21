"""Tests for tahuti.__main__ (CLI entry-point)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure worktree src is prioritized over editable installs in venv
worktree_src = str(Path(__file__).resolve().parent.parent / "src")
if sys.path[0] != worktree_src:
    sys.path.insert(0, worktree_src)

import importlib
import tahuti

tahuti_pkg_dir = str(Path(worktree_src) / "tahuti")
if hasattr(tahuti, "__path__") and tahuti_pkg_dir not in tahuti.__path__:
    tahuti.__path__.insert(0, tahuti_pkg_dir)

if "tahuti.__main__" in sys.modules:
    importlib.reload(sys.modules["tahuti.__main__"])

from unittest.mock import MagicMock, patch

import pytest

from tahuti.__main__ import build_parser, main


class TestBuildParser:
    def test_has_all_subcommands(self):
        parser = build_parser()
        subactions = [
            a for a in parser._subparsers._actions if hasattr(a, "_parser_class")
        ]
        assert len(subactions) == 1
        subparser = subactions[0]
        choices = subparser.choices
        expected = {
            "login",
            "list",
            "view",
            "logout",
            "daemon",
            "submit",
            "notifications",
            "calendar",
            "timetable",
            "feedback",
        }
        assert set(choices.keys()) == expected

    def test_login_defaults(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "login",
                "--school",
                "myschool",
                "-e",
                "a@b.com",
                "-p",
                "pass",
                "-d",
                "managebac.cn",
            ]
        )
        assert args.school == "myschool"
        assert args.email == "a@b.com"
        assert args.password == "pass"
        assert args.domain == "managebac.cn"

    def test_list_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["list"])
        assert args.view is None
        assert args.details is None
        assert args.pages is None
        assert args.tag is None

    def test_list_tag_option(self):
        parser = build_parser()
        args1 = parser.parse_args(["list", "--tag", "Exam"])
        assert args1.tag == "Exam"
        args2 = parser.parse_args(["list", "-t", "Quiz"])
        assert args2.tag == "Quiz"

    def test_view_with_target(self):
        parser = build_parser()
        args = parser.parse_args(["view", "12345"])
        assert args.target == "12345"

    def test_submit_args(self):
        parser = build_parser()
        args = parser.parse_args(["submit", "12345", "file.pdf"])
        assert args.target == "12345"
        assert args.file == "file.pdf"

    def test_notifications_args(self):
        parser = build_parser()
        args = parser.parse_args(["notifications", "--page", "2", "--per-page", "10"])
        assert args.page == 2
        assert args.per_page == 10

    def test_daemon_subcommands(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "daemon",
                "start",
                "--once",
                "--dry-run",
                "--school",
                "myschool",
                "-e",
                "a@b.com",
                "-p",
                "x",
                "-c",
                "cookie",
            ]
        )
        assert args.daemon_command == "start"
        assert args.once is True
        assert args.dry_run is True

    def test_daemon_stop(self):
        parser = build_parser()
        args = parser.parse_args(["daemon", "stop"])
        assert args.daemon_command == "stop"

    def test_daemon_configure_webhook(self):
        parser = build_parser()
        args = parser.parse_args(
            ["daemon", "configure-webhook", "http://localhost:8080/hook"]
        )
        assert args.url == "http://localhost:8080/hook"

    def test_calendar_args(self):
        parser = build_parser()
        args = parser.parse_args(
            ["calendar", "--start", "2026-04-01", "--end", "2026-04-30", "--today"]
        )
        assert args.start == "2026-04-01"
        assert args.end == "2026-04-30"
        assert args.today is True

    def test_timetable_args(self):
        parser = build_parser()
        args = parser.parse_args(["timetable", "--date", "2026-04-28", "--today"])
        assert args.date == "2026-04-28"
        assert args.today is True

    def test_view_feedback_args(self):
        parser = build_parser()
        args = parser.parse_args(["view", "12345", "--feedback"])
        assert args.target == "12345"
        assert args.feedback is True

    def test_logout_args(self):
        parser = build_parser()
        args = parser.parse_args(["logout", "--keep-credentials"])
        assert args.keep_credentials is True


def _mock_build_client_result(mock_client, email="a@b.com"):
    """Create the (state, client, email) tuple that _build_client returns."""
    mock_state = MagicMock()
    mock_state.active_profile = "default"
    mock_state.profile.default_subject = ""
    mock_state.profile.default_view = "all"
    mock_state.profile.default_details = False
    mock_state.profile.default_pages = 10
    return mock_state, mock_client, email


class TestMainLogin:
    def test_login_success(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.school = "myschool"
        mock_client.domain = "managebac.cn"
        mock_client.base = "https://myschool.managebac.cn"
        mock_client.session.cookies.get.return_value = "session_cookie"

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(
                mock_client, "test@example.com"
            )
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("builtins.print") as mock_print:
                        with pytest.raises(SystemExit) as exc_info:
                            main(
                                [
                                    "login",
                                    "--school",
                                    "myschool",
                                    "-e",
                                    "test@example.com",
                                    "-p",
                                    "pass",
                                    "-d",
                                    "managebac.cn",
                                    "--format",
                                    "json",
                                ]
                            )
                        assert exc_info.value.code == 0


class TestMainList:
    def test_list_success(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.domain = "managebac.cn"
        mock_client.crawl_all.return_value = {
            "student_name": "John",
            "school": "myschool",
            "base_url": "https://myschool.managebac.cn",
            "crawled_at": "2026-04-29T12:00:00",
            "upcoming": [
                {"id": "1", "title": "HW1", "class_name": "Math", "due_date": "May 1"}
            ],
            "past": [],
            "overdue": [],
            "summary": {"upcoming_count": 1, "past_count": 0, "overdue_count": 0},
        }

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("builtins.print") as mock_print:
                        with pytest.raises(SystemExit) as exc_info:
                            main(["list", "--format", "json"])
                        assert exc_info.value.code == 0
                        printed = mock_print.call_args[0][0]
                        data = json.loads(printed)
                        assert data["ok"] is True

    def test_list_with_tag_filter(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.domain = "managebac.cn"
        mock_client.crawl_all.return_value = {
            "student_name": "John",
            "school": "myschool",
            "base_url": "https://myschool.managebac.cn",
            "crawled_at": "2026-04-29T12:00:00",
            "upcoming": [
                {"id": "1", "title": "HW1", "class_name": "Math", "due_date": "May 1", "labels": ["Summative"]},
                {"id": "2", "title": "HW2", "class_name": "English", "due_date": "May 2", "labels": ["Formative"]}
            ],
            "past": [],
            "overdue": [],
            "summary": {"upcoming_count": 2, "past_count": 0, "overdue_count": 0},
        }

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("builtins.print") as mock_print:
                        with pytest.raises(SystemExit) as exc_info:
                            main(["list", "--tag", "Summative", "--format", "json"])
                        assert exc_info.value.code == 0
                        printed = mock_print.call_args[0][0]
                        data = json.loads(printed)
                        assert data["ok"] is True
                        tasks = data["data"]["tasks"]
                        # HW2 should be filtered out
                        assert len(tasks["upcoming"]) == 1
                        assert tasks["upcoming"][0]["id"] == "1"
                        assert data["data"]["meta"]["tag_filter"] == "Summative"


class TestMainView:
    def test_view_with_url(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_task_detail.return_value = {"description": "Do this"}

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(
                                [
                                    "view",
                                    "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026",
                                    "--format",
                                    "json",
                                ]
                            )
                        assert exc_info.value.code == 0

    def test_view_missing_target(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["view", "--format", "json"])
                        assert exc_info.value.code == 1

    def test_view_task_not_found(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = {
            "upcoming": [],
            "past": [],
            "overdue": [],
            "student_name": "X",
            "school": "s",
            "base_url": "u",
            "crawled_at": "t",
        }

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with pytest.raises(SystemExit) as exc_info:
                        main(["view", "99999", "--format", "json"])
                    assert exc_info.value.code == 1


class TestMainSubmit:
    def test_submit_missing_target(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with pytest.raises(SystemExit) as exc_info:
                        main(["submit", "--format", "json"])
                    assert exc_info.value.code == 1

    def test_submit_missing_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with pytest.raises(SystemExit) as exc_info:
                        main(["submit", "12345", "--format", "json"])
                    assert exc_info.value.code == 1


class TestMainLogout:
    def test_logout(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        with pytest.raises(SystemExit) as exc_info:
            main(["logout", "--format", "json"])
        assert exc_info.value.code == 0


class TestMainCalendar:
    def test_calendar_today(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_calendar_events.return_value = []

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["calendar", "--today", "--format", "json"])
                        assert exc_info.value.code == 0


class TestMainTimetable:
    def test_timetable(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_timetable.return_value = {"days": [], "lessons": []}

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["timetable", "--format", "json"])
                        assert exc_info.value.code == 0





class TestMainNotifications:
    def test_notifications_list(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_notification_token.return_value = ("endpoint", "token")

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("tahuti.__main__.hub_client") as MockHub:
                        mock_hub = MockHub.return_value
                        mock_hub.stats.return_value = {"unread_messages": 2}
                        mock_hub.list.return_value = {
                            "items": [{"id": 1, "title": "Test", "is_read": True}],
                            "meta": {"page": 1},
                        }
                        with patch("builtins.print"):
                            with pytest.raises(SystemExit) as exc_info:
                                main(["notifications", "--format", "json"])
                            assert exc_info.value.code == 0

    def test_notifications_read(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_notification_token.return_value = ("ep", "tok")

        with patch("tahuti.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("tahuti.auth.save_profile"):
                with patch("tahuti.auth.save_session"):
                    with patch("tahuti.__main__.hub_client") as MockHub:
                        mock_hub = MockHub.return_value
                        mock_hub.mark_read.return_value = True
                        with patch("builtins.print"):
                            with pytest.raises(SystemExit) as exc_info:
                                main(
                                    [
                                        "notifications",
                                        "--read",
                                        "12345",
                                        "--format",
                                        "json",
                                    ]
                                )
                            assert exc_info.value.code == 0


class TestMainDaemon:
    def test_daemon_stop_that_stopped_nothing_exits_nonzero(
        self, tmp_path: Path, monkeypatch
    ):
        """A stop request that stopped nothing is a failure, not a success.

        This test previously asserted ``code == 0`` on exactly this payload,
        pinning the defect: a stop-then-start script could not tell that the
        stop never happened and would end up supervising two daemons.
        """
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        nothing_stopped = {"stopped": False, "reason": "pid_file_missing"}
        with (
            patch("tahuti.__main__.ServiceManager") as MockMgr,
            patch("tahuti.__main__.stop_daemon", return_value=nothing_stopped),
        ):
            MockMgr.return_value.stop_background.return_value = {
                "stopped": False,
                "reason": "not_running",
            }
            with patch("builtins.print"):
                with pytest.raises(SystemExit) as exc_info:
                    main(["daemon", "stop", "--format", "json"])
                assert exc_info.value.code == 1

    def test_daemon_stop_that_stopped_a_process_exits_zero(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        with (
            patch("tahuti.__main__.ServiceManager") as MockMgr,
            patch("tahuti.__main__.stop_daemon") as mock_stop,
        ):
            MockMgr.return_value.stop_background.return_value = {
                "stopped": True,
                "pid": 4242,
            }
            with patch("builtins.print"):
                with pytest.raises(SystemExit) as exc_info:
                    main(["daemon", "stop", "--format", "json"])
                assert exc_info.value.code == 0
            mock_stop.assert_not_called()

    def test_daemon_configure_webhook(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

        with patch("tahuti.__main__.configure_webhook") as mock_conf:
            mock_conf.return_value = {"webhook_url": "http://new:8080/hook"}
            with patch("builtins.print"):
                with pytest.raises(SystemExit) as exc_info:
                    main(
                        [
                            "daemon",
                            "configure-webhook",
                            "http://new:8080/hook",
                            "--format",
                            "json",
                        ]
                    )
                assert exc_info.value.code == 0


def test_reclassify_tasks_uses_canonical_classifier(tmp_path: Path):
    from datetime import datetime
    from tahuti.__main__ import _reclassify_tasks

    now = datetime(2026, 9, 10, 12, 0, 0)
    merged_map = {
        "1": {"id": "1", "due_date": "2026-09-20 12:00:00", "has_submit_button": True, "status": "not-submitted"},
        "2": {"id": "2", "due_date": "2026-09-01 12:00:00", "has_submit_button": True, "status": "not-submitted"},
        "3": {"id": "3", "due_date": "2026-09-01 12:00:00", "has_submit_button": True, "status": "submitted"},
    }
    res = _reclassify_tasks(merged_map, now_ref=now)
    assert [t["id"] for t in res["upcoming"]] == ["1"]
    assert [t["id"] for t in res["overdue"]] == ["2"]
    assert [t["id"] for t in res["past"]] == ["3"]

