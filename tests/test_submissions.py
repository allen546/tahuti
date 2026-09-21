"""Tests for submissions lifecycle management: get_submissions, delete_submission, CLI, and MCP."""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch



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
