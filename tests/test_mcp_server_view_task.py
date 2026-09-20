"""`view_task` id resolution: a bare task_id must reach a real task URL.

Lives apart from tests/test_mcp_server.py so this contract has one home. The
bug these guard against: the tool echoed the resolved id in its answer but fed
the *raw* argument to `get_task_detail`, so `view_task(task_id="1000099")`
handed the bare id to a call that only understands
`/student/classes/<cid>/core_tasks/<id>` and produced
`https://<school><id>` — a DNS failure, not a task.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tahuti.mcp_server import view_task

TASK_URL = "https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099"


@pytest.fixture()
def view_task_env(tmp_path):
    """Patch build_client and yield ``(state, client)`` sandboxed in tmp_path."""
    state = MagicMock()
    state.active_profile = "default"
    # The snapshot is read from beside the config file, so pointing config_path
    # into tmp_path is what keeps this test off the real ~/.config/tahuti.
    state.config_path = tmp_path / "config.json"
    client = MagicMock()
    client.domain = "managebac.cn"
    client.school = "myschool"
    client.student_name = "John"
    client.base = "https://myschool.managebac.cn"
    client.get_task_detail.return_value = {"description": "Task body"}
    with patch(
        "tahuti.mcp_server.build_client",
        return_value=(state, client, "test@example.com"),
    ):
        yield state, client


def write_snapshot(state, tasks: list[dict]) -> None:
    """Write a snapshot holding *tasks* beside the state's config file."""
    snapshot = {"upcoming": tasks, "past": [], "overdue": []}
    (state.config_path.parent / "snapshot.json").write_text(
        json.dumps(snapshot), encoding="utf-8"
    )


class TestViewTaskResolution:
    def test_bare_id_fetches_the_link_from_the_snapshot(self, view_task_env):
        state, client = view_task_env
        write_snapshot(state, [{"id": "1000099", "link": TASK_URL, "title": "HW"}])

        data = json.loads(view_task(task_id="1000099"))

        # The assertion that catches the defect: the fetch targets the task URL
        # the snapshot knows about, never the bare id the caller passed.
        assert client.get_task_detail.call_args.args[0] == TASK_URL
        assert data["task"]["id"] == "1000099"
        assert data["task"]["link"] == TASK_URL
        assert data["detail"]["description"] == "Task body"
        # A snapshot hit must not cost a crawl.
        client.find_task_by_id.assert_not_called()

    def test_bare_id_falls_back_to_a_crawl_bounded_by_pages(self, view_task_env):
        state, client = view_task_env
        client.find_task_by_id.return_value = {
            "id": "1000099",
            "link": TASK_URL,
            "title": "HW",
        }

        data = json.loads(view_task(task_id="1000099", pages=3))

        client.find_task_by_id.assert_called_once_with("1000099", max_pages=3)
        assert client.get_task_detail.call_args.args[0] == TASK_URL
        assert data["task"]["link"] == TASK_URL

    def test_non_dict_crawl_result_is_not_mistaken_for_a_task(self, view_task_env):
        state, client = view_task_env
        client.find_task_by_id.return_value = "1000099"

        data = json.loads(view_task(task_id="1000099"))

        assert "error" in data
        client.get_task_detail.assert_not_called()

    def test_crawl_result_without_a_link_skips_the_detail_fetch(self, view_task_env):
        state, client = view_task_env
        client.find_task_by_id.return_value = {"id": "1000099", "title": "HW"}

        data = json.loads(view_task(task_id="1000099"))

        client.get_task_detail.assert_not_called()
        assert data["task"]["id"] == "1000099"
        assert data["detail"] == {}

    def test_full_task_url_is_fetched_verbatim(self, view_task_env):
        state, client = view_task_env
        write_snapshot(state, [{"id": "1000099", "link": TASK_URL, "title": "HW"}])
        url = f"{TASK_URL}?tab=submissions"

        data = json.loads(view_task(task_url=url))

        # A URL target needs no resolution, so it must reach the client exactly
        # as given — query string included.
        client.get_task_detail.assert_called_once_with(url)
        client.find_task_by_id.assert_not_called()
        assert data["task"]["id"] == "1000099"
        assert data["detail"]["description"] == "Task body"

    def test_unresolvable_id_returns_an_actionable_error(self, view_task_env):
        state, client = view_task_env
        client.find_task_by_id.return_value = None

        data = json.loads(view_task(task_id="1234567"))

        assert "1234567" in data["error"]
        assert "task" not in data and "detail" not in data
        client.get_task_detail.assert_not_called()

    def test_detail_fetch_error_is_not_wrapped_in_a_success_envelope(
        self, view_task_env
    ):
        state, client = view_task_env
        write_snapshot(state, [{"id": "1000099", "link": TASK_URL, "title": "HW"}])
        # get_task_detail reports failure by returning this dict, not by raising.
        client.get_task_detail.return_value = {"error": "404 Not Found"}

        data = json.loads(view_task(task_id="1000099"))

        assert data == {"error": "404 Not Found"}
        assert "task" not in data


# ── One derivation, two surfaces ────────────────────────────────────────


class _ViewArgs:
    """The argparse namespace `cmd_view` receives, for a bare ``target``."""

    def __init__(self, target):
        self.target = target
        self.id = None
        self.url = None
        self.pages = None
        self.refresh = False
        self.subject = None
        self.output = None
        self.format = "json"


def _cli_verdict(target: str, tmp_path) -> str:
    """``"ok"`` or the error code ``mb view <target>`` answered with."""
    from tahuti.__main__ import cmd_view

    state = MagicMock()
    state.active_profile = "default"
    # `_snapshot_path` reads beside the config file, so this keeps the sandbox
    # off the operator's real ~/.config/tahuti.
    state.config_path = tmp_path / "config.json"
    client = MagicMock()
    client.get_task_detail.return_value = {"description": "Task body"}
    task = {"id": "1000099", "title": "HW", "link": TASK_URL}

    with (
        patch("tahuti.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch("tahuti.__main__.find_task_by_id", return_value=task),
        patch("tahuti.__main__.print_payload") as printed,
    ):
        rc = cmd_view(_ViewArgs(target))
    if rc == 0:
        return "ok"
    return printed.call_args[0][0]["error"]["code"]


def _mcp_verdict(view_task_env, target: str) -> str:
    """``"ok"`` or the error code ``view_task(task_url=...)`` answered with."""
    state, _client = view_task_env
    # A bare id has to be findable for the tool to get past resolution, which is
    # what makes an *accepted* bare id distinguishable from a refused one.
    write_snapshot(state, [{"id": "1000099", "link": TASK_URL, "title": "HW"}])
    data = json.loads(view_task(task_url=target))
    return "ok" if "task" in data else "error"


# Targets that name a task: a bare numeric id, and a URL carrying
# ``/core_tasks/<id>``.
ACCEPTED_TARGETS = ("1000099", TASK_URL, f"{TASK_URL}?tab=submissions")

# Targets that do not. The class URL is the one that mattered: it used to be
# read as task id ``1000014``.
REFUSED_TARGETS = (
    "",
    "   ",
    "not-a-task-id",
    "1000099/../../etc/passwd",
    "https://myschool.managebac.cn/student/classes/1000014",
    "https://myschool.managebac.cn/attachments/1/leak.pdf",
)


class TestBothSurfacesReadATaskIdTheSameWay:
    """``mb view`` and the MCP ``view_task`` must agree about what a target *is*.

    They used to each carry the rule, and disagreed: the CLI's gate
    (``startswith("http")`` OR ``"/core_tasks/" in target``) admitted every URL
    and then split it on a separator it did not contain, so a class URL became
    the whole string as the "id" and was fetched anyway; the MCP tool demanded
    the separator and refused.  Both now call ``client.task_id_from_target``, so
    the same target gets the same verdict from either entry point.
    """

    @pytest.mark.parametrize("target", ACCEPTED_TARGETS)
    def test_accepted_by_both(self, view_task_env, tmp_path, target):
        assert _cli_verdict(target, tmp_path) == "ok", f"CLI refused {target!r}"
        assert _mcp_verdict(view_task_env, target) == "ok", f"MCP refused {target!r}"

    @pytest.mark.parametrize("target", REFUSED_TARGETS)
    def test_refused_by_both(self, view_task_env, tmp_path, target):
        assert _cli_verdict(target, tmp_path) != "ok", f"CLI accepted {target!r}"
        assert _mcp_verdict(view_task_env, target) != "ok", f"MCP accepted {target!r}"
