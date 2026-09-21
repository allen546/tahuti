"""Tests for submissions lifecycle management: get_submissions, delete_submission, CLI, and MCP."""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from tahuti.client import ManageBacClient
from tahuti.cache import DEFAULT_CACHE_DIR, ResponseCache
from tahuti.__main__ import build_parser, cmd_submissions
from tahuti import mcp_server


def _make_client(cache_dir=None) -> ManageBacClient:
    client = ManageBacClient.__new__(ManageBacClient)
    client.school = "testschool"
    client.domain = "managebac.cn"
    client.base = "https://testschool.managebac.cn"
    client.student_name = "Test Student"
    # Routed into the per-test tmp_path by the autouse `isolated_user_state`
    # fixture in conftest.py, which patches DEFAULT_CACHE_DIR. Spelled out here
    # so the dependency is visible at the call site: a bare ResponseCache()
    # would otherwise read and write the operator's real ~/.config/tahuti/cache.
    client.cache = ResponseCache(cache_dir=cache_dir or DEFAULT_CACHE_DIR)
    client.retry = 0
    client.request_delay = 0.0
    client._last_request_time = 0.0
    client._last_url = None
    client._url_locks = {}
    client._url_locks_mutex = threading.Lock()
    client.session = MagicMock()
    return client


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


HTML_UPCOMING_TASK = """
<!DOCTYPE html>
<html>
<head>
  <meta name="csrf-token" content="test_csrf_token_123" />
</head>
<body>
  <form id="edit_dropbox_17874401" action="/student/classes/101/core_tasks/202/dropbox/upload"></form>
  <table>
    <tr class="file" id="asset_82189817">
      <td>
        <a class="text-break" href="https://s3.amazonaws.com/uploads/asset/file/82189817/Chapter-4-Homework.pdf">
          Chapter-4-Homework.pdf
        </a>
        <label>Uploaded Sep 13, 2026 at 4:30 PM</label>
      </td>
      <td>
        <a class="btn btn-light" href="/feedback/82189817" title="View Teacher Feedback">View Teacher Feedback</a>
        <span class="actions">
          <a class="btn btn-light btn-icon btn-remove" data-method="delete" href="/student/dropboxes/17874401/destroy_asset?file_id=82189817">Delete</a>
        </span>
      </td>
    </tr>
  </table>
</body>
</html>
"""

HTML_PAST_DUE_TASK = """
<!DOCTYPE html>
<html>
<head>
  <meta name="csrf-token" content="test_csrf_token_123" />
</head>
<body>
  <form id="edit_dropbox_17867039" action="/student/classes/101/core_tasks/202/dropbox/upload"></form>
  <table>
    <tr class="file" id="asset_82095155">
      <td>
        <a class="text-break" href="https://s3.amazonaws.com/uploads/asset/file/82095155/Chapter-3-Homework.pdf">
          Chapter-3-Homework.pdf
        </a>
        <label>Uploaded Sep 8, 2026 at 6:53 PM</label>
      </td>
      <td>
        <a class="btn btn-light" href="/feedback/82095155" title="View Teacher Feedback">View Teacher Feedback</a>
      </td>
    </tr>
    <tr class="file" id="asset_82189773">
      <td>
        <a class="text-break" href="https://s3.amazonaws.com/uploads/asset/file/82189773/Accidental-Upload.pdf">
          Accidental-Upload.pdf
        </a>
        <label>Uploaded Sep 13, 2026 at 4:28 PM</label>
      </td>
      <td>
        <a class="btn btn-light" href="/feedback/82189773" title="View Teacher Feedback">View Teacher Feedback</a>
      </td>
    </tr>
  </table>
</body>
</html>
"""

HTML_EMPTY_TASK = """
<!DOCTYPE html>
<html>
<head><meta name="csrf-token" content="test_csrf_token_123" /></head>
<body>
  <form id="edit_dropbox_17874401"></form>
  <table></table>
</body>
</html>
"""


# ── Client Tests ─────────────────────────────────────────────────────────


def test_get_submissions_upcoming():
    client = _make_client()
    with patch.object(client, "_get", return_value=_soup(HTML_UPCOMING_TASK)):
        subs = client.get_submissions("101", "202")
    assert len(subs) == 1
    s = subs[0]
    assert s["asset_id"] == "82189817"
    assert s["name"] == "Chapter-4-Homework.pdf"
    assert s["uploaded_at"] == "Sep 13, 2026 at 4:30 PM"
    assert s["can_delete"] is True
    assert s["delete_url"] == "/student/dropboxes/17874401/destroy_asset?file_id=82189817"
    assert s["dropbox_id"] == "17874401"
    assert s["feedback_url"] == "https://testschool.managebac.cn/feedback/82189817"


def test_get_submissions_past_due():
    client = _make_client()
    with patch.object(client, "_get", return_value=_soup(HTML_PAST_DUE_TASK)):
        subs = client.get_submissions("101", "202")
    assert len(subs) == 2
    s1, s2 = subs
    assert s1["asset_id"] == "82095155"
    assert s1["can_delete"] is False
    assert s1["delete_url"] is None
    assert s1["dropbox_id"] == "17867039"

    assert s2["asset_id"] == "82189773"
    assert s2["name"] == "Accidental-Upload.pdf"
    assert s2["can_delete"] is False


def test_delete_submission_success():
    client = _make_client()
    initial_soup = _soup(HTML_UPCOMING_TASK)
    empty_soup = _soup(HTML_EMPTY_TASK)

    # Read 1 is delete_submission's own lookup of the task page; reads 2 and 3
    # are the post-delete verification, which re-reads the task page and then
    # falls back to the dropbox page when the task page shows no rows.
    with patch.object(client, "_get", side_effect=[initial_soup, empty_soup, empty_soup]):
        # _request_with_retry validates the response URL, so the stub carries a
        # real one. It is still the same DELETE on the same session.
        client.session.request.return_value = MagicMock(
            status_code=200,
            text="Turbolinks.visit(...)",
            url="https://testschool.managebac.cn/student/dropboxes/17874401/destroy_asset?file_id=82189817",
        )
        res = client.delete_submission("101", "202", "82189817")

    assert res["ok"] is True
    assert res["asset_id"] == "82189817"
    assert res["filename"] == "Chapter-4-Homework.pdf"
    assert res["remaining_submissions"] == 0

    client.session.request.assert_called_once()
    method, url = client.session.request.call_args[0]
    assert method == "DELETE"
    assert "file_id=82189817" in url
    assert "17874401" in url


def test_delete_submission_by_filename():
    client = _make_client()
    initial_soup = _soup(HTML_UPCOMING_TASK)
    empty_soup = _soup(HTML_EMPTY_TASK)

    with patch.object(client, "_get", side_effect=[initial_soup, empty_soup, empty_soup]):
        client.session.request.return_value = MagicMock(
            status_code=200,
            url="https://testschool.managebac.cn/student/dropboxes/17874401/destroy_asset?file_id=82189817",
        )
        res = client.delete_submission("101", "202", "Chapter-4-Homework.pdf")

    assert res["ok"] is True
    assert res["asset_id"] == "82189817"


def test_delete_submission_not_found():
    client = _make_client()
    with patch.object(client, "_get", return_value=_soup(HTML_UPCOMING_TASK)):
        with pytest.raises(ValueError, match="Submission not found"):
            client.delete_submission("101", "202", "nonexistent_file.pdf")


def test_delete_submission_server_rollback_rejection():
    client = _make_client()
    past_soup = _soup(HTML_PAST_DUE_TASK)

    # Server returns 200, but file remains on page during verification
    with patch.object(client, "_get", return_value=past_soup):
        client.session.request.return_value = MagicMock(
            status_code=200,
            url="https://testschool.managebac.cn/student/dropboxes/17874401/destroy_asset?file_id=82189773",
        )
        with pytest.raises(RuntimeError, match="task deadline has passed or ManageBac server locked the submission"):
            client.delete_submission("101", "202", "82189773")


# ── CLI Tests ───────────────────────────────────────────────────────────


def test_cli_submissions_parser():
    parser = build_parser()
    args = parser.parse_args(["submissions", "202", "--list"])
    assert args.target == "202"
    assert args.list is True

    args_del = parser.parse_args(["submissions", "202", "--delete", "82189817"])
    assert args_del.delete == "82189817"

    args_add = parser.parse_args(["submissions", "202", "--add", "test.pdf"])
    assert args_add.add == "test.pdf"

    args_sub = parser.parse_args(["submissions", "202", "--submit", "test.pdf"])
    assert args_sub.add == "test.pdf"

    args_fb = parser.parse_args(["submissions", "202", "--check-feedback"])
    assert args_fb.check_feedback is True

    args_fb_id = parser.parse_args(["submissions", "202", "--check-feedback", "82189817"])
    assert args_fb_id.check_feedback == "82189817"


@patch("tahuti.__main__._build_client")
@patch("tahuti.__main__._authenticate_client")
@patch("tahuti.__main__._resolve_task_ids", return_value=("101", "202"))
@patch("tahuti.__main__.print_payload")
def test_cli_submissions_list_default(mock_print, mock_resolve, mock_auth, mock_build):
    client = _make_client()
    mock_build.return_value = (MagicMock(active_profile="default", config_path=MagicMock()), client, "test@example.com")
    with patch.object(client, "get_submissions", return_value=[{"asset_id": "1", "name": "hw.pdf"}]):
        parser = build_parser()
        args = parser.parse_args(["submissions", "202"])
        rc = cmd_submissions(args)
        assert rc == 0
        payload = mock_print.call_args[0][0]
        assert payload["ok"] is True
        assert payload["data"]["action"] == "list"
        assert len(payload["data"]["submissions"]) == 1


@patch("tahuti.__main__._build_client")
@patch("tahuti.__main__._authenticate_client")
@patch("tahuti.__main__._resolve_task_ids", return_value=("101", "202"))
@patch("tahuti.__main__.print_payload")
def test_cli_submissions_delete(mock_print, mock_resolve, mock_auth, mock_build):
    client = _make_client()
    mock_build.return_value = (MagicMock(active_profile="default", config_path=MagicMock()), client, "test@example.com")
    with patch.object(client, "delete_submission", return_value={"ok": True, "asset_id": "1", "filename": "hw.pdf", "remaining_submissions": 0}):
        parser = build_parser()
        args = parser.parse_args(["submissions", "202", "--delete", "1"])
        rc = cmd_submissions(args)
        assert rc == 0
        payload = mock_print.call_args[0][0]
        assert payload["ok"] is True
        assert payload["data"]["action"] == "delete"


@patch("tahuti.__main__._build_client")
@patch("tahuti.__main__._authenticate_client")
@patch("tahuti.__main__.print_payload")
def test_cli_submissions_missing_target(mock_print, mock_auth, mock_build):
    mock_build.return_value = (MagicMock(active_profile="default"), _make_client(), "test@example.com")
    parser = build_parser()
    args = parser.parse_args(["submissions"])
    rc = cmd_submissions(args)
    assert rc == 1
    payload = mock_print.call_args[0][0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_target"


# ── MCP Server Tests ────────────────────────────────────────────────────


@patch("tahuti.mcp_server.build_client")
@patch("tahuti.__main__._resolve_task_ids", return_value=("101", "202"))
def test_mcp_delete_submission(mock_resolve, mock_build):
    """`delete_submission` resolves through the CLI's one shared ladder.

    This used to patch ``tahuti.mcp_server.parse_task_url``, because the tool
    carried its own copy of the ladder and that was the copy's entry point.
    The MCP copy also ended at ``get_tasks_by_view`` with no
    ``find_task_by_id`` step, so this tool reported tasks unresolvable that the
    CLI submits to; the shared ladder is what the contract now names.
    """
    client = _make_client()
    mock_build.return_value = (MagicMock(), client, "test@example.com")
    with patch.object(client, "delete_submission", return_value={"ok": True, "asset_id": "82189817", "filename": "hw.pdf"}) as mock_delete:
        res = json.loads(mcp_server.delete_submission("202", "82189817"))
        assert res["ok"] is True
        assert res["asset_id"] == "82189817"
        mock_delete.assert_called_once_with("101", "202", "82189817")
