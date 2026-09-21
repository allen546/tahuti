"""Shared fixtures for tahuti tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Global user-state isolation ──────────────────────────────────────────
#
# Running this suite used to be able to destroy the operator's live
# credentials. `test_main.TestMainLogout.test_logout` redirected
# MB_CRAWLER_CONFIG and MB_CRAWLER_SESSION into a tmp_path but not
# MB_CRAWLER_CREDS_PATH, so `cmd_logout`'s `resolve_creds_path()` call fell
# through to the real ~/.config/tahuti/creds.json and `clear_creds()` unlinked
# the operator's ManageBac password. A test run must never be able to reach
# the real ~/.config/tahuti/, so every test gets the isolation below whether it
# asks for it or not.
#
# Two kinds of path need redirecting, and the environment alone reaches only
# one of them:
#
# 1. Read at *call* time by `config.resolve_*_path()`. Setting the variable is
#    sufficient.
# 2. Bound at *import* time. `DEFAULT_CACHE_DIR`, `DEFAULT_SNAPSHOT_PATH`,
#    `DEFAULT_STATE_PATH`, `DEFAULT_PID_PATH`, `DEFAULT_LOG_PATH` and
#    `DEFAULT_DAEMON_PATH` are module-level constants computed from
#    `config_dir()` when their module is first imported. No environment
#    variable can reach them, so each one is patched in place. Note that
#    `daemon/__init__.py` does `from .system import DEFAULT_PID_PATH`, which
#    creates a *second*, independent binding for the same value — both
#    namespaces are listed, or half the daemon paths would stay pointed at the
#    operator's home directory.
#
# The redirect target keeps the real on-disk layout (`~/.config/tahuti/...`)
# and `HOME` is pointed at the same tmp_path, so tests that legitimately
# assert "this default lives under the user's config directory" keep passing
# against the sandbox instead of having to be weakened into tautologies.

# Captured at import time, before any fixture can redirect it. Read-only: this
# is the location the tests below must prove they never touch, so it is
# deliberately never stat'd, written, or unlinked.
_REAL_HOME = Path.home()
REAL_CONFIG_DIR = _REAL_HOME / ".config" / "tahuti"

# (env var, filename under the redirected config dir) — read at call time.
_CALL_TIME_PATH_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("MANAGEBAC_CONFIG", "config.json"),
    ("MANAGEBAC_SESSION", "session.json"),
    ("MANAGEBAC_CREDS_PATH", "creds.json"),
)

# Credential/behaviour switches that a developer's shell may leak into the
# suite. Cleared rather than set, so tests that want one can opt back in. Both
# spellings, because the pre-rename `MB_CRAWLER_*` names still work as
# deprecated fallbacks — a leaked one would reach the code just as well as a
# leaked new one.
_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "MANAGEBAC_PASSWORD",
    "MANAGEBAC_COOKIE",
    "MANAGEBAC_KEYCHAIN",
    "MANAGEBAC_NO_PERM_WARN",
    "MB_CRAWLER_PASSWORD",
    "MB_CRAWLER_COOKIE",
    "MB_CRAWLER_KEYCHAIN",
    "MB_CRAWLER_NO_PERM_WARN",
)


def _redirected_paths(config_dir: Path) -> dict[str, Path]:
    """Every import-time path constant, mapped to its sandboxed value."""
    return {
        "tahuti.config.CONFIG_DIR": config_dir,
        "tahuti.config.DEFAULT_CONFIG_PATH": config_dir / "config.json",
        "tahuti.config.DEFAULT_SESSION_PATH": config_dir / "session.json",
        "tahuti.config.DEFAULT_CREDS_PATH": config_dir / "creds.json",
        "tahuti.cache.DEFAULT_CACHE_DIR": config_dir / "cache",
        "tahuti.__main__.DEFAULT_SNAPSHOT_PATH": config_dir / "snapshot.json",
        "tahuti.daemon.DEFAULT_DAEMON_PATH": config_dir / "daemon.json",
        "tahuti.daemon.DEFAULT_SNAPSHOT_PATH": config_dir / "snapshot.json",
        "tahuti.daemon.state.DEFAULT_STATE_PATH": config_dir / "daemon_state.json",
        "tahuti.daemon.system.DEFAULT_PID_PATH": config_dir / "daemon.pid",
        "tahuti.daemon.system.DEFAULT_LOG_PATH": config_dir / "daemon.log",
        # `daemon/__init__.py` re-exports these two under its own names.
        "tahuti.daemon.DEFAULT_PID_PATH": config_dir / "daemon.pid",
        "tahuti.daemon.DEFAULT_LOG_PATH": config_dir / "daemon.log",
    }


@pytest.fixture()
def real_user_config_dir() -> Path:
    """The operator's real config dir, for asserting a test never touched it."""
    return REAL_CONFIG_DIR


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path: Path, monkeypatch):
    """Sandbox every path this package persists to, for every test.

    Autouse and unconditional so no test can forget it and no test can opt out
    by omission. A test that *wants* a different location can still have one:
    its own ``monkeypatch.setenv`` / ``setattr`` runs after this fixture, so a
    per-test override always wins over the sandbox default.
    """
    config_dir = tmp_path / ".config" / "tahuti"
    config_dir.mkdir(parents=True, exist_ok=True)

    for var, filename in _CALL_TIME_PATH_ENV_VARS:
        monkeypatch.setenv(var, str(config_dir / filename))
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    for target, value in _redirected_paths(config_dir).items():
        # Dotted-string form: imports the module on demand and fails loudly if
        # a constant is ever renamed, instead of silently leaving it live.
        monkeypatch.setattr(target, value)

    # Redirecting HOME catches the `Path.home()` calls that no constant covers
    # — the launchd plist and the systemd user unit in daemon/system.py — and
    # keeps `Path.home() / ".config" / "tahuti"` agreeing with the constants
    # above. Windows resolves the profile from these instead of HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # No test may reach a real OS credential store. `keychain.available()` is
    # the fallback `_load_creds()` consults when creds.json is missing, so a
    # helper that happens to be installed on the dev box would otherwise be
    # queried for the operator's password.
    monkeypatch.setattr("tahuti.keychain._tool", lambda: None)

    yield config_dir


@pytest.fixture()
def tmp_config_dir(tmp_path: Path):
    """Return a temporary config directory."""
    return tmp_path / "config"




@pytest.fixture()
def make_crawl_result():
    """Factory fixture that creates a crawl_all()-style result dict."""

    def _make(
        upcoming=None,
        past=None,
        overdue=None,
        student_name="Test Student",
        school="myschool",
        base_url="https://myschool.managebac.cn",
    ):
        upcoming = upcoming or []
        past = past or []
        overdue = overdue or []
        return {
            "student_name": student_name,
            "school": school,
            "base_url": base_url,
            "crawled_at": "2026-04-29T12:00:00",
            "upcoming": upcoming,
            "past": past,
            "overdue": overdue,
            "summary": {
                "upcoming_count": len(upcoming),
                "past_count": len(past),
                "overdue_count": len(overdue),
            },
        }

    return _make


@pytest.fixture()
def sample_task():
    """Return a sample task dict."""
    return {
        "id": "1000026",
        "title": "Homework 3",
        "link": "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026",
        "due_date": "Apr 15",
        "class_name": "Math HL",
        "labels": ["Homework"],
        "grade_letter": "A",
        "grade_score": "95/100",
        "view": "upcoming",
    }
