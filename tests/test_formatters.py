"""Tests for tahuti.formatters."""

from __future__ import annotations

import json
import os
from io import StringIO
from unittest.mock import patch

import pytest

from tahuti.formatters import (
    error,
    ok,
    print_payload,
    render_pretty,
    resolve_format,
)

#: Both spellings of the format override. ``TAHUTI_FORMAT`` is the documented
#: one; ``MB_CLI_FORMAT`` is the pre-rename name, still read so a script that
#: already exported it does not silently change behaviour on upgrade.
FORMAT_ENV_NAMES = ("TAHUTI_FORMAT", "MB_CLI_FORMAT")


class TestResolveFormat:
    def test_explicit_json(self):
        assert resolve_format("json") == "json"

    def test_explicit_pretty(self):
        assert resolve_format("pretty") == "pretty"

    @pytest.mark.parametrize("env_name", FORMAT_ENV_NAMES)
    def test_explicit_format_beats_tty_and_env(self, monkeypatch, env_name):
        monkeypatch.setenv(env_name, "pretty")
        with patch("tahuti.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            assert resolve_format("json") == "json"

    def test_none_defaults_to_pretty_when_tty(self):
        # Unchanged: an interactive terminal still gets the human table.
        with patch("tahuti.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = True
            assert resolve_format(None) == "pretty"

    def test_none_defaults_to_json_when_not_tty(self):
        # REPLACED DELIBERATELY. This test used to assert "pretty" for a
        # non-TTY stdout, pinning the very defect it was named after: the
        # documented non-TTY behaviour (README "Output Formatting",
        # `--format`'s own help text) did not exist, so `mb list | jq .`
        # failed to parse. The documented contract is JSON.
        with patch("tahuti.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            assert resolve_format(None) == "json"

    @pytest.mark.parametrize("forced,is_tty", [("json", True), ("pretty", False)])
    @pytest.mark.parametrize("env_name", FORMAT_ENV_NAMES)
    def test_env_override_beats_tty_probe(self, monkeypatch, env_name, forced, is_tty):
        monkeypatch.setenv(env_name, forced)
        with patch("tahuti.formatters.sys") as mock_sys:
            # The env wins in both directions, so a script gets the same shape
            # whether or not it happens to be attached to a terminal.
            mock_sys.stdout.isatty.return_value = is_tty
            assert resolve_format(None) == forced

    def test_new_env_name_beats_legacy_env_name(self, monkeypatch):
        # The whole point of the fallback: someone who already exported the old
        # name must not override someone who set the new one. Without this
        # precedence a stale MB_CLI_FORMAT in a shell profile would win over an
        # explicit TAHUTI_FORMAT and look like the new name was ignored.
        monkeypatch.setenv("MB_CLI_FORMAT", "json")
        monkeypatch.setenv("TAHUTI_FORMAT", "pretty")
        assert resolve_format(None) == "pretty"

    def test_empty_legacy_env_name_falls_through(self, monkeypatch):
        # An exported-but-empty old name counts as unset, matching how every
        # other legacy env var in tahuti.config is treated.
        monkeypatch.setenv("MB_CLI_FORMAT", "")
        monkeypatch.setenv("TAHUTI_FORMAT", "json")
        assert resolve_format(None) == "json"

    @pytest.mark.parametrize("env_name", FORMAT_ENV_NAMES)
    def test_env_override_is_case_and_space_insensitive(self, monkeypatch, env_name):
        monkeypatch.setenv(env_name, "  JSON ")
        assert resolve_format(None) == "json"

    @pytest.mark.parametrize("env_name", FORMAT_ENV_NAMES)
    def test_unknown_env_value_is_ignored(self, monkeypatch, env_name):
        monkeypatch.setenv(env_name, "yaml")
        with patch("tahuti.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            assert resolve_format(None) == "json"

    def test_isatty_failure_does_not_crash(self):
        # A closed/detached stdout must degrade to JSON, not raise.
        with patch("tahuti.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.side_effect = ValueError("closed file")
            assert resolve_format(None) == "json"

    def test_real_stdout_is_never_a_tty_under_pytest(self):
        # Pytest replaces stdout with a non-TTY capture object; this is the
        # condition that makes `mb list` pipe JSON inside the test suite.
        with patch("tahuti.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            assert resolve_format(None) == "json"


class TestOk:
    def test_structure(self):
        result = ok("list", "default", {"key": "value"})
        assert result["ok"] is True
        assert result["command"] == "list"
        assert result["profile"] == "default"
        assert result["data"]["key"] == "value"


class TestError:
    def test_structure(self):
        result = error("view", "not_found", "Task not found")
        assert result["ok"] is False
        assert result["command"] == "view"
        assert result["error"]["code"] == "not_found"
        assert result["error"]["message"] == "Task not found"


class TestRenderPretty:
    def test_error_payload(self):
        payload = error("test", "err_code", "Something broke")
        output = render_pretty(payload)
        assert "ERROR [err_code]" in output
        assert "Something broke" in output

    def test_login_success(self):
        payload = ok(
            "login",
            "default",
            {
                "school": "myschool",
                "domain": "managebac.cn",
                "email": "a@b.com",
                "base_url": "https://myschool.managebac.cn",
                "auth_method": "cookie",
            },
        )
        output = render_pretty(payload)
        assert "Login successful" in output
        assert "myschool" in output

    def test_logout_success(self):
        payload = ok("logout", "default", {"logged_out": True, "all_profiles": False})
        output = render_pretty(payload)
        assert "Logout complete" in output

    def test_list_with_tasks(self):
        payload = ok(
            "list",
            "default",
            {
                "meta": {
                    "student_name": "John",
                    "school": "myschool",
                    "view": "all",
                    "subject_filter": None,
                    "details": False,
                },
                "summary": {
                    "upcoming_count": 1,
                    "past_count": 0,
                    "overdue_count": 0,
                    "total_count": 1,
                },
                "tasks": {
                    "upcoming": [
                        {
                            "id": "1",
                            "title": "HW1",
                            "class_name": "Math",
                            "due_date": "Apr 1",
                            "grade_score": "A",
                        }
                    ],
                    "past": [],
                    "overdue": [],
                },
            },
        )
        output = render_pretty(payload)
        assert "Task list" in output
        assert "HW1" in output
        assert "Math" in output

    def test_list_empty(self):
        payload = ok(
            "list",
            "default",
            {
                "meta": {
                    "student_name": "John",
                    "school": "s",
                    "view": "all",
                    "subject_filter": None,
                    "details": False,
                },
                "summary": {
                    "upcoming_count": 0,
                    "past_count": 0,
                    "overdue_count": 0,
                    "total_count": 0,
                },
                "tasks": {"upcoming": [], "past": [], "overdue": []},
            },
        )
        output = render_pretty(payload)
        assert "Task list" in output
        assert "total: 0" in output

    def test_view_task(self):
        payload = ok(
            "view",
            "default",
            {
                "task": {
                    "id": "123",
                    "title": "Test Task",
                    "class_name": "Physics",
                    "due_date": "May 1",
                    "grade_score": "B",
                    "link": "http://x",
                },
                "detail": {
                    "description": "Do this",
                    "comments": ["Nice work"],
                    "attachments": [
                        {
                            "name": "f.pdf",
                            "url": "http://x/f.pdf",
                            "source": "description",
                        }
                    ],
                },
            },
        )
        output = render_pretty(payload)
        assert "Task detail" in output
        assert "Do this" in output
        assert "Nice work" in output
        assert "f.pdf" in output

    def test_submit(self):
        payload = ok(
            "submit", "default", {"filename": "hw.pdf", "task_url": "http://x"}
        )
        output = render_pretty(payload)
        assert "File submitted" in output
        assert "hw.pdf" in output

    def test_notifications(self):
        payload = ok(
            "notifications",
            "default",
            {
                "stats": {"unread_messages": 3},
                "items": [
                    {
                        "id": 1,
                        "title": "New grade",
                        "is_read": False,
                        "created_at": "2026-04-29T10:00:00",
                    }
                ],
                "meta": {"page": 1, "total_pages": 2, "total": 15},
            },
        )
        output = render_pretty(payload)
        assert "Notifications" in output
        assert "New grade" in output
        assert "*" in output

    def test_notifications_empty(self):
        payload = ok("notifications", "default", {"stats": {}, "items": [], "meta": {}})
        output = render_pretty(payload)
        assert "(none)" in output

    def test_notifications_mutate(self):
        payload = ok(
            "notifications.mutate",
            "default",
            {"action": "read", "notification_id": 123, "ok": True},
        )
        output = render_pretty(payload)
        assert "read" in output

    def test_calendar(self):
        payload = ok(
            "calendar",
            "default",
            {
                "start": "2026-04-29",
                "end": "2026-05-05",
                "events": [
                    {
                        "id": 1,
                        "title": "Exam",
                        "start": "2026-04-30T09:00:00",
                        "type": "exam",
                    }
                ],
            },
        )
        output = render_pretty(payload)
        assert "Calendar events" in output
        assert "Exam" in output

    def test_calendar_empty(self):
        payload = ok("calendar", "default", {"start": "x", "end": "y", "events": []})
        output = render_pretty(payload)
        assert "(no events)" in output

    def test_timetable(self):
        payload = ok(
            "timetable",
            "default",
            {
                "start_date": "2026-04-28",
                "days": [{"header": "Monday", "is_today": True}],
                "lessons": [
                    {
                        "period": "P1",
                        "day": "Monday",
                        "is_today": True,
                        "time": "08:00",
                        "subject": "Math",
                        "teacher": "Mr. S",
                        "room": "R1",
                        "year": "Y11",
                    }
                ],
            },
        )
        output = render_pretty(payload)
        assert "Timetable" in output
        assert "Math" in output

    def test_timetable_empty(self):
        payload = ok(
            "timetable", "default", {"start_date": "x", "days": [], "lessons": []}
        )
        output = render_pretty(payload)
        assert "(no lessons)" in output

    def test_view_with_feedback(self):
        payload = ok(
            "view",
            "default",
            {
                "task": {"id": "123", "title": "HW1", "class_name": "Math"},
                "detail": {},
                "feedback": {
                    "general_comments": ["Great work!"],
                    "feedback_items": [
                        {"submission_name": "hw1.pdf", "comment": "Good job"}
                    ],
                },
            },
        )
        output = render_pretty(payload)
        assert "[teacher feedback]" in output
        assert "Great work!" in output
        assert "hw1.pdf" in output
        assert "Good job" in output

    def test_unknown_command_falls_through_to_json(self):
        payload = {"ok": True, "command": "unknown_cmd", "data": {"x": 1}}
        output = render_pretty(payload)
        parsed = json.loads(output)
        assert parsed["data"]["x"] == 1


class TestRenderPrettyCountGradeFreq:
    """The `count-grade-freq` branch had no coverage."""

    def test_summary_is_sorted_by_count_then_name(self):
        payload = ok(
            "count-grade-freq",
            "default",
            {
                "grades": {"A": 5, "B": 5, "C": 2, "F": 9},
                "total": 21,
                "classes": [{"id": "1", "name": "Math"}, {"id": "2", "name": "Physics"}],
            },
        )
        output = render_pretty(payload)
        assert "Grade Frequency Summary" in output
        assert "classes counted: 2" in output
        assert "total tasks: 21" in output
        # Highest count first; ties broken alphabetically (A before B).
        rows = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit() and parts[0] in {"A", "B", "C", "F"}:
                rows.append((parts[0], int(parts[1])))
        assert rows == [("F", 9), ("A", 5), ("B", 5), ("C", 2)]

    def test_summary_empty(self):
        payload = ok(
            "count-grade-freq", "default", {"grades": {}, "total": 0, "classes": []}
        )
        output = render_pretty(payload)
        assert "classes counted: 0" in output
        assert "total tasks: 0" in output


class TestRenderPrettyMixedTimezoneSort:
    """A section mixing ISO-with-offset and textual due dates must not crash.

    parse_due_date returns an aware datetime for ISO input with an offset and a
    naive one for every ManageBac HTML format. Sorting the raw values raised
    `TypeError: can't compare offset-naive and offset-aware datetimes` out of
    render_pretty, which main() does not catch — the user got a traceback and
    no payload at all.
    """

    @staticmethod
    def _list_payload(tasks):
        return ok(
            "list",
            "default",
            {
                "meta": {"student_name": "John", "school": "s", "view": "all"},
                "summary": {
                    "upcoming_count": len(tasks),
                    "past_count": 0,
                    "overdue_count": 0,
                    "total_count": len(tasks),
                },
                "tasks": {"upcoming": tasks, "past": [], "overdue": []},
            },
        )

    def test_aware_and_naive_in_the_same_section(self):
        tasks = [
            {"id": "1", "title": "ISO aware", "class_name": "Physics",
             "due_date": "2026-09-20T23:59:00+08:00"},
            {"id": "2", "title": "HTML text", "class_name": "Physics",
             "due_date": "Sep 19, 11:59 PM"},
            {"id": "3", "title": "Z suffix", "class_name": "Physics",
             "due_date": "2026-09-21T00:00:00Z"},
            {"id": "4", "title": "undated", "class_name": "Physics"},
        ]
        output = render_pretty(self._list_payload(tasks))
        for title in ("ISO aware", "HTML text", "Z suffix", "undated"):
            assert title in output

    def test_aware_and_naive_across_classes(self):
        tasks = [
            {"id": "1", "title": "Physics aware", "class_name": "Physics",
             "due_date": "2026-09-20T23:59:00+08:00"},
            {"id": "2", "title": "Math naive", "class_name": "Math",
             "due_date": "Sep 19, 11:59 PM"},
        ]
        output = render_pretty(self._list_payload(tasks))
        assert "Physics aware" in output
        assert "Math naive" in output

    def test_multi_class_sections_render_separators(self):
        tasks = [
            {"id": "1", "title": "P1", "class_name": "Physics", "due_date": "Sep 19"},
            {"id": "2", "title": "M1", "class_name": "Math", "due_date": "Sep 20"},
        ]
        output = render_pretty(self._list_payload(tasks))
        assert "=== Math ===" in output
        assert "=== Physics ===" in output

    def test_single_class_omits_separator(self):
        tasks = [{"id": "1", "title": "P1", "class_name": "Physics", "due_date": "Sep 19"}]
        output = render_pretty(self._list_payload(tasks))
        assert "Physics" in output
        assert "===" not in output


class TestPrintPayload:
    def test_json_format(self):
        payload = ok("login", "default", {"school": "myschool"})
        output = (
            print_payload.__wrapped__ if hasattr(print_payload, "__wrapped__") else None
        )
        # Test via StringIO capture
        captured = StringIO()
        with patch("builtins.print") as mock_print:
            print_payload(payload, None, "json")
            printed = mock_print.call_args[0][0]
            parsed = json.loads(printed)
            assert parsed["ok"] is True

    def test_pretty_format(self):
        payload = ok("login", "default", {"school": "myschool"})
        with patch("builtins.print") as mock_print:
            print_payload(payload, None, "pretty")
            printed = mock_print.call_args[0][0]
            assert "Login successful" in printed

    def test_write_to_file(self, tmp_path):
        payload = ok("login", "default", {"school": "myschool"})
        output_file = str(tmp_path / "output.json")
        print_payload(payload, output_file, "json")
        content = (tmp_path / "output.json").read_text()
        parsed = json.loads(content)
        assert parsed["ok"] is True

    def test_written_file_is_0600(self, tmp_path):
        # The cache and config writers both assert this; the output writer
        # emits the same grades/session data, so it must not be looser.
        payload = ok("login", "default", {"school": "myschool"})
        dest = tmp_path / "nested" / "output.json"
        print_payload(payload, str(dest), "json")
        mode = dest.stat().st_mode & 0o777
        assert mode == 0o600, oct(dest.stat().st_mode)

    def test_written_file_is_0600_even_under_a_loose_umask(self, tmp_path):
        # mkstemp already opens 0600; pin it anyway so a umask change cannot
        # widen the file.
        payload = ok("list", "default", {"tasks": {}})
        dest = tmp_path / "output.json"
        old_umask = os.umask(0o000)
        try:
            print_payload(payload, str(dest), "json")
        finally:
            os.umask(old_umask)
        assert dest.stat().st_mode & 0o777 == 0o600

    def test_no_temp_file_left_behind(self, tmp_path):
        payload = ok("login", "default", {"school": "myschool"})
        # Write into its own directory: the autouse isolation fixture creates
        # `.config/` in tmp_path, which is not a leftover temp file.
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        dest = out_dir / "output.json"
        print_payload(payload, str(dest), "json")
        assert dest.exists()
        # mkstemp + chmod + os.replace: nothing partial may survive.
        leftovers = [p.name for p in out_dir.iterdir() if p.name != "output.json"]
        assert leftovers == []

    def test_write_is_atomic_replace_of_existing_file(self, tmp_path):
        payload = ok("login", "default", {"school": "myschool"})
        dest = tmp_path / "output.json"
        dest.write_text("stale content that must be replaced\n")
        os.chmod(dest, 0o644)
        print_payload(payload, str(dest), "json")
        assert json.loads(dest.read_text())["ok"] is True
        # Replacing an existing looser file must not inherit its mode.
        assert dest.stat().st_mode & 0o777 == 0o600

    def test_write_failure_leaves_no_partial_file(self, tmp_path):
        payload = ok("login", "default", {"school": "myschool"})
        # Its own directory, so the isolation fixture's `.config/` does not
        # count as a leftover.
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        dest = out_dir / "output.json"
        with patch("tahuti.formatters.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                print_payload(payload, str(dest), "json")
        assert not dest.exists()
        assert [p.name for p in out_dir.iterdir()] == []

    def test_output_path_creates_missing_parents(self, tmp_path):
        payload = ok("login", "default", {"school": "myschool"})
        dest = tmp_path / "a" / "b" / "output.json"
        print_payload(payload, str(dest), "json")
        assert dest.is_file()
        assert dest.stat().st_mode & 0o777 == 0o600

    def test_pretty_payload_to_stdout_when_not_a_tty_and_no_explicit_format(self):
        # The documented non-TTY default is JSON, so a bare `mb list` piped
        # into jq parses without `--format json`.
        payload = ok("login", "default", {"school": "myschool"})
        with patch("tahuti.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            with patch("builtins.print") as mock_print:
                print_payload(payload, None, None)
        printed = mock_print.call_args[0][0]
        assert json.loads(printed)["ok"] is True


class TestDisplayWidthAndPadding:
    def test_get_display_width_ascii(self):
        from tahuti.formatters import get_display_width
        assert get_display_width("hello") == 5

    def test_get_display_width_cjk(self):
        from tahuti.formatters import get_display_width
        # Chinese characters take 2 columns each
        assert get_display_width("期末考试") == 8
        assert get_display_width("Math期末考试") == 12

    def test_pad_string_left(self):
        from tahuti.formatters import pad_string
        padded = pad_string("期末考试", 12, "left")
        # 8 columns of CJK + 4 spaces = 12 columns
        assert padded == "期末考试    "

    def test_pad_string_right(self):
        from tahuti.formatters import pad_string
        padded = pad_string("期末考试", 10, "right")
        # 2 spaces + 8 columns of CJK = 10 columns
        assert padded == "  期末考试"

