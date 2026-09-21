"""The exit-code contract, asserted.

Every ``cmd_*`` handler's return value *is* the process exit status, so a
handler that returns 0 on a failure is invisible to any shell caller:
``mb notifications --read 42 || echo failed`` never fires. These tests drive
each repaired failure path through ``main`` and assert the status, because that
is the only thing a script can branch on. Where the failure is also reported in
the payload, the envelope is asserted to be ``ok: false`` — an ``ok: true``
envelope beside a non-zero status would be its own bug.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

worktree_src = str(Path(__file__).resolve().parent.parent / "src")
if sys.path[0] != worktree_src:
    sys.path.insert(0, worktree_src)

from unittest.mock import MagicMock, patch

import pytest

from tahuti.__main__ import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_NOT_RUNNING,
    EXIT_OK,
    cmd_view,
    main,
)


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch):
    """Point config/session state at tmp_path so nothing real is touched."""
    monkeypatch.setenv("MANAGEBAC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MANAGEBAC_SESSION", str(tmp_path / "session.json"))
    return tmp_path


def _state():
    state = MagicMock()
    state.active_profile = "default"
    return state


def _client():
    client = MagicMock()
    client.get_notification_token.return_value = ("https://hub.example", "tok")
    return client


def _run_main(argv):
    """Run ``main`` and return ``(exit_code, [payload, ...])``.

    ``print_payload`` is intercepted rather than stdout being scraped: the
    payloads under test are dicts before formatting, and `--format json` is not
    what makes them machine-readable.
    """
    payloads: list[dict] = []

    def _capture(payload, output=None, requested_format=None):
        payloads.append(payload)

    with (
        patch("tahuti.__main__.print_payload", side_effect=_capture),
        pytest.raises(SystemExit) as exc_info,
    ):
        main(argv)
    return exc_info.value.code, payloads


# ── `mb notifications --read/--unread/--read-all` ─────────────────────────


class TestNotificationsMutationExitCode:
    """`hub.mark_read()` returns a bare bool; a False used to exit 0."""

    def _run(self, argv, mark_result):
        hub = MagicMock()
        for mark in ("mark_read", "mark_unread", "mark_all_read"):
            getattr(hub, mark).return_value = mark_result
        with (
            patch("tahuti.__main__._build_client", return_value=(_state(), _client(), "a@b.com")),
            patch("tahuti.auth.save_profile"),
            patch("tahuti.auth.save_session"),
            # `cmd_notifications` builds its hub through `auth.hub_client`, not
            # `MNNHubClient` directly, so this is the seam to patch. Patching
            # the class left a real client talking to the fake `hub.example`
            # endpoint the fixture names.
            patch("tahuti.__main__.hub_client", return_value=hub),
        ):
            return _run_main(argv)

    @pytest.mark.parametrize(
        "argv,action",
        [
            (["notifications", "--read", "42", "--format", "json"], "read"),
            (["notifications", "--unread", "42", "--format", "json"], "unread"),
            (["notifications", "--read-all", "--format", "json"], "read_all"),
        ],
    )
    def test_rejected_mutation_exits_nonzero_with_error_envelope(
        self, argv, action, isolated_config
    ):
        code, payloads = self._run(argv, False)

        assert code == EXIT_FAILURE
        assert payloads, "a failing operation must emit a machine-readable payload"
        payload = payloads[-1]
        # The envelope must agree with the outcome rather than claim success
        # over a `false` result, which is what made this undetectable.
        assert payload["ok"] is False
        assert payload["command"] == "notifications.mutate"
        assert payload["error"]["code"] == f"{action}_failed"

    @pytest.mark.parametrize(
        "argv,action",
        [
            (["notifications", "--read", "42", "--format", "json"], "read"),
            (["notifications", "--unread", "42", "--format", "json"], "unread"),
            (["notifications", "--read-all", "--format", "json"], "read_all"),
        ],
    )
    def test_accepted_mutation_exits_zero(self, argv, action, isolated_config):
        code, payloads = self._run(argv, True)

        assert code == EXIT_OK
        payload = payloads[-1]
        assert payload["ok"] is True
        assert payload["data"]["action"] == action
        assert payload["data"]["ok"] is True


# ── `mb view` / `mb download` truthy error dicts ──────────────────────────


class _ViewArgs:
    """The argparse namespace `cmd_view` receives."""

    def __init__(self, **overrides):
        self.id = "123"
        self.target = None
        self.url = None
        self.pages = None
        self.refresh = False
        self.subject = None
        self.output = None
        self.format = "json"
        for key, value in overrides.items():
            setattr(self, key, value)


def test_view_detail_fetch_error_dict_exits_nonzero(capsys):
    """`get_task_detail` returns a *truthy* `{"error": ...}` on failure.

    An `if not detail:` guard cannot see that, so the error used to be nested
    inside an `ok: true` envelope and the process exited 0.
    """
    client = MagicMock()
    client.get_task_detail.return_value = {"error": "Session expired or invalid"}

    with (
        patch("tahuti.__main__._build_client", return_value=(_state(), client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch(
            "tahuti.__main__.find_task_by_id",
            return_value={"id": "123", "title": "Essay", "link": "http://x/123"},
        ),
    ):
        rc = cmd_view(_ViewArgs())

    assert rc == EXIT_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["command"] == "view"
    assert payload["error"]["code"] == "detail_fetch_failed"


def test_view_detail_fetch_error_dict_from_url_target_exits_nonzero(capsys):
    """The URL branch of `view` fetches details too, and has the same hole."""
    client = MagicMock()
    client.get_task_detail.return_value = {"error": "boom"}

    with (
        patch("tahuti.__main__._build_client", return_value=(_state(), client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch("tahuti.__main__.find_task_by_id", return_value=None),
    ):
        rc = cmd_view(
            _ViewArgs(
                id=None,
                target="https://school.managebac.cn/student/classes/1/core_tasks/123",
            )
        )

    assert rc == EXIT_FAILURE
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "detail_fetch_failed"


def test_view_success_still_exits_zero(capsys):
    client = MagicMock()
    client.get_task_detail.return_value = {"attachments": [], "status": "graded"}

    with (
        patch("tahuti.__main__._build_client", return_value=(_state(), client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch(
            "tahuti.__main__.find_task_by_id",
            return_value={"id": "123", "title": "Essay", "link": "http://x/123"},
        ),
    ):
        rc = cmd_view(_ViewArgs())

    assert rc == EXIT_OK
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_view_refuses_a_target_carrying_no_task_id(capsys):
    """`view` and the MCP tools must accept the same target shapes.

    The gate here used to be ``startswith("http")`` OR ``"/core_tasks/" in
    target``, so *any* URL reached ``target.split("core_tasks/")[-1]`` — with no
    separator present that yields the whole URL as the "id", which was then
    handed to ``get_task_detail``. The MCP tool refused the same input. Both
    surfaces now ask ``client.task_id_from_target``, so a class URL or an
    unrelated link is refused before any fetch, with exit 1 and an error
    envelope.
    """
    for target in (
        "https://school.managebac.cn/student/classes/1000023",
        "https://school.managebac.cn/student/classes/1000023/core_tasks",
        "https://school.managebac.cn/attachments/1/leak.pdf",
        "not-a-task-id",
        "",
    ):
        client = MagicMock()
        with (
            patch(
                "tahuti.__main__._build_client",
                return_value=(_state(), client, "a@b.com"),
            ),
            patch("tahuti.__main__._authenticate_client"),
            patch("tahuti.__main__.load_snapshot", return_value={}),
            patch("tahuti.__main__.find_task_by_id", return_value=None),
        ):
            rc = cmd_view(_ViewArgs(id=None, target=target))

        assert rc == EXIT_FAILURE, f"view accepted {target!r}"
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["command"] == "view"
        assert payload["error"]["code"] in ("missing_target", "invalid_target")
        # Refused before any detail fetch, which is the point: a garbage id used
        # to be fetched rather than reported.
        client.get_task_detail.assert_not_called()


def test_view_still_reads_a_bare_id_and_a_task_url(capsys):
    """Control: the shapes that always worked keep working, unchanged.

    The shared rule accepts a bare numeric id and a URL carrying
    ``/core_tasks/<id>``, and `view` keeps its two different fetches — a URL is
    fetched verbatim, a bare id must find its link first.
    """
    client = MagicMock()
    client.get_task_detail.return_value = {"description": "body"}
    task_url = "https://school.managebac.cn/student/classes/1/core_tasks/123"
    with (
        patch("tahuti.__main__._build_client", return_value=(_state(), client, "a@b.com")),
        patch("tahuti.__main__._authenticate_client"),
        patch("tahuti.__main__.load_snapshot", return_value={}),
        patch("tahuti.__main__.find_task_by_id", return_value=None),
    ):
        # The URL branch fetches the target verbatim, snapshot or no snapshot.
        rc = cmd_view(_ViewArgs(id=None, target=task_url))
        assert rc == EXIT_OK
        client.get_task_detail.assert_called_once_with(task_url, bypass_cache=False)

        # The id branch resolves a link from the snapshot before fetching.
        client.get_task_detail.reset_mock()
        with patch(
            "tahuti.__main__.find_task_by_id",
            return_value={"id": "123", "link": task_url},
        ):
            rc = cmd_view(_ViewArgs(id="123"))
        assert rc == EXIT_OK
        client.get_task_detail.assert_called_once_with(
            task_url, from_hint=False, bypass_cache=False
        )





# ── `main`'s handling of failures it does not model ───────────────────────


def test_unexpected_exception_emits_payload_not_traceback(isolated_config):
    """An unmodelled exception used to escape as a raw traceback."""
    client = MagicMock()
    client.get_notification_token.side_effect = RuntimeError("socket exploded")

    with (
        patch("tahuti.__main__._build_client", return_value=(_state(), client, "a@b.com")),
        patch("tahuti.auth.save_profile"),
        patch("tahuti.auth.save_session"),
    ):
        code, payloads = _run_main(["notifications", "--format", "json"])

    assert code == EXIT_FAILURE
    assert payloads, "the caller needs a machine-readable payload, not a traceback"
    payload = payloads[-1]
    assert payload["ok"] is False
    assert payload["command"] == "notifications"
    assert payload["error"]["code"] == "internal_error"
    assert "socket exploded" in payload["error"]["message"]


def test_usage_error_is_argparse_owned(isolated_config):
    """Exit 2 belongs to argparse; handlers never return it themselves."""
    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-command"])
    assert exc_info.value.code == 2


def test_command_error_maps_to_failure(isolated_config):
    """`CommandError` keeps its machine-readable code and a non-zero status."""
    with (
        patch("tahuti.__main__._build_client", return_value=(_state(), _client(), "a@b.com")),
        patch("tahuti.auth.save_profile"),
        patch("tahuti.auth.save_session"),
        patch("tahuti.__main__.hub_client") as MockHub,
    ):
        MockHub.return_value.list.side_effect = RuntimeError("hub down")
        code, _payloads = _run_main(["notifications", "--format", "json"])

    assert code == EXIT_FAILURE



# ── Ctrl-C and a closed pipe ───────────────────────────────────────────────


def _run_expecting(argv, exc):
    """Run ``main`` with *exc* raised from the handler; return the exit code."""
    def _boom(_args):
        raise exc

    real_parser = __import__("tahuti.__main__", fromlist=["build_parser"]).build_parser

    class _Parser:
        def parse_args(self, argv=None):
            args = real_parser().parse_args(argv)
            args.func = _boom
            return args

    with (
        patch("tahuti.__main__.build_parser", _Parser),
        pytest.raises(SystemExit) as exc_info,
    ):
        main(argv)
    return exc_info.value.code


def test_ctrl_c_exits_130_without_a_traceback(isolated_config, capsys):
    """A Ctrl-C at the password prompt used to reach the user as a traceback.

    `KeyboardInterrupt` is a `BaseException`, so the `except Exception` clause
    that turns unexpected errors into a payload never caught it — the whole
    stack unrolled onto the terminal for a user who had simply changed their
    mind.
    """
    code = _run_expecting(["list"], KeyboardInterrupt())

    assert code == EXIT_INTERRUPTED
    assert "Traceback" not in capsys.readouterr().err
    assert "internal_error" not in capsys.readouterr().err


def test_closed_pipe_is_not_an_error(isolated_config):
    """`tahuti list | head` closes stdout early; that is not a failure."""
    assert _run_expecting(["list"], BrokenPipeError(32, "Broken pipe")) == EXIT_OK


def test_exit_code_constants_are_the_documented_contract():
    """Pin the numbers the docstring promises, so a rename cannot drift."""
    assert (EXIT_OK, EXIT_FAILURE, EXIT_NOT_RUNNING, EXIT_INTERRUPTED) == (
        0, 1, 3, 130,
    )
