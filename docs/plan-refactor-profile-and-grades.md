# Specification: Single-Account Flattening & `grades` Removal

Last updated: 2026-09-21.

This document details the architectural plan to remove the multi-profile subsystem, delete `tahuti grades`, clean up credential path resolvers, and consolidate task commands.

---

## 1. Context & Motivation

1. **Multi-profile is unnecessary (YAGNI):** ManageBac is used by high school students and parents. 99.9% of users attend one school and have one account. The existing multi-profile machinery (`--profile`, `profiles.<name>`, `creds.<profile>.json`, profile merging, active profile pointers, and `purge_profiles`) adds substantial code and cognitive overhead for no real-world gain. If a user needs to test or manage multiple accounts, standard Unix environment isolation (`HOME=/path tahuti ...` or `MANAGEBAC_CONFIG=...`) already provides clean separation.
2. **`tahuti grades` is redundant:** `tahuti list` already crawls `/student/classes/<id>/core_tasks` for every active class and extracts task scores and letter grades. ManageBac does not calculate or expose an official expected term grade to student web clients; `tahuti`'s `_compute_expected_grade` was an unweighted heuristic averaging Highcharts point arrays. Individual task grades are already visible in `tahuti list` and `tahuti view`.
3. **Stale credential helper clutter:** Functions like `creds_filename()`, `default_creds_path()`, `legacy_creds_path()`, `creds_paths()`, and `all_creds_paths()` were created solely to support `creds.<profile>.json`. They can be eliminated in favor of a single direct credential path (`~/.config/tahuti/creds.json`).

---

## 2. Deletion of `tahuti grades`

### 2.1 CLI Layer (`src/tahuti/__main__.py`)
- Remove `grades` subparser and `cmd_grades`.
- Remove `grades` from `build_parser()` choices.

### 2.2 Client Layer (`src/tahuti/client.py`)
- Delete `_compute_expected_grade()`.
- Delete `_coerce_chart_points()`.
- Remove category weights scraping (`# Category weights` and `# Grade scale`).
- Streamline `get_class_tasks(class_id)` to parse task cards directly from `/student/classes/{class_id}/core_tasks` without executing chart calculations.

### 2.3 MCP Server (`src/tahuti/mcp_server.py`)
- Remove `get_class_grades` tool.
- Update MCP instructions and documentation tool count (down to 12 tools).

### 2.4 Presentation Layer (`src/tahuti/formatters.py`)
- Remove `format_grades` and command handlers for `grades`, `grades.all`, and `grades.list`.

---

## 3. Credential Path Simplification

Delete the following stale helper functions from `src/tahuti/config.py`:
- `creds_filename(profile)`
- `default_creds_path(profile)`
- `legacy_creds_path()`
- `creds_paths(profile)`
- `all_creds_paths()`

Replace with a single, direct resolver:
```python
def resolve_creds_path(explicit: str | None = None) -> Path:
    """Resolve the path to creds.json (explicit arg, env var, or default)."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = env_value(CREDS_ENV, CREDS_ENV_LEGACY)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_CREDS_PATH  # ~/.config/tahuti/creds.json
```

---

## 4. Single-Account Config & Session Flattening

### 4.1 Schema Migration

#### `config.json` (Flat)
```json
{
  "version": 1,
  "school": "beijing101",
  "domain": "managebac.cn",
  "email": "student@example.com",
  "defaults": {
    "view": "all",
    "pages": 10,
    "subject": "",
    "details": false,
    "format": "pretty",
    "cache_ttl": 900
  }
}
```

#### `session.json` (Flat)
```json
{
  "version": 1,
  "school": "beijing101",
  "domain": "managebac.cn",
  "email": "student@example.com",
  "base_url": "https://beijing101.managebac.cn",
  "cookie": "...",
  "logged_in_at": "2026-09-21T18:00:00"
}
```

### 4.2 Code Implementation in `src/tahuti/config.py`
- `load_state()`:
  - Reads top-level `school`, `domain`, `email`, `defaults` from `config_data`.
  - Backward compatibility: If `school` is not at root, fall back to `config_data.get("profiles", {}).get("default", {})`.
  - Reads top-level `school`, `domain`, `email`, `base_url`, `cookie`, `logged_in_at` from `session_data`.
  - Sets `state.active_profile = "default"` to keep output JSON envelopes and formatters stable.
- `save_profile()`: Writes flat root keys; strips legacy `profiles` and `active_profile` keys.
- `save_session()`: Writes flat root keys; strips legacy `profiles` and `active_profile` keys.
- `clear_session()`: Unlinks `session.json` if it exists.
- `purge_profiles()`: Renamed/aliased to `purge_config()`. Wipes `school`, `domain`, `email`, `defaults`, and legacy keys from `config.json`. Returns `["default"]` if configured, `[]` otherwise.

### 4.3 CLI Changes (`src/tahuti/__main__.py`)
- Remove `--profile` from `add_common_auth_flags()`.
- Remove `--profile` and `--all` from `logout` subparser.
- `tahuti logout`: clears `session.json`.
- `tahuti logout --purge`: clears `session.json`, `creds.json`, and wipes school settings in `config.json`.

---

## 5. Task Command Consolidation (Issue 4)

Tahuti currently has 5 commands touching tasks (`list`, `view`, `submit`, `feedback`, `submissions`).
- **Phase 1 (Immediate):** Delete redundant `tahuti submissions` and merge feedback inspection into `tahuti view <task_id>`.
- **Phase 2 (Future):** Group commands cleanly under resource hierarchy (`tahuti task list`, `tahuti task view <id>`, `tahuti task submit <id> <file>`), keeping top-level shortcuts for frequent daily verbs (`tahuti list`, `tahuti view`, `tahuti submit`).
