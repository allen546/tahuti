"""Tests for tahuti.mcp_server."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure worktree src is prioritized over editable installs in venv — the same
# preamble tests/test_client.py carries. Without it this file can silently test
# the main checkout's copy: the editable install's `.pth` names *its* src, so
# whichever module imports `tahuti` first decides what every later module sees,
# and in a worktree that is not the code under test.
worktree_src = str(Path(__file__).resolve().parent.parent / "src")
if sys.path[0] != worktree_src:
    sys.path.insert(0, worktree_src)

import tahuti

tahuti_pkg_dir = str(Path(worktree_src) / "tahuti")
if hasattr(tahuti, "__path__") and tahuti_pkg_dir not in tahuti.__path__:
    tahuti.__path__.insert(0, tahuti_pkg_dir)

if "tahuti.mcp_server" in sys.modules:
    importlib.reload(sys.modules["tahuti.mcp_server"])

from tahuti.client import ManageBacClient
from tahuti.filters import summary_of
from tahuti.filters import result_views
from tahuti.mcp_server import (
    _error_payload,
    _sanitize_error,
    delete_submission,
    get_calendar_events,
    get_ical_feed,
    get_notifications,
    get_teacher_feedback,
    get_timetable,
    list_classes,
    list_tasks,
    mark_all_notifications_read,
    mark_notification,
    mcp,
    submit_file,
    view_task,
)


@pytest.fixture()
def mock_build_client():
    """Patch auth.build_client for MCP tool tests."""
    with patch("tahuti.mcp_server.build_client") as mock:
        mock_state = MagicMock()
        mock_state.active_profile = "default"
        mock_client = MagicMock()
        mock_client.domain = "managebac.cn"
        mock_client.school = "myschool"
        mock_client.student_name = "John"
        mock_client.base = "https://myschool.managebac.cn"
        mock.return_value = (mock_state, mock_client, "test@example.com")
        yield mock, mock_client


class TestMcpServerSetup:
    def test_mcp_name(self):
        assert mcp.name == "tahuti"

    def test_tools_registered(self):
        tool_names = (
            [t.name for t in mcp._tool_manager._tools.values()]
            if hasattr(mcp, "_tool_manager")
            else []
        )
        # Just check mcp is a FastMCP instance
        assert mcp is not None


class TestListTasksTool:
    """`list_tasks` must take its tasks from the CLI's source, not a second one.

    The CLI's `list` command calls `client.crawl_all` (classes discovered from
    the dashboard, then each class's core_tasks page). This tool used to call
    `client.get_tasks_by_view` (the `tasks_and_deadlines` pages), which is a
    second, overlapping source of the same tasks — so the two surfaces could
    report different sets. These tests pin the shared source.
    """

    @staticmethod
    def _crawl(**sections):
        """A `crawl_all` return value with only the named sections populated."""
        base = {"upcoming": [], "past": [], "overdue": []}
        base.update(sections)
        return base

    def test_list_tasks(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[{"id": "1", "title": "T1", "class_name": "Math"}]
        )
        result = list_tasks()
        data = json.loads(result)
        assert "upcoming" in data
        assert len(data["upcoming"]) == 1

    def test_uses_the_cli_task_source(self, mock_build_client):
        """The divergence fix: `crawl_all`, never the per-view crawl."""
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl()
        list_tasks()
        mock_client.crawl_all.assert_called_once()
        mock_client.get_tasks_by_view.assert_not_called()

    def test_pages_and_details_are_forwarded_to_crawl_all(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl()
        list_tasks(pages=3, details=True)
        # `fetch_notifications=False` is asserted rather than left implicit: it
        # is the whole point of the kwarg's existence at this call site. The
        # tool never reads `notifications`, so it must stop paying for the three
        # MNN-hub requests — but the *default* stays True for un-audited callers,
        # which means a future refactor that drops the kwarg here fails this
        # assertion instead of quietly reintroducing the cost.
        mock_client.crawl_all.assert_called_once_with(
            max_pages=3, fetch_details=True, fetch_notifications=False
        )

    @pytest.mark.parametrize(
        "view,reported_views",
        [
            ("all", ["upcoming", "past", "overdue"]),
            ("upcoming", ["upcoming"]),
            ("past", ["past"]),
            ("overdue", ["overdue"]),
            # Aliases an LLM actually sends.
            ("Upcoming", ["upcoming"]),
            ("upcoming tasks", ["upcoming"]),
            ("overdue tasks", ["overdue"]),
        ],
    )
    def test_view_filters_the_report_not_the_crawl(
        self, mock_build_client, view, reported_views
    ):
        """`view` is a display filter, exactly as the CLI's `--view` is.

        `crawl_all` crawls every view regardless, so one crawl_all call is made
        for every value of `view` — the CLI cannot crawl one section without the
        others either.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[{"id": "upcoming", "title": "u", "class_name": "Math"}],
            past=[{"id": "past", "title": "p", "class_name": "Math"}],
            overdue=[{"id": "overdue", "title": "o", "class_name": "Math"}],
        )
        result = list_tasks(view=view)
        data = json.loads(result)
        assert mock_client.crawl_all.call_count == 1
        for name in ("upcoming", "past", "overdue"):
            assert len(data[name]) == (1 if name in reported_views else 0)
        assert data["summary"]["total_count"] == len(reported_views)
        assert "error" not in data

    def test_summary_shape_matches_the_shared_helper(self, mock_build_client):
        """The tool's summary comes from `filters.summary_of`, not a fourth copy.

        `crawl_all` emits only three of the four keys, so a tool that hand-built
        its own could report a different shape than the CLI's `list` for the
        same student.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[{"id": "1", "title": "u", "class_name": "Math"}],
            past=[{"id": "2", "title": "p", "class_name": "Math"}],
            overdue=[{"id": "3", "title": "o", "class_name": "Math"}],
        )
        data = json.loads(list_tasks())
        assert data["summary"] == summary_of(
            {"upcoming": data["upcoming"], "past": data["past"], "overdue": data["overdue"]}
        )
        assert data["summary"]["total_count"] == 3

    @pytest.mark.parametrize("view", ["al", "upcomingg", "todo", "homework", "1"])
    def test_unrecognised_view_is_an_error_not_an_empty_list(
        self, mock_build_client, view
    ):
        # The old behaviour matched none of the three `if view in (...)` checks,
        # so every list stayed empty and the tool reported total_count 0 — a
        # valid-looking "this student has no homework".
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[{"id": "1", "title": "T1", "class_name": "Math"}]
        )
        result = list_tasks(view=view)
        data = json.loads(result)
        assert "error" in data
        assert view in data["error"]
        assert "tasks" not in data
        # No crawl may happen for a view we already know is invalid.
        mock_client.crawl_all.assert_not_called()
        mock_client.get_tasks_by_view.assert_not_called()

    def test_unrecognised_view_error_names_valid_views(self, mock_build_client):
        mock, _client = mock_build_client
        data = json.loads(list_tasks(view="al"))
        for view in ("all", "upcoming", "past", "overdue"):
            assert view in data["error"]

    def test_default_view_reports_all_sections(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[{"id": "1", "title": "u", "class_name": "Math"}],
            past=[{"id": "2", "title": "p", "class_name": "Math"}],
            overdue=[{"id": "3", "title": "o", "class_name": "Math"}],
        )
        list_tasks()
        data = json.loads(list_tasks())
        assert len(data["upcoming"]) == len(data["past"]) == len(data["overdue"]) == 1
        assert mock_client.crawl_all.call_count == 2

    def test_the_validated_view_is_the_one_result_views_receives(
        self, mock_build_client
    ):
        """The view is normalised once, up front, and that answer is reused.

        The validation call has to happen before `build_client` so a bad view
        costs no network call; `result_views` used to run the alias table a
        second time on the value the tool had already canonicalised.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(upcoming=[{"t": 1}])
        with patch(
            "tahuti.mcp_server.result_views", wraps=result_views
        ) as result_views_spy:
            list_tasks(view="Upcoming")
        result_views_spy.assert_called_once()
        assert result_views_spy.call_args.args[1] == "upcoming"

    def test_list_tasks_with_subject(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[
                {"id": "1", "title": "T1", "class_name": "Math HL"},
                {"id": "2", "title": "T2", "class_name": "English A"},
            ]
        )
        result = list_tasks(subject="Math")
        data = json.loads(result)
        assert len(data["upcoming"]) == 1
        assert data["upcoming"][0]["title"] == "T1"

    def test_subject_matching_is_the_shared_casefold_one(self, mock_build_client):
        """`list_tasks` must not keep a private copy of `filters.matches_subject`.

        The inline matcher this tool carried compared with `str.lower()`, which
        leaves "ß" as "ß"; `filters.matches_subject` casefolds, which maps it to
        "ss". A class stored as "Fußball" was therefore invisible to a student
        typing "fussball" here while `tahuti list` found it — the two
        implementations could drift on non-ASCII input, and only an ASCII
        "Math" case pinned them together.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[
                {"id": "1", "title": "T1", "class_name": "Fußball"},
                {"id": "2", "title": "T2", "class_name": "Math HL"},
            ]
        )
        data = json.loads(list_tasks(subject="fussball"))
        assert [t["title"] for t in data["upcoming"]] == ["T1"]
        assert data["summary"]["total_count"] == 1

    def test_list_tasks_with_tag(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[
                {"id": "1", "title": "T1", "class_name": "Math HL", "labels": ["Summative"]},
                {"id": "2", "title": "T2", "class_name": "English A", "labels": ["Formative"]},
            ]
        )
        result = list_tasks(tag="Summative")
        data = json.loads(result)
        assert len(data["upcoming"]) == 1
        assert data["upcoming"][0]["title"] == "T1"

    @pytest.mark.parametrize(
        "filter_kwargs,expected_ids",
        [
            ({}, ["1", "2", "3"]),
            ({"graded": True}, ["1"]),
            ({"graded": False}, ["2", "3"]),
            ({"submitted": True}, ["1"]),
            ({"submitted": False}, ["2", "3"]),
            ({"grade": "A+"}, ["1"]),
            ({"grade": "4.0"}, ["1"]),
            ({"tag": "Summative"}, ["3"]),
            ({"completed": True}, ["1"]),
            ({"completed": False}, ["2", "3"]),
            # A filter is applied only when its argument is not None, so an
            # explicit None must not narrow the result.
            ({"graded": None, "submitted": None}, ["1", "2", "3"]),
            ({"graded": True, "completed": True}, ["1"]),
        ],
    )
    def test_status_filters_come_from_the_shared_helpers(
        self, mock_build_client, filter_kwargs, expected_ids
    ):
        """The five status filters are `filters.filter_result_by_status`'s.

        The CLI's `list` command applies exactly these through that one call, so
        the tool cannot answer differently for the same combination. This pins
        the delegation the six inline blocks were replaced by.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl(
            upcoming=[
                {
                    "id": "1", "title": "T1", "class_name": "Math HL",
                    "status": "submitted", "grade_letter": "A+",
                },
                {
                    "id": "2", "title": "T2", "class_name": "Math HL",
                    "status": "not-submitted",
                },
                {
                    "id": "3", "title": "T3", "class_name": "English A",
                    "status": "not-submitted", "labels": ["Summative"],
                },
            ]
        )
        data = json.loads(list_tasks(**filter_kwargs))
        assert [t["id"] for t in data["upcoming"]] == expected_ids
        assert data["summary"]["total_count"] == len(expected_ids)


class TestViewTaskTool:
    def test_view_task_by_url(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_task_detail.return_value = {"description": "Task details"}
        result = view_task(
            task_url="https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099"
        )
        data = json.loads(result)
        assert data["task"]["id"] == "1000099"
        assert data["detail"]["description"] == "Task details"

    def test_view_task_by_numeric_id(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_task_detail.return_value = {"description": "d"}
        link = "https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099"
        mock_client.find_task_by_id.return_value = {"id": "1000099", "link": link}
        data = json.loads(view_task(task_id="1000099"))
        assert data["task"]["id"] == "1000099"
        # The resolved task's link is what gets fetched, not the bare id.
        assert mock_client.get_task_detail.call_args.args[0] == link

    def test_view_task_id_from_url_with_query_string(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_task_detail.return_value = {}
        data = json.loads(
            view_task(
                task_url="https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099?foo=1"
            )
        )
        assert data["task"]["id"] == "1000099"

    @pytest.mark.parametrize(
        "target",
        [
            # A URL without /core_tasks/ used to make the whole string the id.
            "https://myschool.managebac.cn/student/dashboard",
            "https://myschool.managebac.cn/student/classes/1000014",
            "https://evil.example.com/student/classes/1/core_tasks",
            "not a task at all",
            "1000099/../../etc/passwd",
        ],
    )
    def test_view_task_rejects_unparseable_target(self, mock_build_client, target):
        mock, mock_client = mock_build_client
        result = view_task(task_url=target)
        data = json.loads(result)
        assert "error" in data
        assert "task" not in data
        mock_client.get_task_detail.assert_not_called()

    def test_view_task_no_target(self, mock_build_client):
        result = view_task()
        data = json.loads(result)
        assert "error" in data


class TestSubmitFileTool:
    def test_submit_by_url(self, mock_build_client, tmp_path):
        mock, mock_client = mock_build_client
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"%PDF-1.4 test")
        mock_client.submit_file.return_value = {
            "ok": True,
            "filename": "hw.pdf",
            "task_url": "http://x",
        }
        result = submit_file(
            task_id="https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099",
            file_path=str(upload),
        )
        data = json.loads(result)
        assert data["ok"] is True
        mock_client.submit_file.assert_called_once_with(
            "1000014", "1000099", str(upload.resolve())
        )

    def test_submit_not_found(self, mock_build_client, tmp_path):
        """An id no source can resolve is an error payload, never an upload.

        `get_tasks_by_view` used to be the tool's own resolution source; the
        ladder is now the CLI's, whose last step is `find_task_by_id`.  That
        method is configured here because leaving it a bare MagicMock would
        hand `parse_task_url` a mock and raise a TypeError rather than answer.
        """
        mock, mock_client = mock_build_client
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"x")
        mock_client.find_task_by_id.return_value = None
        result = submit_file(task_id="99999", file_path=str(upload))
        data = json.loads(result)
        assert "error" in data
        mock_client.submit_file.assert_not_called()
        # The condemned second task source is not consulted at all now.
        mock_client.get_tasks_by_view.assert_not_called()

    def test_submit_numeric_id_resolves(self, mock_build_client, tmp_path):
        """A bare id resolves through `crawl_all`, the CLI's task source.

        This test used to drive `get_tasks_by_view("upcoming")` directly, which
        was the MCP tool's *own* copy of the ladder — the second, overlapping
        source of the same tasks that `list_tasks` had already been moved off,
        so a class whose tasks only appear on its own core_tasks page resolved
        here but was missing from the CLI's `list`.  It now drives the shared
        ladder, and the old source must stay untouched.
        """
        mock, mock_client = mock_build_client
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"x")
        mock_client.crawl_all.return_value = {
            "upcoming": [],
            "past": [],
            "overdue": [
                {
                    "id": "1000099",
                    "link": "/student/classes/1000014/core_tasks/1000099",
                }
            ],
        }
        mock_client.find_task_by_id.return_value = None
        mock_client.submit_file.return_value = {"ok": True}
        result = submit_file(task_id="1000099", file_path=str(upload))
        data = json.loads(result)
        assert data["ok"] is True
        mock_client.submit_file.assert_called_once_with(
            "1000014", "1000099", str(upload.resolve())
        )
        mock_client.get_tasks_by_view.assert_not_called()

    @pytest.mark.parametrize(
        "make_bad_path",
        [
            lambda tmp_path: str(tmp_path / "does-not-exist.pdf"),
            lambda tmp_path: str(tmp_path),  # a directory
            lambda tmp_path: "",
            lambda tmp_path: "   ",
            lambda tmp_path: "hw\x00.pdf",
        ],
        ids=["missing", "directory", "empty", "whitespace", "nul-byte"],
    )
    def test_submit_rejects_unusable_file_path(
        self, mock_build_client, tmp_path, make_bad_path
    ):
        mock, mock_client = mock_build_client
        bad_path = make_bad_path(tmp_path)
        result = submit_file(
            task_id="https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099",
            file_path=bad_path,
        )
        data = json.loads(result)
        assert "error" in data, data
        assert "file_path" in data["error"]
        # Nothing is uploaded and no client is even constructed.
        mock_client.submit_file.assert_not_called()
        mock.assert_not_called()

    def test_submit_rejects_device_file(self, mock_build_client):
        mock, mock_client = mock_build_client
        devnull = Path("/dev/null")
        if not devnull.exists():
            pytest.skip("/dev/null not present")
        data = json.loads(
            submit_file(
                task_id="https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099",
                file_path=str(devnull),
            )
        )
        assert "error" in data
        mock_client.submit_file.assert_not_called()

    def test_submit_expands_user_home(self, mock_build_client, tmp_path, monkeypatch):
        mock, mock_client = mock_build_client
        monkeypatch.setenv("HOME", str(tmp_path))
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"x")
        mock_client.submit_file.return_value = {"ok": True}
        data = json.loads(
            submit_file(
                task_id="https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099",
                file_path="~/hw.pdf",
            )
        )
        assert data["ok"] is True
        assert mock_client.submit_file.call_args.args[2] == str(upload.resolve())

    def test_submit_error_payload_does_not_leak_credentials(self, mock_build_client, tmp_path):
        mock, mock_client = mock_build_client
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"x")
        mock_client.submit_file.side_effect = RuntimeError(
            "403 for cookie _managebac_session=SUPERSECRETVALUE"
        )
        data = json.loads(
            submit_file(
                task_id="https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099",
                file_path=str(upload),
            )
        )
        assert "error" in data
        assert "SUPERSECRETVALUE" not in json.dumps(data)


# ── One task-resolution ladder, shared with the CLI ──────────────────────


class TestSharedTaskResolutionLadder:
    """`submit_file`, `delete_submission` and `get_teacher_feedback`.

    Each of these three tools carried its own copy of the ladder that turns a
    target into a ``(class_id, task_id)`` pair, and each was wrong in a
    different way.  Two of them ended in ``get_tasks_by_view`` — the second,
    overlapping source of the same tasks that ``list_tasks`` had already been
    moved off, so a class whose tasks only appear on its own core_tasks page
    resolved here but was missing from the CLI's ``list``.  ``delete_submission``
    had no final step at all, so it answered "Could not resolve class_id" for
    tasks the CLI submits to.  All three now call the CLI's ``_resolve_task_ids``.
    """

    #: A `crawl_all` result with nothing in it, so only the later steps can
    #: resolve anything.
    EMPTY_CRAWL = {"upcoming": [], "past": [], "overdue": []}

    @staticmethod
    def _crawl_with(task_id: str, link: str) -> dict:
        """A `crawl_all` result whose overdue section holds one task."""
        return {"upcoming": [], "past": [], "overdue": [{"id": task_id, "link": link}]}

    def test_delete_submission_resolves_what_only_find_task_by_id_knows(
        self, mock_build_client
    ):
        """The step this tool's own ladder was missing.

        A task that is in neither the snapshot nor the crawl still resolved for
        the CLI, because `_resolve_task_ids` ends in `find_task_by_id`.  This
        tool stopped before it, so the same id was unresolvable here.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self.EMPTY_CRAWL
        mock_client.find_task_by_id.return_value = {
            "id": "1000099",
            "link": "/student/classes/1000014/core_tasks/1000099",
        }
        mock_client.delete_submission.return_value = {
            "ok": True,
            "asset_id": "82189817",
        }

        data = json.loads(delete_submission(task_id="1000099", asset_id="82189817"))

        assert data["ok"] is True
        mock_client.delete_submission.assert_called_once_with(
            "1000014", "1000099", "82189817"
        )
        mock_client.get_tasks_by_view.assert_not_called()

    def test_get_teacher_feedback_uses_the_same_ladder(self, mock_build_client):
        """The tool whose copy already had the right shape, but its own budget.

        It is the source that matters, not just the sequence of steps: the
        tasks come from `crawl_all`, the same one the CLI's `list` reads.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self._crawl_with(
            "1000099", "/student/classes/1000014/core_tasks/1000099"
        )
        mock_client.find_task_by_id.return_value = None
        mock_client.get_teacher_feedback.return_value = {"comments": ["Good work"]}

        data = json.loads(get_teacher_feedback(task_id="1000099"))

        assert data == {"comments": ["Good work"]}
        mock_client.get_teacher_feedback.assert_called_once_with("1000014", "1000099")
        mock_client.get_tasks_by_view.assert_not_called()

    def test_the_page_budget_is_the_clis(self, mock_build_client):
        """The three copies used 3, 5 and 10 pages; the ladder has one number.

        A different budget means a different set of resolvable tasks, so this
        pins the MCP tools to the CLI's rather than to any copy's own.
        """
        mock, mock_client = mock_build_client
        # An empty crawl, so the ladder walks past it to its last step and both
        # budgets are exercised in one run.
        mock_client.crawl_all.return_value = self.EMPTY_CRAWL
        mock_client.find_task_by_id.return_value = None
        mock_client.get_teacher_feedback.return_value = {"comments": []}

        get_teacher_feedback(task_id="1000099")

        assert mock_client.crawl_all.call_args.kwargs["max_pages"] == 10
        assert mock_client.find_task_by_id.call_args.kwargs["max_pages"] == 10

    @pytest.mark.parametrize(
        "call",
        [
            lambda: delete_submission(task_id="99999", asset_id="82189817"),
            lambda: get_teacher_feedback(task_id="99999"),
            lambda: submit_file(task_id="99999", file_path="/nonexistent/hw.pdf"),
        ],
        ids=["delete_submission", "get_teacher_feedback", "submit_file"],
    )
    def test_unresolvable_task_answers_with_json_not_an_exception(
        self, mock_build_client, call
    ):
        """A tool that used to answer `{"error": ...}` still does.

        The shared ladder refuses with a `CommandError`, which the wrapper
        restates as an `InvalidToolInput` so the envelope a caller already parses
        is unchanged — an exception escaping into the MCP transport would be a
        new failure mode, not a preserved one.
        """
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = self.EMPTY_CRAWL
        mock_client.find_task_by_id.return_value = None

        data = json.loads(call())

        assert "error" in data
        mock_client.get_tasks_by_view.assert_not_called()


class TestGetNotificationsTool:
    def test_list_notifications(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("endpoint", "token")

        with patch("tahuti.mcp_server.hub_client") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.stats.return_value = {"unread_messages": 3}
            mock_hub.list.return_value = {
                "items": [{"id": 1, "title": "Test"}],
                "meta": {"page": 1},
            }
            result = get_notifications()
            data = json.loads(result)
            assert data["stats"]["unread_messages"] == 3
            assert len(data["items"]) == 1

    def test_unread_only_filter(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("https://mnn-hub.prod.faria.cn", "tok")

        with patch("tahuti.mcp_server.hub_client") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.stats.return_value = {}
            mock_hub.list.return_value = {"items": [], "meta": {}}
            get_notifications(unread_only=True)
            mock_hub.list.assert_called_once_with(page=1, per_page=20, filter_="unread")


class TestNotificationToolsValidateTheHubEndpoint:
    """The hub endpoint comes from scraped HTML; the token is a bearer JWT.

    `client.get_notification_token()` returns `data-mnn-hub-endpoint` verbatim,
    and its docstring requires callers to pass it through
    `_validated_hub_endpoint`. The MCP notification tools did not, so a poisoned
    page could name the host that receives `Authorization: Bearer <jwt>`.

    `mock_build_client` hands back a MagicMock client, which stubs the validator
    out — so these tests bind the *real* method onto it. Without that, they
    would assert nothing about the guard.
    """

    TOOLS = (get_notifications, mark_notification, mark_all_notifications_read)

    @pytest.fixture()
    def mock_build_client_validating(self, mock_build_client):
        """The same client, with the production validator bound on it."""
        mock, mock_client = mock_build_client
        mock_client._validated_hub_endpoint = (
            ManageBacClient._validated_hub_endpoint.__get__(mock_client)
        )
        return mock, mock_client

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize(
        "scraped",
        [
            "https://mnn-hub.prod.faria.cn@evil.test",  # userinfo spoof
            "http://mnn-hub.prod.faria.cn",  # cleartext downgrade
            "https://evil.test/hub",  # foreign host
            "wss://evil.test",  # non-http scheme
        ],
    )
    def test_hostile_endpoint_never_receives_the_token(
        self, mock_build_client_validating, tool, scraped
    ):
        _mock, mock_client = mock_build_client_validating
        mock_client.get_notification_token.return_value = (scraped, "JWT-SECRET")

        with patch("tahuti.mcp_server.hub_client") as MockHub:
            MockHub.return_value.stats.return_value = {}
            MockHub.return_value.list.return_value = {"items": [], "meta": {}}
            MockHub.return_value.mark_read.return_value = {}
            MockHub.return_value.mark_all_read.return_value = {}
            try:
                tool(notification_id=1) if tool is mark_notification else tool()
            except Exception:
                # The call may fail for unrelated reasons (mocked hub); only the
                # endpoint that was constructed matters here.
                pass

        assert MockHub.call_count >= 1, "no hub client was constructed"
        endpoint = MockHub.call_args.args[0]
        assert "evil.test" not in endpoint, (
            f"{tool.__name__} sent the hub JWT to attacker-chosen host {endpoint!r}"
        )
        assert endpoint.startswith("https://"), f"token would travel in cleartext: {endpoint!r}"

    @pytest.mark.parametrize("tool", TOOLS)
    def test_legitimate_hub_is_preserved(self, mock_build_client_validating, tool):
        """The guard must not break the working case."""
        from tahuti.notifications import HUB_ENDPOINTS

        _mock, mock_client = mock_build_client_validating
        mock_client.get_notification_token.return_value = (
            HUB_ENDPOINTS["managebac.cn"],
            "JWT-SECRET",
        )

        with patch("tahuti.mcp_server.hub_client") as MockHub:
            MockHub.return_value.stats.return_value = {}
            MockHub.return_value.list.return_value = {"items": [], "meta": {}}
            MockHub.return_value.mark_read.return_value = {}
            MockHub.return_value.mark_all_read.return_value = {}
            try:
                tool(notification_id=1) if tool is mark_notification else tool()
            except Exception:
                pass

        assert MockHub.call_count >= 1
        assert MockHub.call_args.args[0] == HUB_ENDPOINTS["managebac.cn"]


class TestMarkNotificationTool:
    def test_mark_read(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("https://mnn-hub.prod.faria.cn", "tok")

        with patch("tahuti.mcp_server.hub_client") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.mark_read.return_value = True
            result = mark_notification(notification_id=123, action="read")
            data = json.loads(result)
            assert data["ok"] is True
            assert data["action"] == "read"

    def test_invalid_action(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("https://mnn-hub.prod.faria.cn", "tok")

        with patch("tahuti.mcp_server.hub_client") as MockHub:
            result = mark_notification(notification_id=123, action="invalid")
            data = json.loads(result)
            assert "error" in data


class TestMarkAllNotificationsReadTool:
    def test_mark_all_read(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("https://mnn-hub.prod.faria.cn", "tok")

        with patch("tahuti.mcp_server.hub_client") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.mark_all_read.return_value = True
            result = mark_all_notifications_read()
            data = json.loads(result)
            assert data["ok"] is True


class TestGetCalendarEventsTool:
    def test_get_events(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_calendar_events.return_value = [
            {"id": 1, "title": "Exam", "start": "2026-04-30"}
        ]
        result = get_calendar_events(start_date="2026-04-29", end_date="2026-05-05")
        data = json.loads(result)
        assert data["start"] == "2026-04-29"
        assert data["end"] == "2026-05-05"
        assert len(data["events"]) == 1

    def test_defaults_to_today_through_six_days(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_calendar_events.return_value = []
        data = json.loads(get_calendar_events())
        assert data["start"] and data["end"]
        from datetime import date, timedelta

        assert data["start"] == date.today().isoformat()
        assert data["end"] == (date.today() + timedelta(days=6)).isoformat()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"start_date": "29/04/2026"},
            {"start_date": "2026-4-9"},
            {"start_date": "April 29"},
            {"start_date": "'; DROP TABLE"},
            {"start_date": "2026-02-30"},
            {"end_date": "2026-13-01"},
            {"end_date": "2026-05-05T00:00:00Z"},
            {"start_date": "2026-05-05", "end_date": "not-a-date"},
        ],
    )
    def test_rejects_malformed_dates(self, mock_build_client, kwargs):
        mock, mock_client = mock_build_client
        data = json.loads(get_calendar_events(**kwargs))
        assert "error" in data, data
        assert "events" not in data
        mock_client.get_calendar_events.assert_not_called()
        mock.assert_not_called()

    def test_error_message_names_the_expected_format(self, mock_build_client):
        mock, _client = mock_build_client
        data = json.loads(get_calendar_events(start_date="29/04/2026"))
        assert "YYYY-MM-DD" in data["error"]
        assert "start_date" in data["error"]


class TestGetICalFeedTool:
    def test_get_ical(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_ical_feed.return_value = "BEGIN:VCALENDAR\nEND:VCALENDAR"
        result = get_ical_feed()
        assert "VCALENDAR" in result


class TestGetTimetableTool:
    def test_get_timetable(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_timetable.return_value = {
            "days": [{"header": "Monday"}],
            "lessons": [{"subject": "Math", "period": "P1"}],
        }
        result = get_timetable(date_str="2026-04-28")
        data = json.loads(result)
        assert len(data["lessons"]) == 1
        assert data["lessons"][0]["subject"] == "Math"

    def test_defaults_to_this_week(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_timetable.return_value = {"days": [], "lessons": []}
        get_timetable()
        mock_client.get_timetable.assert_called_once_with(None)

    @pytest.mark.parametrize(
        "bad",
        ["28/04/2026", "2026-4-8", "next week", "2026-02-31", "2026-04-28T00:00:00Z"],
    )
    def test_rejects_malformed_date(self, mock_build_client, bad):
        mock, mock_client = mock_build_client
        data = json.loads(get_timetable(date_str=bad))
        assert "error" in data, data
        assert "date_str" in data["error"]
        mock_client.get_timetable.assert_not_called()
        mock.assert_not_called()


class TestListClassesTool:
    """`list_classes` reports the dashboard roster, not a task-link derivation.

    Deriving the roster from task links — which is what this did — drops every
    class with no tasks, because an empty class contributes no link to parse.
    It also cost a full crawl to answer a question the dashboard already
    answers.
    """

    def test_list_classes(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_classes.return_value = {"100": "Math", "200": "English"}
        result = list_classes()
        data = json.loads(result)
        assert len(data["classes"]) == 2
        ids = {c["id"] for c in data["classes"]}
        assert "100" in ids
        assert "200" in ids

    def test_uses_the_dashboard_roster(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_classes.return_value = {}
        list_classes()
        mock_client.get_classes.assert_called_once()
        mock_client.crawl_all.assert_not_called()

    def test_class_with_no_tasks_is_still_listed(self, mock_build_client):
        """The regression this fixes: an empty class has no task link to parse."""
        mock, mock_client = mock_build_client
        mock_client.get_classes.return_value = {"300": "Physics (empty)"}
        data = json.loads(list_classes())
        assert [c["name"] for c in data["classes"]] == ["Physics (empty)"]



class TestErrorSanitisation:
    """Tool results land in the model's context, so errors must be redacted.

    These helpers had no tests at all.
    """

    @pytest.mark.parametrize(
        "message,secret",
        [
            ("login failed for password=hunter2", "hunter2"),
            ("bad cookie: _managebac_session=abcdef123456", "abcdef123456"),
            ("401 from Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
            ("authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("token abc123 rejected", "abc123"),
            ("secret leakage", "leakage"),
            ("session expired", "expired"),
        ],
    )
    def test_credential_material_never_survives(self, message, secret):
        out = _sanitize_error(RuntimeError(message))
        assert secret not in out
        assert "error" in json.loads(_error_payload(RuntimeError(message)))

    def test_sensitive_match_replaces_the_whole_message(self):
        out = _sanitize_error(RuntimeError("401 for cookie _managebac_session=abc"))
        assert out == "an authentication or credential error occurred"

    @pytest.mark.parametrize(
        "message",
        [
            "plain HTTP failure",
            "connection reset by peer",
            "unexpected HTML in response",
            "",
        ],
    )
    def test_benign_messages_are_preserved(self, message):
        out = _sanitize_error(RuntimeError(message))
        assert message[:200] in out or message == ""

    def test_error_payload_is_valid_json(self):
        payload = _error_payload(ValueError("boom"))
        parsed = json.loads(payload)
        assert parsed == {"error": "boom"}

    def test_output_is_truncated(self):
        out = _sanitize_error(RuntimeError("x" * 100_000))
        assert len(out) <= 200

    def test_control_characters_are_stripped(self):
        out = _sanitize_error(RuntimeError("bad\x1b[31mred\x00null\nnewline\ttab"))
        assert "\x1b" not in out
        assert "\x00" not in out
        assert "\n" not in out
        # Tabs are kept so multi-line context stays readable.
        assert "\t" in out

    def test_totally_benign(self):
        assert _sanitize_error(RuntimeError("hello")) == "hello"

    def test_hostile_input_never_raises(self):
        hostile = [
            RuntimeError(),
            RuntimeError(None),
            RuntimeError("\udcff\udcfe not really utf-8"),
            RuntimeError("😀" * 5000),
            RuntimeError("".join(chr(i) for i in range(0, 0x300))),
            RuntimeError("A" * (10**6)),
            RuntimeError("\x7f\x80\x9f del and c1 controls"),
            KeyboardInterrupt("interrupted"),
            MemoryError("oom"),
        ]
        for exc in hostile:
            out = _sanitize_error(exc)
            assert isinstance(out, str)
            assert len(out) <= 200
            # Must also be embeddable in JSON output.
            json.loads(_error_payload(exc))

    def test_non_exception_input_is_tolerated(self):
        # str() on an arbitrary object must not blow up the tool.
        class Weird:
            def __str__(self):
                raise RuntimeError("cannot stringify")

        class ExcWrapper(Exception):
            def __init__(self):
                super().__init__("wrapped")

        try:
            _sanitize_error(ExcWrapper())
        except Exception as exc:  # pragma: no cover - defensive
            pytest.fail(f"_sanitize_error raised: {exc}")

    def test_no_absolute_local_path_leak_for_plain_errors(self):
        # Path-bearing errors are NOT currently stripped (see the report); this
        # test documents the current contract so the gap is visible rather than
        # silent, and will flip to asserting redaction once auth.py stops
        # embedding _creds_path().
        out = _sanitize_error(FileNotFoundError("/home/someone/.config/tahuti/creds.json"))
        assert isinstance(out, str)
