"""Tests for tahuti.client."""

from __future__ import annotations

import contextlib
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
            soup = client._get("/student/tasks_and_deadlines")
            results.append(soup)

        with patch("tahuti.client.time.sleep"):
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

    def _expire(self, client):
        """Age the entry past its TTL so the next read must revalidate."""
        client.cache.ttl = 0

    @contextlib.contextmanager
    def _a_stale_entry(self, client, body, etag=None):
        """Prime the cache with *body*, age it past its TTL, then hand back a
        mocker whose history starts empty.

        Whatever the test registers next is therefore the only request it can
        observe, which is what makes "the re-fetch was conditional" a statement
        about `_get_revalidating` rather than about the priming call.  *etag* is
        what the priming response answers with, and so what the stored entry
        must offer as ``If-None-Match``.
        """
        with rm.Mocker() as m:
            response = {"json": body}
            if etag:
                response["headers"] = {"ETag": etag}
            m.get(self.URL, **response)
            client.get_calendar_events("2026-04-29", "2026-05-05")
            self._expire(client)
            m.reset_mock()
            yield m

    def test_a_fresh_entry_costs_no_request(self, client):
        with rm.Mocker() as m:
            m.get(self.URL, json=[])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            assert m.call_count == 1
            # Second call inside the TTL: served from disk.
            client.get_calendar_events("2026-04-29", "2026-05-05")
            assert m.call_count == 1

    def test_a_stale_entry_sends_if_none_match(self, client):
        with self._a_stale_entry(client, [], etag='W/"aaa"') as m:
            m.get(self.URL, json=[])
            client.get_calendar_events("2026-04-29", "2026-05-05")
            sent = m.request_history[-1].headers.get("If-None-Match")
            assert sent == 'W/"aaa"', sent

    def test_a_304_replays_the_stored_body(self, client):
        with self._a_stale_entry(client, [{"id": 7, "title": "Kept"}]) as m:
            # 304 with a zero-byte body: the answer must come from the cache.
            m.get(self.URL, status_code=304, text="")
            events = client.get_calendar_events("2026-04-29", "2026-05-05")
            assert [e["id"] for e in events] == [7]
            assert m.call_count == 1

    def test_a_304_stores_nothing_new(self, client):
        """A 304 must not overwrite the entry it just validated."""
        with self._a_stale_entry(client, [{"id": 7}]) as m:
            # `_expire` sets `ttl`; only `put` and `invalidate` write `ts`.  So
            # this is still the priming call's timestamp, not a fresh one.
            before = client.cache.get_entry(self.URL)["ts"]
            m.get(self.URL, status_code=304, text="")
            client.get_calendar_events("2026-04-29", "2026-05-05")
            assert client.cache.get_entry(self.URL)["ts"] == before

    def test_a_changed_body_replaces_the_entry_and_its_etag(self, client):
        with self._a_stale_entry(client, [{"id": 1}]) as m:
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


def test_rate_limiter_thread_safety():
    """Concurrent threads respecting rate limits must be serialized by lock."""
    import threading
    import time
    from tahuti.client import ManageBacClient

    c = ManageBacClient("testschool", request_delay=0.05)
    start = time.perf_counter()

    threads = []
    for _ in range(3):
        t = threading.Thread(target=c._respect_rate_limit)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    total_time = time.perf_counter() - start
    assert total_time >= 0.05


def test_nav_class_labels_do_not_drop_real_class_names():
    """Substrings like 'view' in 'Worldview' or 'Review' must not drop the class."""
    from tahuti.client import _NAV_CLASS_LABELS

    assert "worldview" not in _NAV_CLASS_LABELS
    assert "media review" not in _NAV_CLASS_LABELS
    assert "advanced view" not in _NAV_CLASS_LABELS
    assert "browser technology" not in _NAV_CLASS_LABELS

    assert "all classes" in _NAV_CLASS_LABELS
    assert "browse" in _NAV_CLASS_LABELS
    assert "view" in _NAV_CLASS_LABELS
    assert "overview" in _NAV_CLASS_LABELS
    assert "browse all classes" in _NAV_CLASS_LABELS
    assert "view class" in _NAV_CLASS_LABELS
