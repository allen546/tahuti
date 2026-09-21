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
        assert len(tasks) == 6, f"Expected 6 tasks for class 11516148, got {len(tasks)}"

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

