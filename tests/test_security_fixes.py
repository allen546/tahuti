"""Security-regression tests for the audit fixes."""
import pytest
from unittest.mock import MagicMock, patch

from tahuti import __main__ as m
from tahuti.client import ManageBacClient, _validate_school_domain
from tahuti.exceptions import CommandError


class TestSchoolDomainValidation:
    def test_rejects_offsite_school(self):
        with pytest.raises(CommandError) as e:
            _validate_school_domain("evil.com/x", "managebac.com")
        assert e.value.code == "invalid_school"

    def test_rejects_malformed_domain(self):
        with pytest.raises(CommandError) as e:
            _validate_school_domain("myschool", "https://evil.com")
        assert e.value.code == "invalid_domain"

    def test_rejects_empty_school(self):
        with pytest.raises(CommandError):
            _validate_school_domain("", "managebac.com")

    def test_accepts_valid(self):
        assert _validate_school_domain("myschool", "managebac.cn") == ("myschool", "managebac.cn")

    def test_accepts_arbitrary_valid_domain(self):
        assert _validate_school_domain("myschool", "managebac.co.jp") == (
            "myschool",
            "managebac.co.jp",
        )

    def test_strips_domain_suffix(self):
        assert _validate_school_domain("myschool.managebac.cn", "managebac.cn") == (
            "myschool",
            "managebac.cn",
        )

    def test_constructor_rejects_offsite(self):
        with pytest.raises(CommandError):
            ManageBacClient(school="evil.com/x")


class TestStateEvictionOrder:
    # `mark_reminder_dispatched` only prunes once the dict exceeds 5000 entries.
    _CAP = 5000

    def test_evicts_oldest_not_newest(self, tmp_path):
        """Eviction must drop the *oldest* entries, by insertion order.

        Two things made the old version of this test vacuous. It inserted two
        entries, far under the 5000-entry cap, so the eviction branch never ran
        and it only asserted Python's guaranteed dict insertion order. And it
        built the object with `__new__`, bypassing `__init__`, so nothing
        exercised the real load/eviction path.

        The zero-padded ids below are what make the assertion bite: a
        lexicographic sort of the keys would order `task_0001` first too, so
        the fix is pinned by inserting in an order where insertion order and
        sorted order agree — see `test_eviction_is_not_a_lexicographic_sort`,
        which uses ids where they disagree.
        """
        from tahuti.daemon.state import DaemonStateManager

        manager = DaemonStateManager(state_path=tmp_path / "state.json")
        cap = self._CAP
        for i in range(1, cap + 1):
            manager.mark_reminder_dispatched(f"{i:04d}", "24h")

        assert len(manager.dispatched_reminders) == cap, (
            "nothing evicted at exactly the cap; the test below would be vacuous"
        )

        # One more entry crosses the cap by one and must evict exactly one key.
        manager.mark_reminder_dispatched("newest", "24h")

        assert len(manager.dispatched_reminders) == cap
        assert not manager.is_reminder_dispatched("0001", "24h"), (
            "the oldest entry survived; eviction dropped a newer one"
        )
        assert manager.is_reminder_dispatched("0002", "24h"), (
            "eviction removed more than the single excess entry"
        )
        assert manager.is_reminder_dispatched("newest", "24h"), (
            "the newest entry was evicted instead of the oldest"
        )

    def test_eviction_prefers_insertion_order_over_sorted_keys(self, tmp_path):
        """The regression this guards: evicting `sorted(keys)[:excess]`.

        Zero-padded ids make insertion order and sorted order agree, so they
        cannot tell the two implementations apart. Here the new key is chosen to
        sort *before* every existing key: correct eviction drops the oldest
        inserted entry and keeps it, while a lexicographic eviction reaches for
        the new key and drops the entry the test just added. Fails if state.py's
        eviction line is changed to a lexicographic sort.
        """
        from tahuti.daemon.state import DaemonStateManager

        manager = DaemonStateManager(state_path=tmp_path / "state.json")
        cap = self._CAP
        for i in range(1, cap + 1):
            manager.mark_reminder_dispatched(i, "24h")

        oldest = "task_1:ddl_24h"
        assert oldest in manager.dispatched_reminders

        # "task_0" sorts before "task_1", so a lexicographic eviction picks it.
        manager.mark_reminder_dispatched(0, "24h")

        assert oldest not in manager.dispatched_reminders, (
            f"{oldest} was the oldest inserted entry and must be the one evicted"
        )
        assert "task_0:ddl_24h" in manager.dispatched_reminders, (
            "eviction dropped the newest entry because its key sorted first — "
            "state.py is sorting keys instead of dropping the oldest insertion"
        )
        assert len(manager.dispatched_reminders) == cap

    def test_legacy_list_format_migrates(self, tmp_path):
        import json
        from tahuti.daemon.state import DaemonStateManager
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"dispatched_reminders": ["task_1:ddl_1h"]}))
        m = DaemonStateManager(state_path=p)
        assert m.is_reminder_dispatched(1, "1h")
        assert isinstance(m.dispatched_reminders, dict)


def _parse_daemon_start(*extra):
    return m.build_parser().parse_args(["daemon", "start", *extra])


class TestBackgroundDaemonSecretEnv:
    """`daemon start --background` must pass credentials via the environment.

    argv is world-readable via `ps` for the life of the daemon, so leaving the
    password, cookie, or HMAC secret there leaks it to any local user.
    """

    def _start(self, args):
        with patch("tahuti.daemon.system.ServiceManager.start_background") as start:
            start.return_value = {"started": True}
            m.cmd_daemon_start(args)
        assert start.called, "start_background was not called"
        _, kwargs = start.call_args
        return kwargs.get("extra_args") or [], kwargs.get("env") or {}

    def test_password_goes_via_env_not_argv(self):
        extra, env = self._start(
            _parse_daemon_start("--background", "--password", "pw123")
        )
        assert env.get("MANAGEBAC_PASSWORD") == "pw123"
        assert "pw123" not in " ".join(extra)

    def test_cookie_goes_via_env_not_argv(self):
        extra, env = self._start(
            _parse_daemon_start("--background", "--cookie", "cookieval")
        )
        assert env.get("MANAGEBAC_COOKIE") == "cookieval"
        assert "cookieval" not in " ".join(extra)

    def test_background_start_with_credential_does_not_raise(self):
        """Regression: daemon_secret_env was used before it was assigned.

        The original fix built ``daemon_secret_env`` *after* the --password and
        --cookie branches that write into it, so any background start carrying a
        credential raised NameError instead of starting the daemon.
        """
        for argv in (
            ("--password", "pw123"),
            ("--cookie", "cookieval"),
            ("--password", "pw123", "--cookie", "cookieval"),
        ):
            extra, env = self._start(_parse_daemon_start("--background", *argv))
            assert not any(v in " ".join(extra) for v in ("pw123", "cookieval"))


class TestWebhookSecretFromEnv:
    """A background-spawned daemon must sign with the secret it was handed.

    ``daemon start --background`` forwards the secret as MB_WEBHOOK_SECRET, so
    the child must read it from the environment. Reading argv only left the
    daemon signing with an empty secret, and the receiver rejects every push.
    """

    def _captured_secret(self, env_secret):
        captured = {}

        def fake_start_loop(client, cfg, **kwargs):
            captured["secret"] = cfg["webhooks"][0]["secret"]
            raise SystemExit(0)  # stop before print_payload serialises mocks

        args = _parse_daemon_start("--webhook-url", "http://127.0.0.1:42617/webhook")
        args.dry_run = True
        with (
            patch.object(m, "start_loop", fake_start_loop),
            patch.object(
                m, "_build_client", return_value=(MagicMock(), MagicMock(), "default")
            ),
            patch.object(m, "_authenticate_client", lambda *a, **k: None),
        ):
            if env_secret is None:
                import os

                with patch.dict("os.environ", {}, clear=False):
                    os.environ.pop("MB_WEBHOOK_SECRET", None)
                    try:
                        m.cmd_daemon_start(args)
                    except SystemExit:
                        pass
            else:
                with patch.dict("os.environ", {"MB_WEBHOOK_SECRET": env_secret}):
                    try:
                        m.cmd_daemon_start(args)
                    except SystemExit:
                        pass
        return captured.get("secret")

    def test_daemon_start_reads_secret_from_environment(self):
        assert self._captured_secret("shared-secret") == "shared-secret"

    def test_daemon_start_without_env_secret_signs_nothing(self):
        assert self._captured_secret(None) is None

    def test_resolve_secret_prefers_cli_then_env(self):
        import os

        from tahuti.daemon import _resolve_secret

        with patch.dict("os.environ", {"MB_WEBHOOK_SECRET": "env-secret"}):
            assert _resolve_secret("cli-secret") == "cli-secret"
            assert _resolve_secret(None) == "env-secret"
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("MB_WEBHOOK_SECRET", None)
            assert _resolve_secret(None) is None
