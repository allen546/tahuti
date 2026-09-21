"""Tests for submissions lifecycle management: get_submissions, delete_submission, CLI, and MCP."""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch



from tahuti.client import ManageBacClient
from tahuti.cache import DEFAULT_CACHE_DIR, ResponseCache
from tahuti import mcp_server


def _make_client(cache_dir=None) -> ManageBacClient:
    client = ManageBacClient.__new__(ManageBacClient)
    client.school = "testschool"
    client.domain = "managebac.cn"
    client.base = "https://testschool.managebac.cn"
    client.student_name = "Test Student"
    client.cache = ResponseCache(cache_dir=cache_dir or DEFAULT_CACHE_DIR)
    client.retry = 0
    client.request_delay = 0.0
    client._last_request_time = 0.0
    client._last_url = None
    client._url_locks = {}
    client._url_locks_mutex = threading.Lock()
    client.session = MagicMock()
    return client


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
