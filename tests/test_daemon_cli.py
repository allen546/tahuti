"""Tests for daemon CLI commands."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import requests_mock

from tahuti.__main__ import main


def test_cli_daemon_status_not_running_exits_three(tmp_path):
    """`daemon status` succeeded, and its answer is "no daemon": exit 3.

    This test previously ran against the real default pid file and asserted
    ``code == 0``, so it passed for the wrong reason and silently ignored
    whether a daemon was actually there. It is now pinned in both directions.
    """
    status = {"running": False, "pid": None, "pid_file": "x", "log_file": "y"}
    with patch("tahuti.__main__.ServiceManager") as MockMgr:
        MockMgr.return_value.status.return_value = status
        with patch("builtins.print"):
            with pytest.raises(SystemExit) as exc_info:
                main(["daemon", "status", "--format", "json"])
            assert exc_info.value.code == 3


def test_cli_daemon_status_running_exits_zero(tmp_path):
    status = {"running": True, "pid": 4242, "pid_file": "x", "log_file": "y"}
    with patch("tahuti.__main__.ServiceManager") as MockMgr:
        MockMgr.return_value.status.return_value = status
        with patch("builtins.print"):
            with pytest.raises(SystemExit) as exc_info:
                main(["daemon", "status", "--format", "json"])
            assert exc_info.value.code == 0


def test_cli_daemon_test_webhook():
    wh_url = "http://localhost:9999/test-hook"
    with requests_mock.Mocker() as m:
        m.post(wh_url, status_code=200)
        with patch("builtins.print"):
            with pytest.raises(SystemExit) as exc_info:
                main(["daemon", "test-webhook", wh_url, "--format", "json"])
            assert exc_info.value.code == 0


def test_cli_daemon_run_once(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))

    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []

    mock_state = MagicMock()
    mock_state.active_profile = "default"

    with patch("tahuti.__main__._build_client") as mock_bc:
        mock_bc.return_value = (mock_state, mock_client, "test@example.com")
        with patch("tahuti.__main__._authenticate_client"):
            with patch("tahuti.daemon.service.MNNHubProvider") as MockProv:
                prov_inst = MockProv.return_value
                prov_inst.poll_events.return_value = []
                with patch("builtins.print"):
                    with pytest.raises(SystemExit) as exc_info:
                        main(["daemon", "run", "--once", "--format", "json"])
                    assert exc_info.value.code == 0


def test_cli_daemon_start_background_arg_forwarding():
    with patch("tahuti.__main__.ServiceManager") as MockMgr:
        mgr_instance = MockMgr.return_value
        mgr_instance.start_background.return_value = {"started": True, "pid": 12345}
        with patch("builtins.print"):
            with pytest.raises(SystemExit) as exc_info:
                main([
                    "daemon", "start", "-b",
                    "--webhook-url", "https://hook.example.com",
                    "--interval", "45",
                    "--format", "json",
                ])
            assert exc_info.value.code == 0
            call_kwargs = mgr_instance.start_background.call_args[1]
            extra_args = call_kwargs.get("extra_args", [])
            assert "--webhook-url" in extra_args
            assert "https://hook.example.com" in extra_args
            assert "--poll-interval" in extra_args
            assert "45" in extra_args
