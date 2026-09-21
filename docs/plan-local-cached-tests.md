# Specification: Local-Only Cached Replay Test Suite

Last updated: 2026-09-21.

This document specifies the design, architecture, and safety rules for the offline cached replay test suite in `tahuti`.

---

## 1. Context & Motivation

### 1.1 The Failure of Synthetic Mock HTML Tests
Previously, `tahuti` contained over 3,600 lines of tests that used hand-crafted, synthetic HTML strings to test parser functions (`get_classes`, `get_class_tasks`, `_extract_attachments`, etc.). 

These synthetic tests suffered from fatal flaws:
1. **False confidence:** The mocks were constructed by developers to match what they *thought* the parser wanted, rather than how ManageBac actually renders pages.
2. **Markup drift:** When ManageBac changed layout classes, script blobs, or table structures, the tests continued to pass while the live scraper broke.
3. **High maintenance overhead:** Every minor change required updating brittle, hand-written HTML fixtures that did not represent reality.

All synthetic mock HTML tests were deleted in commit `81b168f`.

### 1.2 The Solution: Real Cached Responses
ManageBac's `ResponseCache` already writes every HTTP response to disk as JSON:
```json
{
  "url": "https://beijing101.managebac.cn/student/classes/11516148/core_tasks",
  "status": 200,
  "body": "<!DOCTYPE html>...",
  "ts": 1726914420.0
}
```
Currently, the local development environment has 80+ recorded responses in `~/.config/tahuti/cache/57affa805892591f/`, covering:
- Student dashboard (`/student/dashboard`)
- Class core task lists (`/student/classes/<id>/core_tasks`)
- Task detail pages (`/student/classes/<id>/core_tasks/<id>`)
- Task dropboxes (`/student/classes/<id>/core_tasks/<id>/dropbox`)
- Calendar and WebCal feeds (`/student/calendar`)
- Notifications (`/student/notifications`)

Replaying these real responses through `ManageBacClient` provides 100% realistic scraper verification without network requests.

---

## 2. Privacy & Security Rules

1. **Strictly Local & Gitignored:** Real cached responses contain sensitive student data (student names, ID numbers, class rosters, teacher comments, and grades).
2. **Never Commit Cache Dumps:** The `.gitignore` must strictly ignore `~/.config/tahuti/cache/`, local fixture directories under `tests/fixtures/cache/`, and any `*.cache.json` dumps.
3. **Graceful CI Skip:** Automated CI runners without access to the local cache must cleanly skip the cached replay test suite rather than fail:
   ```python
   pytestmark = pytest.mark.skipif(
       not has_local_cache(),
       reason="Local ManageBac response cache not found"
   )
   ```

---

## 3. Architecture & Test Harness

### 3.1 Offline Test Harness
The test harness initializes a `ManageBacClient` pointed at the local response cache and installs a guard that raises an error if any network request is attempted:

```python
class OfflineClientHarness:
    def __init__(self, cache_dir: Path, domain: str = "beijing101.managebac.cn"):
        self.cache_dir = cache_dir
        self.client = ManageBacClient(
            domain=domain,
            session_cookie="dummy_offline_cookie",
            cache_dir=cache_dir,
            cache_ttl=999999999,  # Never expire during test replay
        )
        # Network circuit breaker
        self.client.session.send = self._network_blocked

    def _network_blocked(self, *args, **kwargs):
        raise RuntimeError("Network access attempted in offline cached test suite!")
```

Because `client._get(url)` checks `self.cache.get(url)` first, any cached page will be loaded directly from disk without hitting the network. If a test requests an uncached URL, the circuit breaker immediately triggers with a clear error.

### 3.2 Target Coverage
The replay test suite (`tests/test_cached_replay.py`) will test:

1. **`get_classes()` Navigation Filtering & Roster:**
   - Parses the real `/student/dashboard`.
   - Confirms navigation labels ("All Classes", "Browse All Classes") are excluded.
   - Confirms legitimate classes (including language/review classes) are extracted.
   - Verifies class IDs, titles, and teacher names match expected schema.

2. **`get_class_tasks()` Task Extraction:**
   - Replays `/student/classes/<id>/core_tasks`.
   - Verifies task count, task IDs, titles, due dates, and statuses.
   - Verifies attachment URLs (both short blob paths and S3 links).
   - Verifies score/criteria extraction without fragile expected-grade heuristics.

3. **`get_task()` & Dropbox Inspection:**
   - Replays task details and `/dropbox` pages.
   - Verifies description extraction, attachment file metadata, and submission statuses.

4. **`crawl_all()` Offline Integration:**
   - Executes full aggregation across cached classes.
   - Verifies sorting by due date, task deduplication, and JSON payload consistency.

5. **`get_calendar()` & WebCal Feed:**
   - Parses real `/student/calendar` and `.ics` feeds.
   - Validates event start/end timestamps and title parsing.

---

## 4. Implementation Steps

1. **Locate or Link Local Cache Directory:**
   - Helper function `find_local_cache()` searches:
     - `~/.config/tahuti/cache/` (finds first subdirectory containing `.json` cache files)
     - Environment variable `TAHUTI_TEST_CACHE_DIR`
2. **Add `tests/test_cached_replay.py`:**
   - Minimalist, assert-based test functions adhering to Ponytail guidelines (no boilerplate, no extra test dependencies).
3. **Verify Zero Regressions:**
   - Run pytest with local cache detected -> runs all real-data assertions.
   - Run pytest with cache disabled -> cleanly skips without test failures.
