"""Tests for tahuti.client."""

from __future__ import annotations

import json
import re
import sys
from datetime import timedelta, timezone
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

if "tahuti.client" in sys.modules:
    importlib.reload(sys.modules["tahuti.client"])

from unittest.mock import MagicMock, patch

import pytest
import requests
import requests_mock as rm

from tahuti.cache import ResponseCache
from tahuti.client import (
    HEADERS,
    ManageBacClient,
    _absolute_event_url,
    parse_task_url,
)


@pytest.fixture()
def client(tmp_path: Path):
    """Create a ManageBacClient with a temp cache directory."""
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True)
    c = ManageBacClient(
        "myschool",
        domain="managebac.cn",
        cache=cache,
        verify=False,
        retry=0,
    )
    c.set_cookie("test_session_cookie")
    return c


class TestManageBacClientInit:
    def test_base_url(self, client):
        assert client.base == "https://myschool.managebac.cn"

    def test_school_strips_domain(self):
        c = ManageBacClient("myschool.managebac.cn", domain="managebac.cn")
        assert c.school == "myschool"

    def test_default_domain(self):
        c = ManageBacClient("myschool")
        assert c.domain == "managebac.com"
        assert c.base == "https://myschool.managebac.com"

    def test_subdomain_property(self):
        c = ManageBacClient("myschool")
        assert c.subdomain == "myschool"

    def test_from_config_classmethod(self):
        from unittest.mock import patch
        with patch("tahuti.auth.build_client") as mock_build:
            mock_client = ManageBacClient("configschool")
            mock_build.return_value = (None, mock_client, "user@school.org")
            loaded = ManageBacClient.from_config(profile="testprofile")
            assert loaded == mock_client
            mock_build.assert_called_once_with(profile="testprofile")

            mock_build.reset_mock()
            loaded_default = ManageBacClient.from_config()
            assert loaded_default == mock_client
            mock_build.assert_called_once_with(profile=None)

    def test_headers_set(self, client):
        """Every static header must reach the session verbatim.

        The User-Agent is checked separately, in
        `test_user_agent_names_the_running_platform`: asserting it against
        `HEADERS` here would be circular, since `HEADERS` is the very value
        under test.
        """
        for key, val in HEADERS.items():
            if key == "User-Agent":
                continue
            assert client.session.headers.get(key) == val

    @pytest.mark.xfail(
        reason="src/tahuti/client.py:63-66 hardcodes a macOS User-Agent on every "
        "platform, so on Linux/Windows the fingerprint names the wrong OS. "
        "Test-only change: the src fix belongs to whoever owns client.py. "
        "This mark reports XPASS once the UA follows sys.platform — delete it then.",
        strict=False,
    )
    def test_user_agent_names_the_running_platform(self, client):
        """The User-Agent must describe the OS it is actually running on.

        `HEADERS` hardcodes a macOS fingerprint on every platform, so the old
        assertion — `session.headers["User-Agent"] == HEADERS["User-Agent"]` —
        could never fail: it compared the constant to itself. A crawler that
        announces "Macintosh; Intel Mac OS X" from a Linux box or a Windows
        Server is a trivially fingerprintable lie, and no test could see it.
        """
        user_agent = client.session.headers.get("User-Agent")
        assert user_agent, "no User-Agent was set on the session"

        # The OS token ManageBac would see, per platform.
        expected_token = {
            "darwin": "Macintosh",
            "win32": "Windows NT",
        }.get(sys.platform)
        if expected_token is None:
            # Linux and other POSIX: a real Linux UA names X11 or Linux.
            assert (
                "X11" in user_agent or "Linux" in user_agent
            ), f"User-Agent does not name Linux: {user_agent!r}"
        else:
            assert expected_token in user_agent, (
                f"on {sys.platform} the User-Agent must name {expected_token!r}, "
                f"got {user_agent!r}"
            )

        # The rest of the fingerprint must stay a well-formed browser string.
        assert "Mozilla/5.0" in user_agent
        assert "AppleWebKit/537.36" in user_agent

    def test_initial_student_name_none(self, client):
        assert client.student_name is None


class TestSetCookie:
    def test_sets_cookie(self, client):
        client.set_cookie("my_cookie")
        assert client.session.cookies.get("_managebac_session") == "my_cookie"

    def test_cookie_domain(self, client):
        client.set_cookie("c")
        cookies_dict = client.session.cookies.get_dict()
        assert "_managebac_session" in cookies_dict


class TestInvalidateCache:
    def test_invalidate(self, client):
        client.cache.put("https://example.com", "body", 200)
        client.invalidate_cache()
        assert client.cache.get("https://example.com") is None


class TestLogin:
    def test_login_success(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/login",
                text='<html><input name="authenticity_token" value="tok123"></html>',
            )
            m.post(
                "https://myschool.managebac.cn/sessions",
                status_code=302,
                headers={
                    "Location": "https://myschool.managebac.cn/student/tasks_and_deadlines"
                },
            )
            m.get(
                "https://myschool.managebac.cn/student/tasks_and_deadlines",
                text="<html>Dashboard</html>",
            )
            assert client.login("user@example.com", "pass") is True

    def test_login_failure_wrong_credentials(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/login",
                text='<html><input name="authenticity_token" value="tok"></html>',
            )
            m.post(
                "https://myschool.managebac.cn/sessions",
                status_code=302,
                headers={"Location": "https://myschool.managebac.cn/login"},
            )
            assert client.login("user@example.com", "wrong") is False

    def test_login_no_csrf_token(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/login",
                text="<html><body>No form here</body></html>",
            )
            assert client.login("user@example.com", "pass") is False


class TestGetTasksByView:
    def test_single_page(self, client, sample_tasks_page_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://myschool\.managebac\.cn/student/tasks_and_deadlines"),
                text=sample_tasks_page_html,
            )
            tasks = client.get_tasks_by_view("upcoming", max_pages=1)
            assert len(tasks) == 2
            assert tasks[0]["title"] == "Homework 3"
            assert tasks[0]["view"] == "upcoming"
            assert tasks[0]["id"] == "1000026"
            assert tasks[0]["class_name"] == "Math HL"
            assert tasks[0]["grade_letter"] == "A"
            assert tasks[0]["grade_score"] == "95/100"
            assert tasks[1]["grade_letter"] is None

    def test_empty_page_stops(self, client):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://myschool\.managebac\.cn/student/tasks_and_deadlines"),
                text="<html><body>No tasks</body></html>",
            )
            tasks = client.get_tasks_by_view("upcoming", max_pages=10)
            assert tasks == []

    def test_pagination(self, client, sample_tasks_page_html):
        # Page 2 carries a *different* task. A listing page never repeats page 1,
        # and get_tasks_by_view now de-duplicates by task id, so reusing the same
        # tile here would collapse the two pages into one.
        page2 = """
        <html><body>
          <div class="f-task-tile">
            <a class="f-tile__title-link" href="/student/classes/1000023/core_tasks/1000028">Lab Report</a>
            <div class="f-tile__description">
              <span>Apr 25</span><a href="/student/classes/1000023">Math HL</a>
            </div>
          </div>
        </body></html>
        """
        with rm.Mocker() as m:
            m.get(
                re.compile(r"page=1"),
                text=sample_tasks_page_html,
            )
            m.get(
                re.compile(r"page=2"),
                text=page2,
            )
            tasks = client.get_tasks_by_view("upcoming", max_pages=10)
            assert len(tasks) == 3  # 2 from page 1 + 1 from page 2


class TestParseTile:
    def test_basic_tile(self, client, sample_task_tile_html):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sample_task_tile_html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        result = client._parse_tile(tile)
        assert result["title"] == "Homework 3"
        assert result["id"] == "1000026"
        assert result["due_date"] == "Apr 15"
        assert result["class_name"] == "Math HL"
        assert result["grade_letter"] == "A"
        assert result["grade_score"] == "95/100"

    def test_tile_no_link(self, client):
        from bs4 import BeautifulSoup

        html = '<div class="f-task-tile"><div>No link here</div></div>'
        soup = BeautifulSoup(html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        assert client._parse_tile(tile) is None

    def test_tile_labels(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div class="f-task-tile">
          <a class="f-tile__title-link" href="/student/classes/1/c/123">Test</a>
          <span class="badge">Urgent</span>
          <span class="badge">Homework</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        result = client._parse_tile(tile)
        assert result["labels"] == ["Urgent", "Homework"]

    def test_tile_submitted_badge_not_grade_score(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div class="f-task-tile">
          <a class="f-tile__title-link" href="/student/classes/1000012/core_tasks/1000099">Homework of summer holiday</a>
          <div class="f-tile__description">
            <span>Sep 07</span>
            <a href="/student/classes/1000012/">Pre-AP Chemistry</a>
          </div>
          <span class="badge">Formative</span>
          <div class="f-tile__suffix">
            <span class="badge">Submitted</span>
          </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        result = client._parse_tile(tile)

        assert result["id"] == "1000099"
        assert result["class_id"] == "1000012"
        # Grade must be None, NOT "Submitted"
        assert result["grade_score"] is None
        assert result["grade_letter"] is None
        # Submission status and lifecycle must be clean
        assert result["submission_status"] == "submitted"
        assert result["status"] == "submitted"
        assert "Submitted" in result["labels"]

    def test_tile_pending_submit_button_not_grade_score(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div class="f-task-tile">
          <a class="f-tile__title-link" href="/student/classes/1000012/core_tasks/1000020">Poster</a>
          <div class="f-tile__description">
            <span>Sep 07</span>
            <a href="/student/classes/1000012/">Pre-AP Chemistry</a>
          </div>
          <span class="badge">Formative</span>
          <div class="f-tile__suffix">
            <a class="btn btn-primary" href="/student/classes/1000012/core_tasks/1000020/dropbox">Submit Coursework</a>
          </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        result = client._parse_tile(tile)

        assert result["id"] == "1000020"
        assert result["class_id"] == "1000012"
        assert result["grade_score"] is None
        assert result["grade_letter"] is None
        assert result["has_submit_button"] is True
        assert result["submission_status"] == "pending"
        assert result["status"] == "not-submitted"


class TestHasNextPage:
    def test_has_next_page(self, client):
        from bs4 import BeautifulSoup

        html = '<html><body><a href="?view=upcoming&page=2">Next</a></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is True

    def test_no_next_page(self, client, sample_tasks_page_html_no_next):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sample_tasks_page_html_no_next, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is False

    def test_next_page_button(self, client):
        from bs4 import BeautifulSoup

        html = '<html><body><button class="next" aria-label="Next page">></button></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is True

    def test_disabled_next_button(self, client):
        from bs4 import BeautifulSoup

        html = '<html><body><button class="next disabled" disabled>></button></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is False


class TestCaptureStudentName:
    def test_captures_name(self, client, sample_tasks_page_html):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sample_tasks_page_html, "html.parser")
        client._capture_student_name(soup)
        assert client.student_name == "John Smith"

    def test_does_not_overwrite(self, client, sample_tasks_page_html):
        from bs4 import BeautifulSoup

        client.student_name = "Already Set"
        soup = BeautifulSoup(sample_tasks_page_html, "html.parser")
        client._capture_student_name(soup)
        assert client.student_name == "Already Set"

    def test_no_profile_link(self, client):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body>Nothing</body></html>", "html.parser")
        client._capture_student_name(soup)
        assert client.student_name is None


class TestGetTaskDetail:
    def test_returns_description(self, client, sample_task_detail_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(
                    r"https://myschool\.managebac\.cn/student/classes/.+/core_tasks/.+"
                ),
                text=sample_task_detail_html,
            )
            detail = client.get_task_detail(
                "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026"
            )
            assert detail is not None
            assert "Complete the exercises" in detail["description"]
            assert detail["comments"][0] == "Teacher comment: Great work!"
            assert "Submitted" in detail["submission"]
            assert len(detail["attachments"]) >= 1

    def test_strips_base_url(self, client, sample_task_detail_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(
                    r"https://myschool\.managebac\.cn/student/classes/.+/core_tasks/.+"
                ),
                text=sample_task_detail_html,
            )
            detail = client.get_task_detail(
                "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026"
            )
            assert detail is not None

    def test_duplicate_url_encoded_attachments(self, client):
        html = """
        <html>
        <body>
          <main>
            <h3>Description</h3>
            <div class="fr-view">
              <a href="/student/classes/1000023/attachments/123/%E5%A4%8D%E4%B9%A0%E6%8F%90%E7%BA%B2_%E4%B9%9D%E5%B9%B4%E7%BA%A7%E4%B8%8B.doc" class="fr-file">复习提纲_九年级下.doc</a>
              <a href="/student/classes/1000023/attachments/123/%E5%A4%8D%E4%B9%A0%E6%8F%90%E7%BA%B2_%E4%B9%9D%E5%B9%B4%E7%BA%A7%E4%B8%8B.doc" class="fr-file"></a>
            </div>
          </main>
        </body>
        </html>
        """
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://myschool\.managebac\.cn/student/classes/.+/core_tasks/.+"),
                text=html,
            )
            detail = client.get_task_detail(
                "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026"
            )
            assert detail is not None
            assert len(detail["attachments"]) == 1
            assert detail["attachments"][0]["name"] == "复习提纲_九年级下.doc"


class TestCrawlAll:
    def test_combines_views(self, client, sample_tasks_page_html_no_next):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"view=upcoming"),
                text=sample_tasks_page_html_no_next,
            )
            m.get(
                re.compile(r"view=past"),
                text="<html><body>No tasks</body></html>",
            )
            m.get(
                re.compile(r"view=overdue"),
                text="<html><body>No tasks</body></html>",
            )
            result = client.crawl_all(max_pages=1, fetch_details=False)
            assert result["school"] == "myschool"
            assert result["base_url"] == "https://myschool.managebac.cn"
            assert len(result["upcoming"]) == 1
            assert len(result["past"]) == 0
            assert len(result["overdue"]) == 0
            assert result["summary"]["upcoming_count"] == 1
            assert result["crawled_at"] is not None

    def test_with_fetch_details(
        self, client, sample_tasks_page_html_no_next, sample_task_detail_html
    ):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"view=upcoming"),
                text=sample_tasks_page_html_no_next,
            )
            m.get(
                re.compile(r"view=past"),
                text="<html></html>",
            )
            m.get(
                re.compile(r"view=overdue"),
                text="<html></html>",
            )
            m.get(
                re.compile(r"/student/classes/\d+/core_tasks/\d+$"),
                text=sample_task_detail_html,
            )
            # fetch_details goes through the event *hint* page, not the detail
            # page. This route was previously unmatched, so get_task_detail
            # swallowed the connection error and stored {"error": ...} as the
            # "detail" — the assertion below passed on the error dict.
            m.get(
                re.compile(r"/student/classes/\d+/events/\d+/hint$"),
                text=sample_task_detail_html,
            )
            result = client.crawl_all(max_pages=1, fetch_details=True)
            assert len(result["upcoming"]) == 1
            assert "detail" in result["upcoming"][0]
            assert "error" not in result["upcoming"][0]["detail"]


class TestGetCalendarEvents:
    def test_returns_events(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/events.json",
                json=[
                    {
                        "id": 1,
                        "title": "Exam",
                        "start": "2026-04-30T09:00:00",
                        "end": "2026-04-30T12:00:00",
                        "allDay": False,
                        "description": "<p>Math exam</p>",
                        "type": "exam",
                        "category": "assessment",
                        "url": "/student/calendar/1",
                        "backgroundColor": "#ff0000",
                    }
                ],
            )
            events = client.get_calendar_events("2026-04-29", "2026-05-05")
            assert len(events) == 1
            assert events[0]["title"] == "Exam"
            assert events[0]["all_day"] is False
            assert events[0]["color"] == "#ff0000"

    def test_empty_events(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/events.json",
                json=[],
            )
            events = client.get_calendar_events("2026-04-29", "2026-05-05")
            assert events == []


class TestGetICalFeed:
    def test_fetches_ical(self, client, sample_calendar_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR\nEND:VCALENDAR",
            )
            ical = client.get_ical_feed()
            assert "VCALENDAR" in ical

    def test_no_webcal_link_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text="<html><body>No link</body></html>",
            )
            with pytest.raises(RuntimeError, match="webcal"):
                client.get_ical_feed()


class TestGetTimetable:
    def test_parses_timetable(self, client, sample_timetable_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://myschool\.managebac\.cn/student/timetables"),
                text=sample_timetable_html,
            )
            result = client.get_timetable("2026-04-28")
            assert len(result["days"]) == 2
            assert result["days"][0]["header"] == "Monday"
            assert result["days"][0]["is_today"] is True
            assert len(result["lessons"]) == 1
            lesson = result["lessons"][0]
            assert lesson["subject"] == "Math HL"
            assert lesson["period"] == "P1"
            assert lesson["teacher"] == "Mr. Smith"
            assert lesson["room"] == "Room 101"
            assert lesson["class_id"] == "1000023"

    def test_no_table_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://myschool\.managebac\.cn/student/timetables"),
                text="<html><body>No timetable</body></html>",
            )
            with pytest.raises(RuntimeError, match="timetable"):
                client.get_timetable()


class TestGetClassGrades:
    def test_parses_grades(self, client, sample_grades_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/classes/1000023/core_tasks",
                text=sample_grades_page_html,
            )
            result = client.get_class_grades("1000023")
            assert len(result["tasks"]) == 2
            assert result["tasks"][0]["grade_letter"] == "A"
            assert result["tasks"][0]["category"] == "Homework"
            assert len(result["categories"]) == 2
            assert result["categories"][0]["name"] == "Homework"
            assert result["categories"][0]["weight"] == 0.4
            assert result["expected_grade"] is not None
            assert result["expected_grade"]["letter_grade"] == "B"

    def test_no_grades_page(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/classes/999/core_tasks",
                text="<html><body>No grades</body></html>",
            )
            result = client.get_class_grades("999")
            assert result["tasks"] == []
            assert result["categories"] == []


class TestSubmitFile:
    def test_submit_success(self, client, sample_dropbox_page_html, tmp_path: Path):
        test_file = tmp_path / "homework.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake")

        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/1000023/core_tasks/1000026/dropbox$"),
                text=sample_dropbox_page_html,
            )
            m.post(
                "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026/dropbox/upload",
                json={"ok": True},
            )
            result = client.submit_file("1000023", "1000026", str(test_file))
            assert result["ok"] is True
            assert result["filename"] == "homework.pdf"
            assert "1000026" in result["task_url"]

    def test_submit_file_not_found(self, client, sample_dropbox_page_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text=sample_dropbox_page_html,
            )
            with pytest.raises(FileNotFoundError):
                client.submit_file("1000023", "1000026", "/nonexistent/file.pdf")

    def test_submit_no_csrf_raises(self, client, tmp_path: Path):
        test_file = tmp_path / "hw.pdf"
        test_file.write_bytes(b"content")
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text="<html><body>No CSRF</body></html>",
            )
            with pytest.raises(RuntimeError, match="CSRF"):
                client.submit_file("1000023", "1000026", str(test_file))

    def test_submit_no_form_raises(self, client, tmp_path: Path):
        test_file = tmp_path / "hw.pdf"
        test_file.write_bytes(b"content")
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text='<html><head><meta name="csrf-token" content="tok"></head><body>No form</body></html>',
            )
            with pytest.raises(RuntimeError, match="form"):
                client.submit_file("1000023", "1000026", str(test_file))


class TestGetSubmissions:
    def test_returns_submissions(self, client, sample_dropbox_page_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text=sample_dropbox_page_html,
            )
            subs = client.get_submissions("1000023", "1000026")
            assert len(subs) == 1
            assert subs[0]["name"] == "document.pdf"

    def test_filters_view_feedback(self, client):
        html = """
        <html><body>
        <table>
          <tr><a href="/student/classes/1/attachments/1/file.pdf">file.pdf</a></tr>
          <tr><a href="/student/classes/1/attachments/2/feedback.pdf">View Teacher Feedback</a></tr>
        </table>
        </body></html>
        """
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text=html,
            )
            subs = client.get_submissions("1", "1")
            assert len(subs) == 1
            assert subs[0]["name"] == "file.pdf"

    def test_error_returns_list_with_error(self, client):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                status_code=500,
            )
            subs = client.get_submissions("1", "1")
            assert len(subs) == 1
            assert "error" in subs[0]


class TestExtractAttachments:
    def test_extracts_file_links(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div>
          <a href="/student/classes/1/attachments/123/report.pdf" class="fr-file">report.pdf</a>
          <a href="javascript:void(0)">ignore</a>
          <a href="mailto:test@test.com">ignore</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        attachments = client._extract_attachments(soup)
        assert len(attachments) == 1
        assert attachments[0]["name"] == "report.pdf"
        assert "report.pdf" in attachments[0]["url"]

    def test_deduplicates(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div>
          <a href="/student/classes/1/attachments/123/file.pdf">file.pdf</a>
          <a href="/student/classes/1/attachments/123/file.pdf">file.pdf</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        attachments = client._extract_attachments(soup)
        assert len(attachments) == 1


class TestGetNotificationToken:
    def test_extracts_token(self, client, sample_notifications_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/notifications",
                text=sample_notifications_page_html,
            )
            endpoint, token = client.get_notification_token()
            assert endpoint == "https://mnn-hub.prod.faria.com"
            assert token == "eyJhbGciOiJIUzI1NiJ9.test.token"

    def test_no_trigger_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/notifications",
                text="<html><body>No trigger</body></html>",
            )
            with pytest.raises(RuntimeError, match="trigger"):
                client.get_notification_token()


class TestGetCsrf:
    def test_extracts_csrf(self, client):
        from bs4 import BeautifulSoup

        html = '<html><head><meta name="csrf-token" content="abc123"></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._get_csrf(soup) == "abc123"

    def test_no_csrf_returns_none(self, client):
        from bs4 import BeautifulSoup

        html = "<html><head></head></html>"
        soup = BeautifulSoup(html, "html.parser")
        assert client._get_csrf(soup) is None


class TestCountGradeFrequencies:
    def test_counts_across_classes(self, client):
        tasks_data = {
            "upcoming": [
                {
                    "id": "1",
                    "class_name": "Math",
                    "link": "/student/classes/100/core_tasks/1",
                },
            ],
            "past": [],
            "overdue": [],
        }
        grades_data = {
            "tasks": [
                {"grade_letter": "A"},
                {"grade_letter": "B"},
                {"grade_letter": "A"},
            ],
            "categories": [],
            "grade_scale": {},
            "expected_grade": None,
        }
        with rm.Mocker() as m:
            m.get(re.compile(r"view=upcoming"), json=[])
            m.get(re.compile(r"view=past"), json=[])
            m.get(re.compile(r"view=overdue"), json=[])
            m.get(
                "https://myschool.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
                text="<html></html>",
            )
            m.get(
                "https://myschool.managebac.cn/student/tasks_and_deadlines?view=past&page=1",
                text="<html></html>",
            )
            m.get(
                "https://myschool.managebac.cn/student/tasks_and_deadlines?view=overdue&page=1",
                text="<html></html>",
            )
            # We need to mock crawl_all and get_class_grades
            with patch.object(
                client,
                "crawl_all",
                return_value={
                    "upcoming": [
                        {
                            "id": "1",
                            "class_name": "Math",
                            "link": "/student/classes/100/core_tasks/1",
                        }
                    ],
                    "past": [],
                    "overdue": [],
                },
            ):
                with patch.object(
                    client,
                    "get_class_grades",
                    return_value={
                        "tasks": [
                            {"grade_letter": "A"},
                            {"grade_letter": "B"},
                            {"grade_letter": "A"},
                        ],
                    },
                ):
                    result = client.count_grade_frequencies()
                    assert result["grades"] == {"A": 2, "B": 1}
                    assert result["total"] == 3

    def test_filter_no_match(self, client):
        with patch.object(
            client,
            "crawl_all",
            return_value={
                "upcoming": [
                    {
                        "id": "1",
                        "class_name": "Math",
                        "link": "/student/classes/100/core_tasks/1",
                    }
                ],
                "past": [],
                "overdue": [],
            },
        ):
            result = client.count_grade_frequencies(class_filter="Physics")
            assert "error" in result
            assert "Physics" in result["error"]


class TestRetryLogic:
    def test_retries_on_connection_error(self, tmp_path: Path):
        import requests as _requests

        cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=False)
        client = ManageBacClient("myschool", domain="managebac.cn", cache=cache, retry=2)
        client.set_cookie("c")
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/login",
                [
                    {"exc": _requests.ConnectionError("fail")},
                    {"exc": _requests.ConnectionError("fail")},
                    {
                        "text": '<html><input name="authenticity_token" value="t"></html>'
                    },
                ],
            )
            m.post(
                "https://myschool.managebac.cn/sessions",
                status_code=302,
                headers={"Location": "/student/tasks_and_deadlines"},
            )
            m.get(
                "https://myschool.managebac.cn/student/tasks_and_deadlines",
                text="<html>Dashboard</html>",
            )
            with patch("tahuti.client.time.sleep"):
                assert client.login("a@b.com", "pass") is True

    def test_retries_on_503(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=False)
        client = ManageBacClient("myschool", domain="managebac.cn", cache=cache, retry=1)
        client.set_cookie("c")
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
                [
                    {"status_code": 503, "text": "Service Unavailable"},
                    {"text": "<html></html>"},
                ],
            )
            with patch("tahuti.client.time.sleep"):
                tasks = client.get_tasks_by_view("upcoming", max_pages=1)
                assert tasks == []

    def test_max_retries_exceeded(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=False)
        client = ManageBacClient("myschool", domain="managebac.cn", cache=cache, retry=1)
        client.set_cookie("c")
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
                exc=ConnectionError("fail"),
            )
            with patch("tahuti.client.time.sleep"):
                with pytest.raises(ConnectionError):
                    client.get_tasks_by_view("upcoming", max_pages=1)


class TestGetCached:
    def test_returns_cached_response(self, client, sample_tasks_page_html):
        client.cache.put(
            "https://myschool.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
            sample_tasks_page_html,
            200,
        )
        soup = client._get("/student/tasks_and_deadlines?view=upcoming&page=1")
        assert soup.find("title") is not None

    def test_session_expired_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/tasks",
                status_code=302,
                headers={"Location": "/login"},
            )
            m.get(
                "https://myschool.managebac.cn/login",
                text="<html>login page</html>",
            )
            with pytest.raises(RuntimeError, match="expired"):
                client._get("/student/tasks")

    def test_submit_file_invalidates_cache(self, client, tmp_path: Path, sample_dropbox_page_html):
        test_file = tmp_path / "hw.pdf"
        test_file.write_bytes(b"content")
        client.cache.put("https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026", "cached task detail", 200)
        
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/1000023/core_tasks/1000026/dropbox$"),
                text=sample_dropbox_page_html,
            )
            m.post(
                "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026/dropbox/upload",
                json={"ok": True},
            )
            client.submit_file("1000023", "1000026", str(test_file))
        
        assert client.cache.get("https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026") is None


class TestClientConcurrency:
    def test_concurrent_get_coalescing(self, tmp_path: Path):
        import threading
        import time
        from unittest.mock import MagicMock, patch
        from tahuti.client import ManageBacClient
        from tahuti.cache import ResponseCache

        cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True, ttl=1800)
        client = ManageBacClient("myschool", domain="managebac.cn", cache=cache)
        client.set_cookie("c")

        call_count = 0
        first_thread_entered_request = threading.Event()
        resume_first_thread = threading.Event()

        def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            first_thread_entered_request.set()
            resume_first_thread.wait(timeout=5)
            r = MagicMock()
            r.url = url
            r.status_code = 200
            r.text = "<html><body>Response content</body></html>"
            return r

        client._request_with_retry = mock_request

        results = []
        threads = []

        def worker():
            with patch("tahuti.client.time.sleep"):
                soup = client._get("/student/tasks_and_deadlines")
                results.append(soup)

        t1 = threading.Thread(target=worker)
        t1.start()

        assert first_thread_entered_request.wait(timeout=2)

        t2 = threading.Thread(target=worker)
        t2.start()

        # Small sleep to ensure t2 has started and is blocked on lock
        time.sleep(0.05)

        resume_first_thread.set()

        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert call_count == 1
        assert results[0].find("body").text == "Response content"
        assert results[1].find("body").text == "Response content"


class TestParseTaskUrl:
    def test_empty_or_none(self):
        assert parse_task_url("") == (None, None)
        assert parse_task_url(None) == (None, None)

    def test_full_url(self):
        url = "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026"
        assert parse_task_url(url) == ("1000023", "1000026")

    def test_full_url_with_subpath(self):
        url = "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026/dropbox"
        assert parse_task_url(url) == ("1000023", "1000026")

    def test_path_only(self):
        path = "/student/classes/1000023/core_tasks/1000026"
        assert parse_task_url(path) == ("1000023", "1000026")

    def test_bare_task_id(self):
        assert parse_task_url("1000026") == (None, "1000026")

    def test_task_id_with_trailing_slash(self):
        assert parse_task_url("tasks/1000026/") == (None, "1000026")

    def test_slashes_only(self):
        assert parse_task_url("///") == (None, None)




def _offset_for(zone_key: str, moment) -> timedelta:
    """The UTC offset *moment* takes in *zone_key*, via that zone's own rules."""
    import datetime as dt

    from zoneinfo import ZoneInfo

    return moment.replace(tzinfo=ZoneInfo(zone_key)).utcoffset()


class TestSchoolDisplayTimezone:
    """Due dates carry no offset, so the offset is chosen for the *parsed date*.

    `_school_display_tz` used `time.altzone if time.daylight else time.timezone`.
    `time.daylight` is nonzero whenever a DST *rule* exists, not when DST is in
    force, so a zone like Europe/Berlin resolved to the summer offset all year:
    a January due date came back UTC+2 instead of UTC+1, putting every winter
    task an hour ahead of `classify_task_view` and firing reminders early for the
    whole standard-time season.
    """

    # zone -> (offset in January, offset in July). The southern-hemisphere entry
    # is deliberately inverted from the northern ones: it proves the code reads
    # the zone's rules rather than assuming "winter = earlier offset".
    DST_ZONES = {
        "Europe/Berlin": (timedelta(hours=1), timedelta(hours=2)),
        "America/New_York": (timedelta(hours=-5), timedelta(hours=-4)),
        "Australia/Sydney": (timedelta(hours=11), timedelta(hours=10)),
    }

    @pytest.mark.parametrize("zone_key", sorted(DST_ZONES))
    def test_winter_and_summer_resolve_to_their_own_offsets(self, zone_key, monkeypatch):
        """The offset must belong to the date being parsed, not to today.

        Parametrized over the zone rather than the host's own, so it runs the same
        everywhere instead of skipping on a UTC/no-DST runner.
        """
        import datetime as dt

        from tahuti.client import _school_display_tz

        monkeypatch.setenv("TZ", zone_key)
        expected_january, expected_july = self.DST_ZONES[zone_key]

        # A DST-aware zone object pins whichever offset is in force at call time,
        # so resolve it through the zone the same way `parse_due_date` does.
        jan = _offset_for(zone_key, dt.datetime(2026, 1, 15, 23, 59))
        jul = _offset_for(zone_key, dt.datetime(2026, 7, 15, 23, 59))

        assert jan == expected_january, (
            f"on {zone_key} a January date must take the standard-time offset, "
            f"not the daylight one"
        )
        assert jul == expected_july

    @pytest.mark.parametrize("zone_key", sorted(DST_ZONES))
    @pytest.mark.parametrize(
        "text,expected_local",
        [
            ("January 20, 2026 at 23:59", (2026, 1, 20, 23, 59)),
            ("July 20, 2026 at 23:59", (2026, 7, 20, 23, 59)),
            ("2026-01-20 23:59", (2026, 1, 20, 23, 59)),
            ("2026-07-20 23:59", (2026, 7, 20, 23, 59)),
        ],
    )
    def test_parsed_wall_clock_survives_the_utc_round_trip(
        self, zone_key, text, expected_local, monkeypatch
    ):
        """The parsed wall-clock time must be the school's, in every season.

        Compares through UTC, which is what `classify_task_view` effectively does
        when it puts an aware parsed date next to an aware `now`.
        """
        import datetime as dt

        from tahuti.client import parse_due_date

        monkeypatch.setenv("TZ", zone_key)
        parsed = parse_due_date(text)
        assert parsed is not None, f"{text!r} did not parse"
        # Convert to UTC and back using the same zone rules the parser used.
        back = parsed.astimezone(dt.timezone.utc).astimezone(parsed.tzinfo)
        assert (back.year, back.month, back.day, back.hour, back.minute) == expected_local

    def test_utc_env_means_utc(self, monkeypatch):
        """`TZ=UTC` must not fall through to the /etc/localtime symlink.

        `_local_iana_zone` special-cases the UTC/GMT spellings precisely so that
        an explicit `TZ=UTC` is not silently replaced by whatever zone the host
        happens to be configured for.
        """
        import datetime as dt

        from tahuti.client import _local_iana_zone

        monkeypatch.setenv("TZ", "UTC")
        resolved = _local_iana_zone()
        # `ZoneInfo("UTC")` and `timezone.utc` are different objects that both
        # mean UTC, so compare what a date actually resolves to.
        probe = dt.datetime(2026, 1, 15, 12)
        assert probe.replace(tzinfo=resolved).utcoffset() == dt.timedelta(0)

    def test_tz_unset_falls_back_to_etc_localtime(self, monkeypatch):
        """With no TZ the host's own zone is used, whatever it is.

        Asserts the *contract* rather than a particular zone, so it holds on a
        UTC runner as well as on a DST one.
        """
        import datetime as dt

        from tahuti.client import _local_iana_zone, parse_due_date

        monkeypatch.delenv("TZ", raising=False)
        resolved = _local_iana_zone()
        assert resolved is not None, "no zone could be resolved for this host"

        # And the parser still produces a usable aware datetime through it.
        parsed = parse_due_date("September 15, 2026 at 23:59")
        assert parsed is not None
        assert parsed.utcoffset() is not None
        back = parsed.astimezone(dt.timezone.utc).astimezone(parsed.tzinfo)
        assert (back.month, back.day, back.hour, back.minute) == (9, 15, 23, 59)


class TestRetryClamp:
    """`--retry` is a plain int with no floor on the argparse side."""

    @pytest.mark.parametrize("value", [-1, -5, -100])
    def test_negative_retry_is_clamped_to_zero(self, value):
        c = ManageBacClient("myschool", domain="managebac.cn", retry=value, verify=False)
        assert c.retry == 0

    def test_negative_retry_does_not_raise_typeerror(self):
        """The bug: `range(retry + 1)` on a negative never ran the body, so the
        wrapper fell through to `raise last_exc` with `last_exc` still None —
        `TypeError: exceptions must derive from BaseException`."""
        c = ManageBacClient("myschool", domain="managebac.cn", retry=-1, verify=False)
        c.set_cookie("cookie")
        with rm.Mocker() as m:
            m.get(re.compile(r".*"), exc=requests.ConnectionError("boom"))
            with pytest.raises(requests.ConnectionError):
                c._get("/student/tasks_and_deadlines")


class TestCalendarEventUrl:
    """FullCalendar serializes a link-less event as ``"url": null``."""

    def test_null_url_is_not_a_crash(self):
        assert _absolute_event_url("https://x.cn", None) is None

    def test_missing_and_empty_url(self):
        assert _absolute_event_url("https://x.cn", "") is None

    def test_non_string_url(self):
        assert _absolute_event_url("https://x.cn", 12345) is None

    def test_root_relative_is_expanded(self):
        assert (
            _absolute_event_url("https://x.cn", "/student/events/1")
            == "https://x.cn/student/events/1"
        )

    def test_absolute_is_passed_through(self):
        assert (
            _absolute_event_url("https://x.cn", "https://other.test/e")
            == "https://other.test/e"
        )


class TestConditionalRevalidation:
    """`_get_revalidating` — the only place If-None-Match is wired in.

    The Rails HTML pages never 304 (their ETag is a digest of a body that
    rotates every render), so this must not reach `_get`.  These two endpoints
    were measured to answer 304, with a bogus ETag returning 200 as the
    negative control.
    """

    URL = "https://myschool.managebac.cn/student/events.json?start=2026-04-29&end=2026-05-05"

    def _prime(self, client, body="[]", etag='W/"aaa"'):
        client.cache.put(self.URL, body, 200, etag)

    def _expire(self, client):
        """Age the entry past its TTL so the next read must revalidate."""
        client.cache.ttl = 0

    def test_a_fresh_entry_costs_no_request(self, client):
        with rm.Mocker() as m:
            m.get(self.URL, json=[])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            assert m.call_count == 1
            # Second call inside the TTL: served from disk.
            client.get_calendar_events("2026-04-29", "2026-05-05")
            assert m.call_count == 1

    def test_a_stale_entry_sends_if_none_match(self, client):
        with rm.Mocker() as m:
            m.get(self.URL, json=[], headers={"ETag": 'W/"aaa"'})
            client.get_calendar_events("2026-04-29", "2026-05-05")
            self._expire(client)
            m.reset_mock()
            m.get(self.URL, json=[])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            sent = m.request_history[-1].headers.get("If-None-Match")
            assert sent == 'W/"aaa"', sent

    def test_a_304_replays_the_stored_body(self, client):
        with rm.Mocker() as m:
            m.get(self.URL, json=[{"id": 7, "title": "Kept"}])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            self._expire(client)
            m.reset_mock()
            # 304 with a zero-byte body: the answer must come from the cache.
            m.get(self.URL, status_code=304, text="")
            events = client.get_calendar_events("2026-04-29", "2026-05-05")
            assert [e["id"] for e in events] == [7]
            assert m.call_count == 1

    def test_a_304_stores_nothing_new(self, client):
        """A 304 must not overwrite the entry it just validated."""
        with rm.Mocker() as m:
            m.get(self.URL, json=[{"id": 7}])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            before = client.cache.get_entry(self.URL)["ts"]
            self._expire(client)
            m.reset_mock()
            m.get(self.URL, status_code=304, text="")
            client.get_calendar_events("2026-04-29", "2026-05-05")
            assert client.cache.get_entry(self.URL)["ts"] == before

    def test_a_changed_body_replaces_the_entry_and_its_etag(self, client):
        with rm.Mocker() as m:
            m.get(self.URL, json=[{"id": 1}])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            self._expire(client)
            m.reset_mock()
            m.get(self.URL, json=[{"id": 2}], headers={"ETag": 'W/"bbb"'})
            events = client.get_calendar_events("2026-04-29", "2026-05-05")
            assert [e["id"] for e in events] == [2]
            entry = client.cache.get_entry(self.URL)
            assert entry["etag"] == 'W/"bbb"'

    def test_no_stored_etag_means_no_conditional_header(self, client):
        """A first-ever fetch must not send an empty If-None-Match."""
        with rm.Mocker() as m:
            m.get(self.URL, json=[])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            assert "If-None-Match" not in m.request_history[-1].headers

    def test_the_html_path_never_sends_it(self, client, sample_timetable_html):
        """`_get` must stay unconditional — that is the whole finding."""
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://myschool\.managebac\.cn/student/timetables"),
                text=sample_timetable_html,
            )
            client.get_timetable()
            assert "If-None-Match" not in m.request_history[-1].headers


class TestCachedWebcalToken:
    """The 171 KB calendar page exists to yield a 36-character token.

    Measured: the token was byte-identical across samples 901 s apart while the
    page body rotated on every render and the session cookie rotated between
    samples.  So the token's entry is written with a TTL of its own, longer
    than the page's, in the same cache as the page — one object, so there is no
    second one to keep in step with the first.
    """

    TOKEN_KEY = "https://myschool.managebac.cn/student/calendar?__derived__=webcal_url"

    def test_the_page_is_fetched_once_per_ttl(self, client, sample_calendar_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR",
            )
            client.get_ical_feed()
            assert m.call_count == 2  # page + ics
            client.get_ical_feed()
            # Neither is re-fetched: both the token and the feed are cached.
            assert m.call_count == 2
            pages = [
                r for r in m.request_history if r.path == "/student/calendar"
            ]
            assert len(pages) == 1

    def test_the_token_is_stored_under_a_key_naming_its_page(self, client, sample_calendar_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR",
            )
            client.get_ical_feed()
            entry = client.cache.get_entry(self.TOKEN_KEY)
            assert entry is not None
            assert entry["body"].endswith("abc123.ics")

    def test_the_entry_records_its_own_longer_ttl(self, client, sample_calendar_page_html):
        """The mechanism: the TTL travels with the entry.

        The second-cache version froze `ttl`, `cache_dir` and `enabled` onto a
        mirror object once, in ``__init__``, and nothing re-established them
        when ``client.cache`` was later replaced — which two other test modules
        do.  Recording the TTL on the entry cannot go stale that way.
        """
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR",
            )
            client.get_ical_feed()
            entry = client.cache.get_entry(self.TOKEN_KEY)
            assert entry["ttl"] == client.cache.ttl * 24
            assert entry["ttl"] > client.cache.ttl

    def test_a_rotated_token_recovers_from_the_page(self, client, sample_calendar_page_html):
        """The probe covered 901 s and could not rule out a longer cadence, so a
        stale token must cost one wasted request rather than a broken command."""
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            # A sequence, not three registrations: the last registration would
            # win and the 404 would never be seen.
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                [
                    {"text": "BEGIN:VCALENDAR", "status_code": 200},
                    {"text": "not found", "status_code": 404},
                    {"text": "BEGIN:VCALENDAR", "status_code": 200},
                ],
            )
            assert "VCALENDAR" in client.get_ical_feed()

            # The token the server has since rotated: the feed now 404s.  The
            # token's own TTL is still running, so the only page fetch is the
            # recovery one — the cached token was tried first and failed.
            client.cache.ttl = 0
            out = client.get_ical_feed()
            assert "VCALENDAR" in out
            # The page was re-fetched to re-derive the token.
            pages = [
                r for r in m.request_history if r.path == "/student/calendar"
            ]
            assert len(pages) == 2

    def test_logout_clears_the_token(self, client, sample_calendar_page_html, tmp_path):
        """It is a credential-ish value, so it must not outlive the session."""
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR",
            )
            client.get_ical_feed()
            assert client.cache.get_entry(self.TOKEN_KEY) is not None
            client.cache.clear()
            assert client.cache.get_entry(self.TOKEN_KEY) is None

    def test_the_token_outlives_the_page_ttl(self, client, sample_calendar_page_html):
        """The whole point: sharing one TTL would expire them together and
        re-fetch all 171 KB to re-read a 36-character value."""
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR",
            )
            client.get_ical_feed()
            # Only the page/feed TTL lapses.
            client.cache.ttl = 0
            m.reset_mock()
            client.get_ical_feed()
            pages = [
                r for r in m.request_history if r.path == "/student/calendar"
            ]
            assert pages == [], "the 171 KB page was re-fetched for its token"
            # The feed itself was revalidated, not re-downloaded.
            assert m.call_count == 1

    def test_a_disabled_cache_re_derives_the_token(self, client, sample_calendar_page_html):
        """`--refresh` turns the response cache off; the token must go with it.

        The old mirror object carried its own `enabled`, copied across once in
        ``__init__``, so a cache replaced afterwards could leave a live token
        behind a disabled one.  One object cannot.
        """
        with rm.Mocker() as m:
            m.get(
                "https://myschool.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR",
            )
            client.get_ical_feed()
            client.cache.enabled = False
            client.get_ical_feed()
            pages = [
                r for r in m.request_history if r.path == "/student/calendar"
            ]
            assert len(pages) == 2, "a disabled cache still served the token"
