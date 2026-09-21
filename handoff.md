# Handoff: tahuti

Last updated: 2026-09-21.

This document summarizes the state of **tahuti** (formerly `mb-crawler`), what has been resolved, how the codebase is structured, and the remaining tasks.

---

## 1. Project Overview

**tahuti** is a Python toolkit and CLI for **ManageBac** (a school management platform by Faria Education Group). Since ManageBac does not provide a public API for students, tahuti acts as an authenticated client that fetches and parses web pages and internal JSON endpoints.

Supported origins:
- `managebac.com` (International)
- `managebac.cn` (China)

Components in `src/tahuti/`:
- **SDK (`client.py`)**: `ManageBacClient` handles session cookies, authentication, requests, caching, and HTML/JSON parsing.
- **CLI (`__main__.py`)**: Command-line interface (`tahuti list`, `tahuti grades`, `tahuti submit`, `tahuti download`, etc.).
- **MCP Server (`mcp_server.py`)**: Model Context Protocol server exposing ManageBac tools to AI assistants (Claude Desktop, Cursor).
- **Daemon (`daemon/`)**: Background service that periodically polls ManageBac and dispatches notifications/webhooks.

**Architecture Principle**: The CLI is the primary reference implementation. The MCP server wraps the same underlying client methods and should maintain behavioral parity with the CLI.

---

## 2. Repository and Worktree Layout

### Main Checkout
- `/Users/allen/Desktop/t8/mb-crawler` (currently on `fix/mcp-divergences-efficiency`, superseded)

### Active Worktrees
- `/Users/allen/Desktop/t8/mb-crawler-wt-handoff`: Branch `fix/mcp-cli-parity-and-efficiency` (**active working branch**). All main fixes, efficiency updates, test cleanups, and this document live here.
- `/Users/allen/Desktop/t8/mb-crawler-wt-remove-host-guard`: Branch `fix/remove-host-guard`. Contains the secured in-flight work fixing attachment downloads and removing overly restrictive host guards.

### Python Environment & Running Tests
The main virtualenv is at `/Users/allen/Desktop/t8/mb-crawler/.venv`. When running tests in a worktree, set `PYTHONPATH` so python loads the worktree's `src/`:

```bash
cd <worktree>
PYTHONPATH=$PWD/src /Users/allen/Desktop/t8/mb-crawler/.venv/bin/python -m pytest -q
```

---

## 3. What Was Recently Resolved

### 3.1 Reverted `submit` Local Snapshot Write
- **What happened:** A previous commit (`2c502d6`) attempted to save 1 HTTP request during `tahuti submit` by updating the local snapshot in-place instead of refetching the class grade page. However, this left `grade_letter` and sibling tasks stale and caused divergence with the MCP server's `submit_file`.
- **Resolution:** Reverted in commit `2d10624`. `submit` now eagerly calls `client.get_class_tasks(class_id, bypass_cache=True)` in both CLI and MCP, ensuring consistent, fresh state immediately after an upload.

### 3.2 Debunked `student_name` Regression Concern
- **Investigation:** A prior handoff note raised concern that skipping notification fetches in `crawl_all(fetch_notifications=False)` would leave `student_name` as `None` on schools where the dashboard supposedly lacked profile links.
- **Finding:** Verified directly against actual cached ManageBac responses (`beijing101.managebac.cn`). Both `/student/dashboard` and `/student/notifications` have identical `<a href="/student/profile">` anchors. Calling `client._capture_student_name(soup)` on the real dashboard HTML captures the student's name (`Allen (孙英祺) Yingqi Sun`) without issue. Skipping notification requests on task crawls is safe.

### 3.3 Purged All Mock HTML Parser Tests (-3,604 lines)
- **Problem:** Many tests tested HTML parsers by feeding them hand-rolled, synthetic HTML strings. These mocks were often written to match the parser rather than real ManageBac markup, providing false confidence while live scraping broke.
- **Resolution:** Deleted all synthetic HTML parser tests (commit `81b168f`):
  - Removed `tests/test_submission_status_parsing.py`
  - Removed `tests/test_client_scrape_fixes.py`
  - Removed `tests/test_feedback.py`
  - Removed `tests/test_daemon_stealth.py`
  - Removed mock HTML client tests from `tests/test_submissions.py`
  - Removed mock HTML scraper test classes from `tests/test_client.py`
  - Removed synthetic HTML fixtures from `tests/conftest.py`
- **Result:** Test suite is down to **1,443 passing unit tests** (running in ~20 seconds), covering CLI parsing, configuration, keyring, session management, caching TTLs, daemon scheduling, and event diffing.

### 3.4 Secured In-Flight Host Guard Work Outside `/tmp`
- The uncommitted attachment fix was previously located in a temporary directory (`/private/tmp/guard-removal-58347`).
- All unstaged edits and untracked test files were committed on branch `fix/remove-host-guard` (commit `710b840`).
- The worktree was relocated to `/Users/allen/Desktop/t8/mb-crawler-wt-remove-host-guard`.

### 3.5 Earlier Landed Fixes
- **CLI/MCP Parity (`540a560`):** `list_tasks` in MCP now calls `crawl_all` so no classes are missed; `list_classes` gets its roster from `get_classes()` rather than reconstructing it from task links.
- **Session Health Check (`48e9962`):** Simplified the session check from a heavy dashboard GET to a fast HEAD request.
- **Webcal Token TTL (`3848b19`):** Handled webcal tokens with per-entry cache TTL instead of maintaining a secondary cache object.
- **Logout Purge & Domain Setup (`6e8dd90`):** Added `logout --purge` to wipe school profiles from `config.json`, and set `domain` defaults to `None` so users are only prompted if domain is unknown.

### 3.6 Removed `tahuti download`, Host Guard, and Domain Allowlist (commit `1d259e3`)
- **Removed `tahuti download`:** Downloading files is left to standard tools (`curl`, browsers, agents) using the URLs returned by `tahuti view` and MCP `get_task`. Deleted `cmd_download`, `download` subparser, `_safe_filename`, `_expected_download_host`, and `_refuse_reason_for_url` (~330 lines deleted).
- **Eliminated Domain Allowlist (`ALLOWED_DOMAINS`):** Replaced hardcoded allowlist with bare hostname syntax validation (`_DOMAIN_RE`). Any valid ManageBac hostname is supported.
- **Removed Cross-Host Guard (`_assert_same_host`):** ManageBac redirects to AWS S3 / CDNs. Python's `requests.Session` scopes cookies by domain (RFC 6265), preventing session exfiltration to S3. Off-host HTTPS redirects are followed safely up to 10 hops.
- **Display URL Formatter:** Added `display_url` in `src/tahuti/formatters.py` to truncate long pre-signed S3 URLs and hide signature query strings in terminal pretty output.
- **Tests Updated:** Removed `tests/test_download.py` and old download test cases. Added `tests/test_domain_shape_and_hub.py` (37 unit tests). Full suite: **1,451 passed, 1 xfailed, 1 xpassed in 18.64s** (net -838 lines).

### 3.7 Resolved Small Code Issues (5.2, 5.3, 5.5)
- **5.2 `get_classes()` Navigation Filter:** Replaced naive substring matching (`kw in name.lower()`) with exact matching against `_NAV_CLASS_LABELS` (`frozenset({"all classes", "browse", "view", "overview", "browse all classes", "view class"})`). Legitimate class names containing substrings like "Worldview", "Review", or "Advanced View" are no longer dropped.
- **5.3 Malformed `profiles` Defense:** Added `_as_dict()` helper in `config.py` so `load_state()`, `save_profile()`, `save_session()`, `clear_session()`, and `purge_profiles()` safely tolerate `null`, string, or missing `profiles` maps without raising `AttributeError` or `TypeError`.
- **5.4 SDK Default Domain Clarification:** Analyzed `ManageBacClient.__init__(..., domain="managebac.com")`. Confirmed this is **deliberate library behavior** providing a convenient default for direct SDK consumers and unit tests. The actual bug previously fixed was in `config.py` (`ProfileConfig.domain`), where defaulting to `"managebac.com"` hid unconfigured CLI profile states.
- **5.5 Rate Limiter Thread Safety:** Serialized `_respect_rate_limit` using `self._rate_limit_lock` so concurrent worker threads are properly delayed and spaced out by at least `request_delay`.
- **Test Suite Hygiene:** Fixed a mock leak in `test_concurrent_get_coalescing` where concurrent threads entered `patch("tahuti.client.time.sleep")`, inadvertently persisting a Mock into subsequent tests.

### 3.8 Removed `count-grade-freq` (commit `b1ac17c`)
- Deleted `tahuti count-grade-freq` CLI command, `count_grade_frequencies()` client method, and MCP `count_grade_frequencies` tool (-198 lines).
- Grade statistics are trivially replaced by standard shell one-liners (e.g. `tahuti list --format json | jq -r '.[].score // empty' | sort | uniq -c | sort -nr`).

---

## 4. Upcoming Architectural Plans (Repo Artifacts)

Detailed technical specifications are documented in `docs/`:
1. [`docs/plan-refactor-profile-and-grades.md`](docs/plan-refactor-profile-and-grades.md):
   - **Delete `tahuti grades` entirely:** Task scores and criteria are already crawled by `tahuti list`. The "Expected Grade" calculation was an unweighted heuristic over Highcharts points that ManageBac does not officially expose.
   - **Single-account flattening:** Remove multi-profile complexity (`--profile`, `profiles.<name>`, `purge_profiles()`, multi-profile merging) in favor of flat `config.json` and `session.json`. Multi-account testing can use standard Unix isolation (`HOME=/dir`).
   - **Clean credential paths:** Delete stale helper functions (`creds_filename`, `default_creds_path`, `legacy_creds_path`, `creds_paths`, `all_creds_paths`) in favor of direct `resolve_creds_path()`.
   - **Task verb consolidation (Issue 4):** Fold `submissions` and `feedback` into `tahuti view` and `tahuti submit`.
2. [`docs/plan-local-cached-tests.md`](docs/plan-local-cached-tests.md):
   - **Local-only cached replay suite:** Replay real ManageBac HTTP responses recorded in `~/.config/tahuti/cache/` through `ManageBacClient` with a network circuit breaker.
   - Verifies real dashboard class parsing, task extraction, attachment links, and full crawl aggregation offline.
   - Strictly local and gitignored to protect sensitive student data; cleanly skipped on CI without cache.

---

## 5. Remaining Open Issues

### 5.6 Repository Cleanup
- Delete stray tracked file `json` in repository root.
- Remove leftover test files in parent directory (`/Users/allen/Desktop/t8/mb-crawler-test-*`).
- Delete scratch branch `trial5`.
- Prune merged/unused git worktrees.

---

## 6. Verification Summary

Current test suite status on `fix/mcp-cli-parity-and-efficiency`:

```bash
$ PYTHONPATH=$PWD/src /Users/allen/Desktop/t8/mb-crawler/.venv/bin/python -m pytest -q
1449 passed, 1 xfailed, 1 xpassed in 42.66s
```

All 1,449 unit and integration tests pass cleanly.
