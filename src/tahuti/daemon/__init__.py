"""Daemon package for tahuti real-time notifications and deadline tracking."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time as dt_time, timedelta
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from zoneinfo import ZoneInfo

from ..client import ManageBacClient
from ..config import SNAPSHOT_FILENAME, config_dir
from .events import (
    DaemonConfig,
    MBEvent,
    ReminderThreshold,
    StealthConfig,
    WebhookConfig,
)
from .provider import (
    AbstractNotificationProvider,
    MNNHubProvider,
    MobilePushProvider,
)
from .scheduler import DDLScheduler
from .service import DaemonService
from .state import DaemonStateManager
from .stealth import StealthTaskCrawler
from .stream import ManageBacDaemon
from .system import (
    DEFAULT_LOG_PATH,
    DEFAULT_PID_PATH,
    ONCE_PID_SENTINEL,
    ServiceManager,
    read_pid_file,
    terminate_pid,
    write_pid_file,
)
from .webhook import WebhookDispatcher
from ..task_status import format_grade_display, is_task_graded

__all__ = [
    "AbstractNotificationProvider",
    "DDLScheduler",
    "DaemonConfig",
    "DaemonService",
    "DaemonStateManager",
    "MNNHubProvider",
    "ManageBacDaemon",
    "MBEvent",
    "MobilePushProvider",
    "ReminderThreshold",
    "ServiceManager",
    "StealthConfig",
    "StealthTaskCrawler",
    "WebhookConfig",
    "WebhookDispatcher",
    "configure_channel_send",
    "configure_webhook",
    "make_auth_refresh_fn",
    "normalize_active_windows",
    "run_daemon_once",
    "save_daemon_config",
    "start_loop",
    "stop_daemon",
]

log = logging.getLogger(__name__)

DEFAULT_DAEMON_PATH = config_dir() / "daemon.json"
DEFAULT_WEBHOOK_URL = "http://127.0.0.1:42617/webhook"
DEFAULT_SNAPSHOT_PATH = config_dir() / SNAPSHOT_FILENAME

# Empty means "no gating": the daemon polls on its interval around the clock.
# Active hours are opt-in — either `--active-hours-start/--active-hours-end` or
# an `active_windows` entry in daemon.json.
DEFAULT_ACTIVE_WINDOWS: list[list[str]] = []


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


# Secrets may be supplied via the environment instead of argv, because
# argv is world-readable via `ps` on most systems.
SECRET_ENV = "MB_WEBHOOK_SECRET"


def _resolve_secret(cli_secret: str | None) -> str | None:
    """Prefer the environment over argv for the HMAC secret."""
    if cli_secret:
        return cli_secret
    val = os.environ.get(SECRET_ENV)
    return val or None


# ── Backward Compatibility API ──────────────────────────────────────────


def _coerce_window_edge(value: object) -> str | None:
    """Normalise one edge of an active window to ``"HH:MM"``.

    Hand-written daemon.json files reach us with ints (``[[7, 23]]``), bare
    hours (``"7"``) and single-digit strings (``"7:00"``). Anything that cannot
    be read as a wall-clock time returns None so the caller can drop it.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{value:02d}:00" if 0 <= value <= 23 else None
    if isinstance(value, float) and value.is_integer():
        return _coerce_window_edge(int(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if ":" not in text:
        # A bare hour: "7" and "07" both mean 07:00.
        if not text.isdigit():
            return None
        hour = int(text)
        return f"{hour:02d}:00" if 0 <= hour <= 23 else None
    head, _, tail = text.partition(":")
    if not head.strip().isdigit():
        return None
    hour = int(head.strip())
    if not 0 <= hour <= 23:
        return None
    minute_text = tail.strip() or "0"
    if not minute_text.isdigit():
        return None
    minute = int(minute_text)
    if not 0 <= minute <= 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def normalize_active_windows(raw: object) -> list[list[str]]:
    """Return only the well-formed ``[[start, end], ...]`` windows in ``raw``.

    A malformed window is dropped rather than carried forward: the documented
    contract is that a typo fails *open* (keep polling), and the only way to
    guarantee that from here is to never hand a malformed window to the code
    that parses it. If every window is malformed the result is empty, which
    means "no gating".
    """
    if not isinstance(raw, (list, tuple)):
        return []
    windows: list[list[str]] = []
    for window in raw:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            continue
        start = _coerce_window_edge(window[0])
        end = _coerce_window_edge(window[1])
        if start is None or end is None:
            continue
        windows.append([start, end])
    return windows


def load_daemon_config(path: str | None = None) -> dict:
    daemon_path = Path(path).expanduser() if path else DEFAULT_DAEMON_PATH
    if not daemon_path.exists():
        return {
            "delivery": {
                "mode": "webhook",
                "webhook_url": DEFAULT_WEBHOOK_URL,
            },
            "active_windows": DEFAULT_ACTIVE_WINDOWS,
            "verify_tls": True,
            "snapshot_file": str(DEFAULT_SNAPSHOT_PATH),
            "pid_file": str(DEFAULT_PID_PATH),
            "log_file": str(DEFAULT_LOG_PATH),
        }
    data = json.loads(daemon_path.read_text(encoding="utf-8"))
    if "delivery" not in data:
        data["delivery"] = {
            "mode": "webhook",
            "webhook_url": data.pop("webhook_url", DEFAULT_WEBHOOK_URL),
        }
    if "active_windows" not in data:
        start = data.pop("active_hours_start", 7)
        end = data.pop("active_hours_end", 23)
        data["active_windows"] = [[f"{start:02d}:00", f"{end:02d}:00"]]
    data["active_windows"] = normalize_active_windows(data.get("active_windows"))
    return data


def save_daemon_config(data: dict, path: str | None = None) -> Path:
    """Write daemon.json so the HMAC secret is never world-readable.

    ``write_text`` creates the file with the process umask — 0644 on most
    systems — *containing the cleartext webhook secret*, and only the ``chmod``
    that follows tightens it. A crash between the two lines leaves the secret
    at 0644 permanently. mkstemp + chmod + ``os.replace`` closes that window:
    the destination is created by renaming an already-0600 file.
    """
    daemon_path = Path(path).expanduser() if path else DEFAULT_DAEMON_PATH
    _ensure_parent(daemon_path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(daemon_path.parent),
        prefix=".daemon_config_",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        # chmod before the rename, so the secret is 0600 before it is visible
        # under its final name.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, daemon_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return daemon_path


def _failed_config_save(exc: Exception) -> dict:
    """The in-band failure signal for the configure helpers.

    Both used to return the config dict unconditionally, so a write that failed
    could only surface as an exception — which left the CLI with nothing to turn
    into an exit code. The ``ok`` flag is what ``return 0 if cfg.get("ok") else 1``
    keys off.
    """
    log.error("Could not persist daemon config: %s", exc)
    return {"ok": False, "error": str(exc)}


def configure_webhook(url: str, path: str | None = None) -> dict:
    try:
        config = load_daemon_config(path)
        config["delivery"] = {"mode": "webhook", "webhook_url": url}
        save_daemon_config(config, path)
    except Exception as exc:  # noqa: BLE001 - reported in-band, not raised
        return _failed_config_save(exc)
    return {"ok": True, **config}


def configure_channel_send(
    channel_id: str,
    recipient: str,
    path: str | None = None,
    zeroclaw_bin: str | None = None,
) -> dict:
    try:
        config = load_daemon_config(path)
        delivery: dict[str, str] = {
            "mode": "channel_send",
            "channel_id": channel_id,
            "recipient": recipient,
        }
        if zeroclaw_bin:
            delivery["zeroclaw_bin"] = zeroclaw_bin
        config["delivery"] = delivery
        save_daemon_config(config, path)
    except Exception as exc:  # noqa: BLE001 - reported in-band, not raised
        return _failed_config_save(exc)
    return {"ok": True, **config}


def _task_index(tasks: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tasks if t.get("id")}


def diff_index(old: dict, new: dict) -> tuple[list[dict], list[dict]]:
    alerts: list[dict] = []
    changed_ids: list[str] = []

    old_overdue = _task_index(old.get("overdue", []))
    new_overdue = _task_index(new.get("overdue", []))
    old_upcoming = _task_index(old.get("upcoming", []))
    new_upcoming = _task_index(new.get("upcoming", []))

    all_old = {**old_overdue, **old_upcoming, **_task_index(old.get("past", []))}
    all_new = {**new_overdue, **new_upcoming, **_task_index(new.get("past", []))}

    # Overdue tasks
    for tid, task in new_overdue.items():
        if tid not in old_overdue:
            alerts.append(
                {
                    "type": "new_overdue",
                    "severity": "high",
                    "task": task,
                    "message": f"Task is now overdue: {task['title']} ({task.get('class_name', '')})",
                }
            )
            changed_ids.append(tid)

    # Overdue transitions within upcoming view
    for tid, task in new_upcoming.items():
        if tid in new_overdue:
            continue
        if task.get("view") == "overdue" and (
            tid not in old_upcoming or old_upcoming[tid].get("view") != "overdue"
        ):
            alerts.append(
                {
                    "type": "new_overdue",
                    "severity": "high",
                    "task": task,
                    "message": f"Task is now overdue: {task['title']} ({task.get('class_name', '')})",
                }
            )
            changed_ids.append(tid)

    # New upcoming tasks
    for tid, task in new_upcoming.items():
        if tid not in old_upcoming and task.get("view") != "overdue":
            if is_task_graded(task):
                # Score released along with task itself: suppress new_upcoming in favor of task_graded below
                continue
            alerts.append(
                {
                    "type": "new_upcoming",
                    "severity": "medium",
                    "task": task,
                    "message": f"New task: {task['title']} due {task.get('due_date', '?')} ({task.get('class_name', '')})",
                }
            )
            changed_ids.append(tid)

    # Grade change: fire whenever the effective grade display changes to a real value
    # Covers first-time grading, re-grading, N/A↔letter, score corrections,
    # and tasks created with a grade already released.
    for tid, task in all_new.items():
        old_task = all_old.get(tid)
        if old_task is not None:
            old_display = format_grade_display(old_task, standalone=True)
            new_display = format_grade_display(task, standalone=True)
            if new_display != old_display and new_display != "None":
                grade_letter = task.get("grade_letter") or ""
                grade_score = task.get("grade_score") or ""
                alerts.append(
                    {
                        "type": "task_graded",
                        "severity": "info",
                        "task": task,
                        "grade_letter": grade_letter or None,
                        "grade_score": grade_score or None,
                        "message": (
                            f"Grade posted: {task.get('title', tid)}"
                            f" ({task.get('class_name', '')}) → {new_display}"
                        ),
                    }
                )
                changed_ids.append(tid)
        elif is_task_graded(task):
            new_display = format_grade_display(task, standalone=True)
            grade_letter = task.get("grade_letter") or ""
            grade_score = task.get("grade_score") or ""
            alerts.append(
                {
                    "type": "task_graded",
                    "severity": "info",
                    "task": task,
                    "grade_letter": grade_letter or None,
                    "grade_score": grade_score or None,
                    "message": (
                        f"Grade posted: {task.get('title', tid)}"
                        f" ({task.get('class_name', '')}) → {new_display}"
                    ),
                }
            )
            changed_ids.append(tid)

    # Notifications
    old_unread = old.get("notifications", {}).get("unread_count", 0)
    new_unread = new.get("notifications", {}).get("unread_count", 0)
    if new_unread > old_unread:
        delta = new_unread - old_unread
        alerts.append(
            {
                "type": "new_notifications",
                "severity": "medium",
                "task": {},
                "message": f"{delta} new notification(s) ({new_unread} unread total)",
            }
        )

    # Teacher feedback — only fires when snapshot was produced with feedback_items
    # (i.e. the daemon ran with feedback.enabled=true). Tracks per-task novelty
    # in old["feedback_seen"] = {task_id: [submission_name, ...]}.
    old_feedback_seen: dict[str, list[str]] = old.get("feedback_seen", {})
    new_feedback_seen: dict[str, list[str]] = {}
    for tid, task in all_new.items():
        fb_items = task.get("feedback_items", [])
        if not fb_items:
            continue
        # Only consider submissions that actually have feedback content
        new_names = [
            f["submission_name"]
            for f in fb_items
            if (f.get("comment") or f.get("rubric") or f.get("attachments"))
            and f.get("submission_name")
        ]
        new_feedback_seen[tid] = new_names
        old_names = set(old_feedback_seen.get(tid, []))
        novel = [n for n in new_names if n not in old_names]
        if novel:
            alerts.append(
                {
                    "type": "new_feedback",
                    "severity": "info",
                    "task": task,
                    "message": (
                        f"Teacher feedback posted: {task.get('title', tid)} "
                        f"({task.get('class_name', '')}) — "
                        + ", ".join(novel)
                    ),
                }
            )
            changed_ids.append(tid)

    # Persist updated feedback_seen into the new snapshot for next diff
    if new_feedback_seen:
        new["feedback_seen"] = {**old_feedback_seen, **new_feedback_seen}

    return alerts, changed_ids


def _diff_snapshots_full(old: dict, new: dict) -> list[dict]:
    return diff_index(old, new)[0]


def load_snapshot(path: Path) -> dict:
    from tahuti.__main__ import load_snapshot as _load

    return _load(path)


def save_snapshot(path: Path, data: dict) -> None:
    from tahuti.__main__ import save_snapshot as _save

    _save(path, data)


def _post_webhook(
    webhook_url: str, alerts: list[dict], result: dict, verify: bool = True
) -> bool:
    import requests
    message = "\n".join(alert["message"] for alert in alerts)
    footer = (
        f"\n[tahuti daemon] student={result.get('student_name')} "
        f"upcoming={result.get('summary', {}).get('upcoming_count', '?')}"
    )
    payload = {"message": message + footer}
    response = requests.post(webhook_url, json=payload, timeout=60, verify=verify)
    return response.status_code < 400


def run_daemon_once(
    client: ManageBacClient,
    daemon_config: dict,
    dry_run: bool = False,
) -> dict:
    snapshot_path = Path(daemon_config["snapshot_file"]).expanduser()
    old = load_snapshot(snapshot_path)
    # Deliberately still fetching notifications (the `crawl_all` default).  The
    # three MNN-hub requests are *not* waste here: `diff_index` below reads
    # `new["notifications"]["unread_count"]` and raises the `new_notifications`
    # alert when it climbs, so passing `fetch_notifications=False` would zero
    # that count and the daemon would stop reporting new notifications
    # entirely — silently, because every `run_daemon_once` test passes a mocked
    # client whose `crawl_all` return value is fixed, so no assertion would
    # catch it.  `save_snapshot` also persists the key, and the next cycle's
    # `diff_index` reads it back as `old`.
    result = client.crawl_all(max_pages=10, fetch_details=False)
    alerts = _diff_snapshots_full(old, result)

    delivered = False
    if dry_run:
        # The snapshot is the baseline every future diff is measured against, so
        # advancing it here is the same class of bug as persisting dedup state:
        # the dry run would report the alerts it found and then leave a baseline
        # claiming they had already been seen. The next real run diffs against
        # *that*, finds nothing, and delivers nothing — the dry run ate the
        # delivery it was only meant to preview. The snapshot is also the user's
        # own record of what was last delivered, and a dry run delivers nothing.
        log.info(
            "Dry run: %d alert(s) computed, snapshot left at %s unchanged",
            len(alerts),
            snapshot_path,
        )
    else:
        save_snapshot(snapshot_path, result)
        if alerts:
            webhook_url = daemon_config.get("delivery", {}).get(
                "webhook_url", DEFAULT_WEBHOOK_URL
            )
            verify = daemon_config.get("verify_tls", True)
            delivered = _post_webhook(webhook_url, alerts, result, verify=verify)
    return {
        "alerts": alerts,
        "alert_count": len(alerts),
        "delivered": delivered,
        "snapshot_file": str(snapshot_path),
    }


def _parse_window(w: list[str]) -> tuple[dt_time, dt_time]:
    parts_s = w[0].split(":")
    parts_e = w[1].split(":")
    return (
        dt_time(int(parts_s[0]), int(parts_s[1])),
        dt_time(int(parts_e[0]), int(parts_e[1])),
    )


def _now_local() -> datetime:
    return datetime.now(ZoneInfo("UTC")).astimezone()


def _is_in_window(now: dt_time, start: dt_time, end: dt_time) -> bool:
    """Whether ``now`` falls inside ``[start, end)``.

    The end is deliberately exclusive: a ``09:00-17:00`` window must stop
    polling at 17:00, not keep polling through the minute that begins it. With
    an inclusive end, a window ending exactly at ``now`` reports "inside", the
    daemon polls one more cycle, and back-to-back windows double-count their
    shared boundary minute.
    """
    if start <= end:
        return start <= now < end
    # Window wraps past midnight (e.g. 22:00-02:00): either side of midnight.
    return now >= start or now < end


def _next_active_window(daemon_config: dict) -> datetime:
    """The earliest moment at or after now when polling is allowed again."""
    windows = daemon_config.get("active_windows") or DEFAULT_ACTIVE_WINDOWS
    if not windows:
        # No gating configured: "now" is always inside a window.
        return _now_local()
    now = _now_local()
    now_t = now.time()

    for w in windows:
        start, end = _parse_window(w)
        if _is_in_window(now_t, start, end):
            return now

    # Not inside any window: the next opening is the earliest start still ahead
    # of us today. Taking the *first match in list order* instead of the minimum
    # makes the daemon sleep through an earlier window that happens to be listed
    # later — e.g. [["22:00","23:00"],["12:00","13:00"]] at 10:00 waits until
    # 22:00 and misses everything due at midday.
    later_today: list[datetime] = []
    starts: list[dt_time] = []
    for w in windows:
        start, _ = _parse_window(w)
        starts.append(start)
        candidate = now.replace(
            hour=start.hour, minute=start.minute, second=0, microsecond=0
        )
        if candidate > now:
            later_today.append(candidate)
    if later_today:
        return min(later_today)

    # Every window has opened and closed today; the next one is tomorrow's
    # earliest start.
    first_start = min(starts)
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=first_start.hour, minute=first_start.minute, second=0, microsecond=0
    )


def _time_until(target: datetime) -> float:
    delta = (target - _now_local()).total_seconds()
    return max(delta, 1.0)


def _is_tahuti_pid(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        cmdline = result.stdout.strip()
        # No bare "mb" here: it matches unrelated processes (systemd, etc.)
        # and a stale pid file would then signal the wrong process. `tahuti`
        # stays because that is the module the daemon child is spawned as.
        return any(
            k in cmdline for k in ("tahuti", "tahuti", "mb_crawler", "mb.cli")
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


def _log(path: Path, message: str) -> None:
    _ensure_parent(path)
    # Messages embed scraped task titles, so strip control characters to
    # prevent forged log lines and terminal escape sequences.
    safe = "".join(
        ch for ch in str(message) if ch == "\t" or (0x20 <= ord(ch) != 0x7F)
    )
    line = f"[{datetime.now().isoformat()}] {safe}"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def make_auth_refresh_fn(
    client: ManageBacClient, state: object | None = None
) -> Callable[[], bool]:
    """Build the session-refresh callback the daemon needs to survive expiry.

    Without it, :class:`~tahuti.daemon.provider.MNNHubProvider` re-raises the
    "session expired" error on every poll and the daemon spins on a dead session
    forever while ``daemon status`` still reports it running.
    """

    def refresh_fn() -> bool:
        from ..auth import _relogin_from_creds

        try:
            _relogin_from_creds(client, state)
            return True
        except Exception as err:  # noqa: BLE001 - any failure means "not refreshed"
            log.warning("Silent re-login failed: %s", err)
            return False

    return refresh_fn


def _release_pid_file(pid_path: Path, expected: str) -> None:
    """Unlink ``pid_path`` only while it still records ``expected``.

    Unlinking unconditionally is how a finishing ``daemon stop`` removes the pid
    file of a ``daemon start`` that landed in the same window, stranding a live
    daemon that nothing can stop.
    """
    try:
        current = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if current == expected:
        pid_path.unlink(missing_ok=True)


def _alert_from_event(event: MBEvent) -> dict:
    """Render a dispatched event in the ``alerts`` shape the CLI reports."""
    data = event.data if isinstance(event.data, dict) else {}
    title = data.get("title") or data.get("task_title") or ""
    class_name = data.get("class_name") or ""
    suffix = f" ({class_name})" if class_name else ""
    if event.event == "deadline_approaching":
        threshold = data.get("reminder_threshold") or "soon"
        message = f"Deadline in {threshold}: {title}{suffix}"
    else:
        label = str(event.event).replace("_", " ")
        message = f"{label}: {title}{suffix}".strip()
    return {
        "type": event.event,
        "severity": "medium",
        "task": data,
        "message": message,
    }


def _run_once(service: DaemonService, log_path: Path) -> dict:
    """Run exactly one check cycle and report what actually happened.

    The previous ``once`` branch diffed a snapshot, logged ``delivered=False``
    and returned before :class:`DaemonService` — the only owner of the webhook
    dispatcher — was ever constructed, so ``daemon start --once --webhook-url …``
    exited 0 having sent nothing.
    """
    res = service.run_check_cycle()
    events = list(res.get("dispatched_events") or [])
    alerts = [_alert_from_event(event) for event in events]
    detail_fetches = sum(
        1
        for event in events
        if isinstance(event.data, dict) and event.data.get("enriched_task")
    )
    # A dry run computes what it *would* POST and posts nothing, so "delivered"
    # has to be false for it.
    delivered = bool(alerts) and not service.dry_run
    _log(
        log_path,
        f"once alert_count={len(alerts)} "
        f"details_fetched={detail_fetches} "
        f"delivered={delivered}",
    )
    return {
        "alerts": alerts,
        "alert_count": len(alerts),
        "detail_fetches": detail_fetches,
        "delivered": delivered,
        "dry_run": bool(service.dry_run),
        "new_notifications": res.get("new_notifications", 0),
        "reminders_dispatched": res.get("reminders_dispatched", 0),
        "total_dispatched": res.get("total_dispatched", len(events)),
        # A cycle that could not poll is not a cycle that found nothing. Without
        # this the CLI has to exit 0 on a poll that never happened.
        "poll_error": res.get("poll_error"),
        "deadline_error": res.get("deadline_error"),
        # ``to_dict()`` because the CLI json-dumps this payload and MBEvent is
        # not serialisable.
        "dispatched_events": [event.to_dict() for event in events],
    }


def start_loop(
    client: ManageBacClient,
    daemon_config: dict,
    dry_run: bool = False,
    once: bool = False,
    on_start: Callable[[DaemonService], None] | None = None,
    auth_refresh_fn: Callable[[], bool] | None = None,
) -> dict:
    """Run one check cycle (``once``) or the daemon loop until interrupted.

    ``auth_refresh_fn`` must be supplied by the CLI, which is the only place
    that has the persisted credential state a silent re-login needs. Omitting it
    is the defect that turns a session expiry into a permanent silent failure.
    """
    pid_path = Path(daemon_config["pid_file"]).expanduser()
    log_path = Path(daemon_config["log_file"]).expanduser()
    _ensure_parent(pid_path)

    if once:
        # A one-shot run must not publish a pid `daemon stop` would act on: this
        # process exits momentarily, and the pid in the file is not a daemon.
        write_pid_file(pid_path, ONCE_PID_SENTINEL)

    try:
        config = DaemonConfig.from_dict(daemon_config)
        service = DaemonService(
            client,
            config=config,
            on_start=on_start,
            dry_run=dry_run,
            auth_refresh_fn=auth_refresh_fn,
            daemon_config=daemon_config,
            # The service owns its own pid file: `daemon run` reaches it without
            # going through here, and both paths have to be stoppable.
            pid_file=None if once else pid_path,
        )
        if once:
            return _run_once(service, log_path)
        # In multi-loop mode run DaemonService. `dry_run` used to stop at this
        # branch: only the `once` path above ever consulted it, so
        # `daemon start --dry-run` (without --once) POSTed real webhooks. It has
        # to reach the service, which owns the dispatcher.
        service.run_forever()
        return {"stopped": True}
    finally:
        if once:
            _release_pid_file(pid_path, ONCE_PID_SENTINEL)
        else:
            # Defensive: the service already removed its own pid file on the way
            # out. Only touch it if it still names this process.
            _release_pid_file(pid_path, str(os.getpid()))


def stop_daemon(path: str | None = None) -> dict:
    """Signal the running daemon and report the *verified* outcome.

    Sending SIGTERM and immediately reporting success is how a daemon wedged in
    a webhook retry gets declared stopped while it keeps running.
    """
    config = load_daemon_config(path)
    pid_path = Path(config["pid_file"]).expanduser()

    pid = read_pid_file(pid_path)
    if pid is None:
        if pid_path.exists():
            # Present but unparseable (or the `--once` sentinel): clear it.
            pid_path.unlink(missing_ok=True)
            return {
                "stopped": False,
                "reason": "invalid_pid",
                "pid_file": str(pid_path),
            }
        return {
            "stopped": False,
            "reason": "pid_file_missing",
            "pid_file": str(pid_path),
        }

    if not _is_tahuti_pid(pid):
        pid_path.unlink(missing_ok=True)
        return {
            "stopped": False,
            "reason": "not_tahuti_process",
            "pid": pid,
            "pid_file": str(pid_path),
        }

    outcome = terminate_pid(pid)
    result: dict = {
        "pid": pid,
        "pid_file": str(pid_path),
        "escalated_to_sigkill": bool(outcome.get("escalated")),
    }
    if outcome.get("exited"):
        result["stopped"] = True
    else:
        result["stopped"] = False
        result["reason"] = (
            "signal_failed" if outcome.get("error") else "did_not_exit"
        )
        if outcome.get("error"):
            result["error"] = outcome["error"]
        # Still running: keep the pid file so the next `daemon stop` can find it.
        return result

    # Gone. Leave a pid file that no longer names the pid we stopped alone: a
    # `daemon start` may have replaced it while we were waiting.
    _release_pid_file(pid_path, str(pid))
    return result
