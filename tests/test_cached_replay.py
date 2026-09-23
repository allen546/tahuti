"""Offline test suite replaying real ManageBac HTTP responses from local disk cache.

Specification: docs/plan-local-cached-tests.md
Adheres to Ponytail guidelines: assert-based, minimal, zero network requests,
safe CI skipping when local cache is unavailable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tahuti.cache import ResponseCache
from tahuti.client import ManageBacClient


def find_local_cache() -> Path | None:
    """Locate local ManageBac response cache directory.

    Checks:
    1. TAHUTI_TEST_CACHE_DIR environment variable (set to 'none'/'disabled' to disable)
    2. ~/.config/tahuti/cache/<subfolder> containing .json files
    """
    env_dir = os.environ.get("TAHUTI_TEST_CACHE_DIR")
    if env_dir is not None:
        if env_dir.strip().lower() in ("0", "false", "none", "off", "disabled", "disable", ""):
            return None
        p = Path(env_dir).expanduser()
        if p.is_dir():
            if any(p.glob("*.json")):
                return p
            for sub in p.iterdir():
                if sub.is_dir() and any(sub.glob("*.json")):
                    return sub
        return None

    default_root = Path("~/.config/tahuti/cache").expanduser()
    if default_root.is_dir():
        for sub in default_root.iterdir():
            if sub.is_dir() and any(sub.glob("*.json")):
                return sub
        if any(default_root.glob("*.json")):
            return default_root
    return None


def has_local_cache() -> bool:
    """True when a local response cache directory with .json files exists."""
    return find_local_cache() is not None


class OfflineClientHarness:
    """ManageBacClient wired strictly to local response cache with circuit breaker."""

    def __init__(self, cache_dir: Path, domain: str = "beijing101.managebac.cn"):
        self.cache_dir = cache_dir
        if "." in domain:
            school, self.domain = domain.split(".", 1)
        else:
            school, self.domain = domain, "managebac.com"

        # ReplayResponseCache ensures responses never expire during replay
        class _ReplayResponseCache(ResponseCache):
            def get(self, url: str, allow_stale: bool = True):
                return super().get(url, allow_stale=True)

        self.client = ManageBacClient(
            school=school,
            domain=self.domain,
            cache=_ReplayResponseCache(cache_dir, ttl=999999999),
        )
        self.client.set_cookie("dummy_offline_cookie")

        # Network circuit breaker: any outbound HTTP request raises immediately
        self.client.session.send = self._network_blocked

    def _network_blocked(self, *args, **kwargs):
        raise RuntimeError("Network access attempted in offline cached test suite!")


# ── Unit tests for harness and cache detection (runs everywhere including CI) ──


def test_find_local_cache_env_override(tmp_path, monkeypatch):
    """TAHUTI_TEST_CACHE_DIR points directly to a cache directory."""
    monkeypatch.setenv("TAHUTI_TEST_CACHE_DIR", str(tmp_path))
    assert find_local_cache() is None

    (tmp_path / "dummy.json").write_text('{"url": "https://foo"}', encoding="utf-8")
    assert find_local_cache() == tmp_path


def test_find_local_cache_subfolder(tmp_path, monkeypatch):
    """TAHUTI_TEST_CACHE_DIR points to root containing account subdirectories."""
    sub = tmp_path / "account_hash"
    sub.mkdir()
    (sub / "response.json").write_text('{"url": "https://foo"}', encoding="utf-8")
    monkeypatch.setenv("TAHUTI_TEST_CACHE_DIR", str(tmp_path))
    assert find_local_cache() == sub


def test_offline_harness_circuit_breaker(tmp_path):
    """Offline harness must raise if an uncached URL is requested."""
    harness = OfflineClientHarness(tmp_path)
    with pytest.raises(RuntimeError, match="Network access attempted"):
        harness.client._get("/student/uncached_random_page")


# ── Real Cached Replay Tests (skipped cleanly when no local cache is present) ──


@pytest.mark.skipif(
    not has_local_cache(),
    reason="Local ManageBac response cache not found",
)
class TestCachedReplay:
    @pytest.fixture(scope="class")
    @staticmethod
    def harness() -> OfflineClientHarness:
        cache_path = find_local_cache()
        assert cache_path is not None, "has_local_cache() was True but find_local_cache() returned None"
        return OfflineClientHarness(cache_path)

    def test_get_classes_roster_and_filtering(self, harness: OfflineClientHarness):
        """Parse real /student/dashboard: verify roster and exclude navigation items."""
        classes = harness.client.get_classes()
        assert len(classes) >= 5, f"Expected at least 5 classes, got {len(classes)}"

        # Confirm navigation items are filtered out
        for cid, name in classes.items():
            assert cid.isdigit(), f"Class ID should be digits: {cid}"
            assert name and len(name.strip()) > 0
            name_lower = name.lower()
            assert "all classes" not in name_lower
            assert "browse" not in name_lower

        # Confirm legitimate classes are present
        class_names = " ".join(classes.values())
        assert "English" in class_names or "Calculus" in class_names or "Physics" in class_names

    def test_get_class_tasks_extraction(self, harness: OfflineClientHarness):
        """Replay /student/classes/<id>/core_tasks: verify count, IDs, titles, due dates, statuses."""
        # Class 11516148 (Calculus)
        tasks = harness.client.get_class_tasks("11516148", "AP Calculus BC")
        assert len(tasks) == 7, f"Expected 7 tasks for class 11516148, got {len(tasks)}"

        statuses = set()
        views = set()
        for t in tasks:
            assert t["id"].isdigit(), f"Task ID should be numeric: {t['id']}"
            assert t["title"], f"Task title should not be empty: {t}"
            assert t["class_name"] == "AP Calculus BC"
            assert t["view"] in ("upcoming", "past", "overdue")
            views.add(t["view"])
            statuses.add(t["status"])

        assert "submitted" in statuses or "not-submitted" in statuses
        assert "upcoming" in views or "past" in views

    def test_task_detail_and_attachments(self, harness: OfflineClientHarness):
        """Replay task details: verify description and both short blob and S3 attachments."""
        # Task 27538237 in Chemistry 11516095 contains both short blob path and S3 attachment
        detail = harness.client.get_task_detail("/student/classes/11516095/core_tasks/27538237")
        assert detail is not None
        assert "description" in detail or "description_html" in detail
        assert detail.get("class_description") is not None

        attachments = detail.get("attachments", [])
        assert len(attachments) >= 2, f"Expected at least 2 attachments, got {len(attachments)}"

        urls = [a["url"] for a in attachments]
        # Verify short blob path
        assert any("/attachments/" in u for u in urls), f"Short blob attachment missing in {urls}"
        # Verify S3 link
        assert any("amazonaws.com" in u or ".s3." in u for u in urls), f"S3 attachment missing in {urls}"

        for a in attachments:
            assert a["name"], f"Attachment name missing: {a}"
            assert a["url"].startswith("http"), f"Attachment url invalid: {a}"
            assert a["source"] in ("description", "submission", "discussion")

    def test_task_dropbox_inspection(self, harness: OfflineClientHarness):
        """Replay task dropbox page: verify form extraction and submissions query."""
        # Test detail on unsubmitted task with dropbox
        detail = harness.client.get_task_detail("/student/classes/11516148/core_tasks/27610522")
        assert detail is not None
        assert detail.get("has_submit_button") is True
        assert detail.get("submission_status") == "not-submitted"

        # Inspect the cached dropbox page directly
        soup = harness.client._get("/student/classes/11516148/core_tasks/27610522/dropbox")
        form = soup.find("form", id=lambda x: x and "dropbox" in x)
        assert form is not None
        assert "edit_dropbox_" in form.get("id", "")

        # get_submissions() executes cleanly offline
        submissions = harness.client.get_submissions("11516148", "27610522")
        assert isinstance(submissions, list)

    def test_crawl_all_offline_integration(self, harness: OfflineClientHarness):
        """Full aggregation across all cached classes: deduplication, sorting, and schema consistency."""
        result = harness.client.crawl_all(fetch_notifications=True, fetch_details=False)

        # Schema consistency
        for key in ("student_name", "school", "base_url", "crawled_at", "upcoming", "past", "overdue", "notifications", "summary"):
            assert key in result, f"Key {key} missing from crawl_all result"

        assert result["student_name"], "Student name should be captured from cached response"
        assert result["school"] == "beijing101"

        # Task deduplication
        all_tasks = result["upcoming"] + result["past"] + result["overdue"]
        assert len(all_tasks) > 0, "Expected aggregated tasks across cached classes"
        all_ids = [t["id"] for t in all_tasks]
        assert len(all_ids) == len(set(all_ids)), "Duplicate task IDs found in crawl_all output"

        # Summary counts match list lengths
        assert result["summary"]["upcoming_count"] == len(result["upcoming"])
        assert result["summary"]["past_count"] == len(result["past"])
        assert result["summary"]["overdue_count"] == len(result["overdue"])

        # Notifications parsed
        notifs = result["notifications"]
        assert "items" in notifs
        assert len(notifs["items"]) > 0, "Expected notifications to be parsed from cache"
        for n in notifs["items"]:
            assert "id" in n
            assert "title" in n

    def test_calendar_and_webcal_feed(self, harness: OfflineClientHarness):
        """Replay /student/calendar and .ics feeds: parse events, timestamps, and titles."""
        # 1. Calendar page yields valid webcal URL
        soup = harness.client._get("/student/calendar")
        assert soup.find("a", href=re.compile(r"webcal://")) is not None
        webcal_url = harness.client._webcal_url()
        assert webcal_url.startswith("https://")
        assert ".ics" in webcal_url

        # 2. iCal feed content
        feed = harness.client.get_ical_feed()
        assert "BEGIN:VCALENDAR" in feed
        assert "BEGIN:VEVENT" in feed

        # 3. Event parsing
        events = []
        current: dict[str, str] = {}
        for line in feed.splitlines():
            if line == "BEGIN:VEVENT":
                current = {}
            elif line == "END:VEVENT":
                if "SUMMARY" in current:
                    events.append(current)
            elif ":" in line:
                k, v = line.split(":", 1)
                prop = k.split(";")[0]
                current[prop] = v

        assert len(events) >= 50, f"Expected >=50 events in feed, found {len(events)}"
        for ev in events:
            assert "SUMMARY" in ev and len(ev["SUMMARY"].strip()) > 0
            assert "DTSTART" in ev
            assert "DTEND" in ev
            assert "UID" in ev
            # Timestamp format YYYYMMDDTHHMMSS
            assert re.match(r"^\d{8}T\d{6}$", ev["DTSTART"]), f"Bad start timestamp: {ev['DTSTART']}"
            assert re.match(r"^\d{8}T\d{6}$", ev["DTEND"]), f"Bad end timestamp: {ev['DTEND']}"

    def test_get_class_grades_calculus(self, harness: OfflineClientHarness):
        """Replay /student/classes/11516148/core_tasks: parse overall grade, weights, and scale."""
        grades = harness.client.get_class_grades("11516148", "AP Calculus BC")
        assert grades["class_id"] == "11516148"
        assert grades["class_name"] == "AP Calculus BC"
        assert grades["overall"]["mark"] == "B"
        assert grades["overall"]["score"] == 81.67
        assert grades["categories_count"] == 5
        assert grades["assessed_categories_count"] == 2

        categories = {c["category"]: c for c in grades["grade_composition"]}
        assert "Homework" in categories
        assert categories["Homework"]["weight"] == 0.25
        assert categories["Homework"]["mark"] == "A"
        assert categories["Homework"]["score"] == 91.8
        assert "test" in categories
        assert categories["test"]["weight"] == 0.2
        assert "Class Attendance" in categories
        assert categories["Class Attendance"]["weight"] == 0.05

        # Check grading scale
        scale = grades["grade_scale"]
        assert scale.get("5") == "A"
        assert scale.get("4") == "B"
        assert scale.get("1") == "F"

    def test_get_class_grades_unassessed(self, harness: OfflineClientHarness):
        """Replay unassessed class (11516058): verify empty/pending overall grade."""
        grades = harness.client.get_class_grades("11516058")
        assert grades["class_id"] == "11516058"
        assert grades["overall"]["mark"] == "-"
        assert grades["overall"]["score"] is None

    def test_get_all_grades(self, harness: OfflineClientHarness):
        """Fetch all grades across roster: verify all classes aggregated."""
        all_grades = harness.client.get_all_grades()
        assert "classes" in all_grades
        classes = all_grades["classes"]
        assert len(classes) >= 5
        cid_set = {c["class_id"] for c in classes}
        assert "11516148" in cid_set
        for c in classes:
            assert "class_id" in c
            assert "class_name" in c
            assert "overall" in c
            assert "grade_composition" in c
            assert "grade_scale" in c

    def test_crawl_all_grade_enrichment(self, harness: OfflineClientHarness):
        """crawl_all() parses grades in single pass and enriches envelope & tasks."""
        result = harness.client.crawl_all(fetch_notifications=False, fetch_details=False)
        assert "classes" in result
        assert "class_grades" in result
        assert "11516148" in result["class_grades"]
        calc_grade = result["class_grades"]["11516148"]
        assert calc_grade["overall"]["mark"] == "B"

        calc_class = next(c for c in result["classes"] if c["id"] == "11516148")
        calc_name = calc_class["name"]
        calc_tasks = [t for t in result["upcoming"] + result["past"] + result["overdue"] if t.get("class_name") == calc_name or "Calculus" in t.get("class_name", "")]
        assert len(calc_tasks) > 0
        for t in calc_tasks:
            assert "class_overall" in t
            assert t["class_overall"]["mark"] == "B"
            assert t["class_overall"]["score"] == 81.67

    def test_class_and_grades_formatters(self, harness: OfflineClientHarness):
        """Formatters render clean tables and metadata; class.view NEVER lists tasks."""
        from tahuti.formatters import render_pretty

        calc_grades = harness.client.get_class_grades("11516148", "AP Calculus BC")
        all_grades = harness.client.get_all_grades()

        # 1. class.list / grades.all
        rendered_list = render_pretty({"ok": True, "command": "class.list", "profile": "default", "data": all_grades})
        assert "ID" in rendered_list
        assert "Class" in rendered_list
        assert "Overall Mark" in rendered_list
        assert "Calculus" in rendered_list
        assert "81.67%" in rendered_list

        # 2. class.view (must NOT include task list)
        rendered_view = render_pretty({"ok": True, "command": "class.view", "profile": "default", "data": calc_grades})
        assert "AP Calculus BC" in rendered_view
        assert "Overall Grade:" in rendered_view
        assert "B (81.67%)" in rendered_view
        assert "[Grade Composition]" in rendered_view
        assert "[Grading Scale]" in rendered_view
        # Ensure task details or task list headers are strictly absent
        assert "Upcoming Tasks" not in rendered_view
        assert "Past Tasks" not in rendered_view
        assert "Overdue Tasks" not in rendered_view
        assert "core_tasks" not in rendered_view

        # 3. grades.composition
        rendered_comp = render_pretty({"ok": True, "command": "grades.composition", "profile": "default", "data": all_grades})
        assert "Grade Composition Across All Classes" in rendered_comp
        assert "Calculus" in rendered_comp
        assert "Overall: B (81.67%)" in rendered_comp

        # 4. list command headers with overall grade
        list_result = harness.client.crawl_all(fetch_notifications=False, fetch_details=False)
        list_payload = {
            "ok": True,
            "command": "list",
            "profile": "default",
            "data": {
                "meta": {
                    "student_name": list_result.get("student_name"),
                    "school": list_result.get("school"),
                    "view": "all",
                },
                "summary": list_result.get("summary"),
                "tasks": {
                    "upcoming": list_result.get("upcoming"),
                    "past": list_result.get("past"),
                    "overdue": list_result.get("overdue"),
                },
            },
        }
        rendered_tasks = render_pretty(list_payload)
        assert "[B (81.67%)]" in rendered_tasks

    def test_mcp_class_tools(self, monkeypatch, harness: OfflineClientHarness):
        """MCP list_classes includes overall grades; get_class_grades returns canonical model."""
        import json
        from tahuti import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "build_client",
            lambda **kwargs: (None, harness.client, "test@example.com"),
        )

        # list_classes
        res_classes_raw = mcp_server.list_classes()
        res_classes = json.loads(res_classes_raw)
        assert "classes" in res_classes
        calc = next((c for c in res_classes["classes"] if c["id"] == "11516148"), None)
        assert calc is not None
        assert "Calculus" in calc["name"]
        assert calc["overall"]["mark"] == "B"
        assert calc["overall"]["score"] == 81.67

        # get_class_grades
        res_grades_raw = mcp_server.get_class_grades("11516148")
        res_grades = json.loads(res_grades_raw)
        assert res_grades["class_id"] == "11516148"
        assert res_grades["overall"]["mark"] == "B"
        assert len(res_grades["grade_composition"]) == 5
        assert res_grades["grade_scale"].get("5") == "A"



