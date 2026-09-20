"""Verification for the dead-constant / snapshot-path cleanup.

Deliberately *not* under ``tests/``: that tree is owned by other agents on this
branch, and this is a scratch check rather than a suite addition. Run it with:

    .venv/bin/python -m pytest verify_snapshot_paths.py -q -p no:randomly

It answers three questions the suite does not currently ask:

1. Do ``config.snapshot_path``, ``__main__._snapshot_path`` and
   ``mcp_server._snapshot_path_for`` resolve to the *same file* for both a
   default and a non-default profile?
2. Does ``own_state_refusal`` — the submit containment rule — protect exactly
   that file?  If the MCP read and the refusal disagreed about the filename,
   this is where it would show.
3. Is ``DEFAULT_SNAPSHOT_TTL`` gone, and does ``default_cache_ttl`` still reach
   a usable int when the config key is present but null?
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tahuti import config as cfg
from tahuti.config import AppState, ProfileConfig, SessionConfig

_MAIN = "tahuti.__main__"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point every path this package persists to at a throwaway home.

    Mirrors ``tests/conftest.py``'s autouse fixture, because this file lives
    outside ``tests/`` and so does not inherit it. Both kinds of path need
    redirecting: the env vars reach the ones resolved per call, and the
    ``setattr`` calls reach the ones bound at import time.
    """
    home = tmp_path / "home"
    config_dir = home / ".config" / "tahuti"
    config_dir.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var, name in (
        ("MANAGEBAC_CONFIG", "config.json"),
        ("MANAGEBAC_SESSION", "session.json"),
        ("MANAGEBAC_CREDS_PATH", "creds.json"),
    ):
        monkeypatch.setenv(var, str(config_dir / name))

    for target, value in (
        (f"{_MAIN}.DEFAULT_SNAPSHOT_PATH", config_dir / "snapshot.json"),
        ("tahuti.daemon.DEFAULT_SNAPSHOT_PATH", config_dir / "snapshot.json"),
    ):
        monkeypatch.setattr(target, value)

    return config_dir


def _state(config_path: Path) -> AppState:
    return AppState(
        config_path=config_path,
        session_path=config_path.parent / "session.json",
        active_profile="default",
        profile=ProfileConfig(name="default"),
        session=SessionConfig(name="default"),
    )


def _write_config(path: Path, defaults: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"profiles": {"default": {"defaults": defaults}}}),
        encoding="utf-8",
    )


# ── 1. The three derivations agree ───────────────────────────────────


@pytest.mark.parametrize("relocated", [False, True], ids=["default", "non-default"])
def test_three_derivations_agree(sandbox, monkeypatch, relocated):
    from tahuti.__main__ import _snapshot_path
    from tahuti.mcp_server import _snapshot_path_for

    config_path = sandbox / "config.json"
    if relocated:
        # `MB_CRAWLER_CONFIG` moves the whole set; the snapshot must follow.
        elsewhere = sandbox.parent / "elsewhere"
        elsewhere.mkdir(parents=True, exist_ok=True)
        config_path = elsewhere / "config.json"
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))

    state = _state(config_path)
    expected = config_path.parent / cfg.SNAPSHOT_FILENAME

    assert cfg.snapshot_path(state) == expected
    assert _snapshot_path(state) == expected
    assert _snapshot_path_for(state) == expected


def test_default_snapshot_path_constants_use_the_filename(sandbox):
    """Both import-time spellings must equal config_dir()/SNAPSHOT_FILENAME."""
    import tahuti.daemon as daemon
    from tahuti.__main__ import DEFAULT_SNAPSHOT_PATH

    assert DEFAULT_SNAPSHOT_PATH == cfg.config_dir() / cfg.SNAPSHOT_FILENAME
    assert daemon.DEFAULT_SNAPSHOT_PATH == cfg.config_dir() / cfg.SNAPSHOT_FILENAME


# ── 2. The containment rule protects the file the MCP server reads ────


@pytest.mark.parametrize("relocated", [False, True], ids=["default", "non-default"])
def test_containment_refuses_exactly_the_mcp_read_path(sandbox, monkeypatch, relocated):
    from tahuti.mcp_server import _snapshot_path_for

    config_path = sandbox / "config.json"
    if relocated:
        elsewhere = sandbox.parent / "elsewhere"
        elsewhere.mkdir(parents=True, exist_ok=True)
        config_path = elsewhere / "config.json"
        monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))

    read_path = _snapshot_path_for(_state(config_path))

    refusal = cfg.own_state_refusal(read_path)
    assert refusal is not None, (
        f"{read_path} is read by the MCP server but is not tahuti's own state"
    )
    assert "task snapshot" in refusal


def test_a_file_outside_the_config_dir_is_not_refused(sandbox):
    """The rule must not swallow every file on the system.

    Note it *does* refuse any file inside ``config_dir()``, by design — see the
    scope comment above ``own_state_locations``. So the negative control has to
    live outside it.
    """
    elsewhere = sandbox.parent / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    neighbour = elsewhere / "homework.pdf"
    neighbour.write_text("x", encoding="utf-8")
    assert cfg.own_state_refusal(neighbour) is None


def test_renaming_the_filename_moves_the_read_and_the_refusal_together(sandbox, monkeypatch):
    """The test that actually pins the bug this branch exists to remove.

    Comparing today's three derivations proves nothing: the hardcoded literal
    happened to equal the constant, so they agreed by coincidence. The defect
    only bites when the *name changes*, so that is what is simulated here —
    ``SNAPSHOT_FILENAME`` is monkeypatched and the containment rule is asked
    about whatever the MCP server would now read.

    With the old code this failed: ``own_state_locations`` derived its entry
    from the constant and so protected ``<dir>/tasks-cache.json``, while
    ``mcp_server._snapshot_path_for`` still spelled ``snapshot.json`` by hand
    and read a file the containment rule knew nothing about.
    """
    from tahuti.__main__ import _snapshot_path
    from tahuti.mcp_server import _snapshot_path_for

    monkeypatch.setattr(cfg, "SNAPSHOT_FILENAME", "tasks-cache.json")
    state = _state(sandbox / "config.json")

    read_path = _snapshot_path_for(state)
    assert read_path.name == "tasks-cache.json", (
        "the MCP read did not follow the constant — it still spells its own name"
    )

    # ...and the containment rule must protect that very same file.
    assert _snapshot_path(state) == read_path
    assert cfg.snapshot_path(state) == read_path
    refusal = cfg.own_state_refusal(read_path)
    assert refusal is not None, (
        f"{read_path} is read by the MCP server but is no longer tahuti's own "
        "state — the containment rule and the read have diverged"
    )
    assert "task snapshot" in refusal


# ── 3. The dead constant is gone, and the TTL still resolves ──────────


def test_default_snapshot_ttl_is_gone(sandbox):
    import tahuti.__main__ as main

    assert not hasattr(main, "DEFAULT_SNAPSHOT_TTL")


@pytest.mark.parametrize(
    "cache_ttl_value",
    [None, "not-a-number", True],
    ids=["null", "garbage", "bool"],
)
def test_null_cache_ttl_still_yields_a_usable_int(sandbox, monkeypatch, cache_ttl_value):
    """The branch that DEFAULT_SNAPSHOT_TTL used to guard must stay unreachable."""
    config_path = sandbox / "config.json"
    _write_config(config_path, {"cache_ttl": cache_ttl_value})
    monkeypatch.setenv("MANAGEBAC_CONFIG", str(config_path))

    state = cfg.load_state(config_path)
    assert isinstance(state.profile.default_cache_ttl, int)
    assert state.profile.default_cache_ttl == cfg.DEFAULT_CACHE_TTL


def test_third_spelling_of_900_is_not_reintroduced():
    """config.DEFAULT_CACHE_TTL, cache.DEFAULT_TTL and the literal stay the only ones."""
    import tahuti.cache as cache

    assert cfg.DEFAULT_CACHE_TTL == cache.DEFAULT_TTL == 900
    assert cfg.SNAPSHOT_FILENAME == "snapshot.json"
