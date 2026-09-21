"""Tests for the CLI defects the publish-prep audit found by execution.

Each test names the command it covers and the symptom it used to produce.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from tahuti import __version__
from tahuti.__main__ import build_parser, main


# ── `tahuti --version` ───────────────────────────────────────────────────


def test_version_flag_exits_zero_with_package_version(capsys):
    """`tahuti --version` used to die with argparse's exit 2 (required subparsers)."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert out.startswith("tahuti ")


def test_version_short_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["-V"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_version_is_sourced_from_tahuti_version():
    """Guards against the string drifting from `tahuti.__version__`."""
    parser = build_parser()
    action = next(
        a for a in parser._actions if getattr(a, "dest", None) == "version"
    )
    assert __version__ in action.version


def test_pyproject_version_matches_tahuti_version():
    """`pyproject.toml` and `tahuti.__version__` must agree.

    They are the two places a version lives, and a release that updates one and
    not the other builds an artifact whose metadata and runtime disagree — the
    sdist is stamped from pyproject while `tahuti --version` reads the module.
    Read with a regex rather than `tomllib`, which is 3.11+ and the floor is
    3.10.
    """
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    match = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M
    )
    assert match, "could not read [project].version from pyproject.toml"
    assert match.group(1) == __version__


# ── `tahuti view --feedback` ──────────────────────────────────────────────


def _feedback_dict(items):
    return {
        "task_id": "123",
        "class_id": "456",
        "task_url": "http://x/123",
        "grade": {"grade_letter": "A", "grade_score": "9/10"},
        "general_comments": ["Nice work"],
        "feedback_items": items,
    }


def _feedback_item(name, comment="ok"):
    return {
        "submission_name": name,
        "feedback_url": None,
        "comment": comment,
        "rubric": [],
        "attachments": [{"name": f"{name}.annotated.pdf"}],
        "annotated_download_url": None,
        "error": None,
    }


def test_view_with_feedback(capsys):
    from tahuti.__main__ import cmd_view

    items = [_feedback_item("essay.pdf"), _feedback_item("quiz.pdf")]
    client = MagicMock()
    client.get_teacher_feedback.return_value = _feedback_dict(items)
    client.find_task_by_id.return_value = {"id": "123", "link": "http://x/classes/456/core_tasks/123"}
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__._resolve_task_ids", return_value=("456", "123")),
        patch("tahuti.__main__.load_snapshot", return_value={"upcoming": [], "past": [], "overdue": []}),
    ):
        rc = cmd_view(_ViewArgs(target="123", feedback=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["feedback"] == _feedback_dict(items)


def test_view_without_feedback(capsys):
    from tahuti.__main__ import cmd_view

    client = MagicMock()
    client.find_task_by_id.return_value = {"id": "123", "link": "http://x/classes/456/core_tasks/123"}
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={"upcoming": [], "past": [], "overdue": []}),
    ):
        rc = cmd_view(_ViewArgs(target="123", feedback=False))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "feedback" not in payload["data"]


# ── `tahuti submit --id` ─────────────────────────────────────────────────


def test_submit_accepts_id_instead_of_positional(capsys):
    from tahuti.__main__ import cmd_submit

    class Args:
        target = None
        id = "1000026"
        file = "hw.pdf"
        pages = 10
        output = None
        format = None

    client = MagicMock()
    client.submit_file.return_value = {"filename": "hw.pdf", "ok": True}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__._resolve_task_ids", return_value=("456", "1000026")),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch("tahuti.__main__.find_task_by_id", return_value=None),
    ):
        rc = cmd_submit(Args())

    assert rc == 0
    client.submit_file.assert_called_once_with("456", "1000026", "hw.pdf")


# ── `tahuti view --subject` ──────────────────────────────────────────────


class _ViewArgs:
    def __init__(self, **overrides):
        self.target = None
        self.id = None
        self.url = None
        self.subject = None
        self.pages = 10
        self.refresh = False
        self.output = None
        self.format = None
        for key, value in overrides.items():
            setattr(self, key, value)


def test_view_subject_mismatch_is_reported(capsys):
    from tahuti.__main__ import cmd_view

    client = MagicMock()
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch("tahuti.__main__.find_task_by_id", return_value={
            "id": "123", "title": "Essay", "class_name": "Physics", "link": "http://x/123"
        }),
    ):
        rc = cmd_view(_ViewArgs(id="123", subject="Math", format="json"))

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "subject_mismatch"


def test_view_subject_match_passes_through(capsys):
    from tahuti.__main__ import cmd_view

    client = MagicMock()
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch("tahuti.__main__.find_task_by_id", return_value={
            "id": "123", "title": "Essay", "class_name": "Mathematics HL", "link": "http://x/123"
        }),
    ):
        rc = cmd_view(_ViewArgs(id="123", subject="Math", format="json"))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_view_without_subject_ignores_the_check(capsys):
    from tahuti.__main__ import cmd_view

    client = MagicMock()
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch("tahuti.__main__.find_task_by_id", return_value={
            "id": "123", "title": "Essay", "class_name": "Physics", "link": "http://x/123"
        }),
    ):
        rc = cmd_view(_ViewArgs(id="123"))

    assert rc == 0


# ── `tahuti notifications --unread-only` ─────────────────────────────────


def test_notifications_unread_only_filters_the_request():
    from tahuti.__main__ import cmd_notifications

    class Args:
        page = 1
        per_page = 20
        read = None
        unread = None
        read_all = False
        unread_only = True
        output = None
        format = None

    hub = MagicMock()
    hub.stats.return_value = {"unread": 3}
    hub.list.return_value = {"items": [], "meta": {}}
    client = MagicMock()
    client.get_notification_token.return_value = ("https://hub.example", "tok")
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.hub_client", return_value=hub),
    ):
        rc = cmd_notifications(Args())

    assert rc == 0
    hub.list.assert_called_once_with(page=1, per_page=20, filter_="unread")


def test_notifications_defaults_to_all_filter():
    from tahuti.__main__ import cmd_notifications

    class Args:
        page = 1
        per_page = 20
        read = None
        unread = None
        read_all = False
        unread_only = False
        output = None
        format = None

    hub = MagicMock()
    hub.stats.return_value = {}
    hub.list.return_value = {"items": [], "meta": {}}
    client = MagicMock()
    client.get_notification_token.return_value = ("https://hub.example", "tok")
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.hub_client", return_value=hub),
    ):
        cmd_notifications(Args())

    hub.list.assert_called_once_with(page=1, per_page=20, filter_="all")


def test_notifications_accepts_unread_only_flag():
    args = build_parser().parse_args(["notifications", "--unread-only"])
    assert args.unread_only is True
