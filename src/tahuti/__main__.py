"""CLI entry-point for ``mb`` / ``python -m tahuti``.

Exit-code contract
------------------
Every ``cmd_*`` handler returns an ``int``, and ``main`` turns it straight into
the process status (``raise SystemExit(args.func(args))``). The payload and the
exit code must agree: a non-zero exit never leaves its failure described only
*inside* an ``ok: true`` envelope.

``0``
    Success — the operation did what was asked.
``1``
    Operational failure the caller should react to: auth or network trouble, a
    task that could not be resolved, a mutation the server rejected, a stop
    request that stopped nothing.
``2``
    Usage error. Owned entirely by ``argparse`` — ``add_subparsers(required=
    True)`` already exits 2 for a missing or unknown command — so no handler
    returns it.
``3``
    "Not running". Only ``daemon status``: the query itself succeeded and its
    answer is "there is no daemon", which a supervisor must be able to tell
    apart from "the status call broke" (which is ``1``). Mirrors
    ``systemctl is-active``.
``130``
    Interrupted — the user pressed Ctrl-C. Reported rather than dumped as a
    traceback so a caller can distinguish "someone cancelled this" from "this
    broke". No handler returns it; only ``main`` does.

Client methods signal failure inconsistently — some raise, some return a bare
``False`` (``MNNHubClient.mark_read``), and some return a *truthy*
``{"error": ...}`` dict (``ManageBacClient.get_task_detail``). Each handler
translates whichever it got into the contract above, so a guard written as
``if not result:`` is not sufficient for the error-dict case.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .auth import apply_authenticated, build_client, hub_client, session_email
from .client import ManageBacClient, parse_task_url, task_id_from_target
from . import __version__
from . import config
from . import keychain
from .config import (
    all_creds_paths,
    SNAPSHOT_FILENAME,
    clear_creds,
    clear_session,
    config_dir,
    COOKIE_ENV,
    COOKIE_ENV_LEGACY,
    creds_paths,
    env_value,
    load_state,
    own_state_refusal,
    PASSWORD_ENV,
    PASSWORD_ENV_LEGACY,
    purge_profiles,
    resolve_creds_path,
    save_profile,
    save_session,
    warn_on_weak_permissions,
)
from .daemon import (
    DaemonConfig,
    DaemonService,
    DEFAULT_WEBHOOK_URL,
    ServiceManager,
    WebhookDispatcher,
    configure_webhook,
    load_daemon_config,
    start_loop,
    stop_daemon,
)
from .daemon import _resolve_secret
from .exceptions import CommandError
from .filters import (
    classify_task_view,
    find_task_by_id,
    matches_subject,
    result_views,
    summary_of,
)
from .formatters import error, ok, print_payload

log = logging.getLogger(__name__)

# Exit-code contract — see the module docstring for the reasoning.
EXIT_OK = 0
# 1 is the catch-all operational failure. 2 is deliberately absent: argparse
# owns it (`add_subparsers(required=True)`) and never hands a handler a chance
# to return it.
EXIT_FAILURE = 1
# Only `daemon status`: "there is no daemon", as distinct from "the status call
# itself failed" (EXIT_FAILURE).
EXIT_NOT_RUNNING = 3
# Ctrl-C. 128 + SIGINT, so a shell sees the same status it would from any other
# interrupted process and can tell a cancellation from a failure.
EXIT_INTERRUPTED = 130


# ── Client helpers ──────────────────────────────────────────────────────


def _stdin_is_interactive() -> bool:
    """Whether a human is present to answer a prompt.

    Same defensive shape as ``resolve_format`` in :mod:`tahuti.formatters`: a
    closed or replaced stdin must degrade to "non-interactive" rather than
    raise, because the daemon and every CI caller reach the code this guards.
    """
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _prompt_login_setup(args) -> None:
    """Walk a fresh device through domain, school and email, in that order.

    Asks about what is still unknown, so a configured machine is not
    interrogated and a new one is walked through instead of failing on a flag
    nobody knew it needed. The password prompt stays in :func:`_build_client`
    and runs last, which puts the credential questions in the order they are
    actually used.

    The domain obeys exactly the same rule as school and email. It used to be
    exempted — "always confirm it, because it always has a value" — on the
    grounds that ``ProfileConfig.domain`` and ``SessionConfig.domain`` defaulted
    to ``"managebac.com"`` and so a domain could never be *absent*. That
    reasoning made the exemption self-fulfilling: the default was the reason the
    question could not be skipped, and the question was the reason nobody
    noticed the default was doing the answering. Both dataclass fields default
    to ``None`` now, ``load_state`` reads the config key without a fallback, and
    :func:`auth.build_client` is the one place the string is substituted — so
    "nothing supplies a domain" is a state this function can actually observe,
    and a ``managebac.cn`` operator is asked once, on the device that does not
    know yet, rather than on every login thereafter.

    Deliberately gated on ``login``: every other command funnels through
    ``_build_client`` too, and prompting there would stall ``list``/``submit``
    in scripts and hang the detached daemon. Non-interactive stdin returns
    immediately, leaving the existing silent resolution — and its
    ``missing_credentials`` errors — in charge.
    """
    if not _stdin_is_interactive() or args.cookie:
        return
    try:
        state = load_state(args.profile, args.config, args.session_file)
    except Exception:
        # An absent or unreadable state file must not become a traceback on the
        # way to a prompt; treat it as "nothing has been saved yet".
        state = None
    profile = getattr(state, "profile", None)
    session = getattr(state, "session", None)

    # Same shape as the school and email rules below: `--domain` is the
    # override, and the question is asked only when neither the profile nor the
    # session supplies one. Reaching this branch at all means every source came
    # back empty, so the value on screen is the built-in default rather than a
    # saved one — an empty answer adopts it, which is one keystroke instead of a
    # flag you have to remember exists.
    if not (
        args.domain
        or getattr(profile, "domain", None)
        or getattr(session, "domain", None)
    ):
        current_domain = "managebac.com"
        args.domain = (
            input(f"Base domain [{current_domain}]: ").strip() or current_domain
        )

    if not (
        args.school
        or getattr(profile, "school", None)
        or getattr(session, "school", None)
    ):
        # Nothing sensible can be defaulted here — it is the hostname — so keep
        # asking rather than handing an empty string to the URL builder.
        while not args.school:
            args.school = input("School subdomain (e.g. myschool): ").strip()

    if not (
        args.email
        or getattr(profile, "email", None)
        or getattr(session, "email", None)
    ):
        args.email = input("Email: ").strip() or args.email


def _build_client(args, command: str) -> tuple:
    """CLI wrapper: maps argparse namespace to :func:`auth.build_client`."""
    # `command` exists for exactly this: the interactive setup belongs to
    # `login`, not to the fifteen other commands that share this builder.
    if command == "login":
        _prompt_login_setup(args)
    password = getattr(args, "password", None)
    cookie = args.cookie
    if not password and not cookie:
        # Environment fallback for non-interactive/CI use. `tahuti daemon start -b`
        # already hands these to the detached child, so reading them back closes
        # the loop: `MANAGEBAC_PASSWORD=... tahuti daemon run` needs no prompt.
        # An explicit --password/--cookie still wins over the environment.
        password = env_value(PASSWORD_ENV, PASSWORD_ENV_LEGACY)
        cookie = env_value(COOKIE_ENV, COOKIE_ENV_LEGACY)
        if not password and not cookie:
            state = load_state(args.profile, args.config, args.session_file)
            if not state.session.cookie or getattr(args, "reauth", False):
                password = getpass.getpass("ManageBac password: ")
    verify = not getattr(args, "no_verify_tls", False)
    # `--no-remember-me` is the only thing that reaches ManageBac; where a kept
    # password lands is `--keychain`'s business, and whether it is kept at all is
    # `--keep-credentials`'s. Neither has any say over the session file, which
    # `build_client` writes because that is the persistent session.
    return build_client(
        school=args.school,
        domain=args.domain,
        email=args.email,
        password=password,
        cookie=cookie,
        profile=args.profile,
        refresh=getattr(args, "refresh", False),
        reauth=getattr(args, "reauth", False),
        verify=verify,
        cache_ttl=getattr(args, "cache_ttl", None),
        retry=getattr(args, "retry", 3),
        remember_me=_remember_me(args),
        keep_credentials=bool(getattr(args, "keep_credentials", False)),
        use_keychain=getattr(args, "keychain", None),
    )


def _remember_me(args) -> bool | None:
    """What to tell ManageBac about ``remember_me`` for this invocation.

    ``None`` — omit the field — only comes from ``login --no-remember-me``. The
    flag exists on no other command, so every other invocation keeps sending
    ``remember_me=1`` exactly as it always has.
    """
    if bool(getattr(args, "no_remember_me", False)):
        return None
    return True


def _authenticate_client(state, client, email: str) -> str:
    """Record in memory what this client authenticated as, for the payload.

    Deliberately writes nothing: :func:`auth.build_client` owns every on-disk
    write. This function used to save the profile and session itself, behind a
    ``persist`` switch whose only purpose was to override the policy
    ``build_client`` had already applied — and ``_relogin_from_creds`` saved
    unconditionally, so the two owners disagreed and the flag lost.
    """
    return apply_authenticated(state, client, email)


def _cache_dir_for_email(email: str | None) -> Path:
    """The response-cache directory that belongs to *email*.

    ``auth.build_client`` keys the cache on the first 16 hex digits of the
    login email's SHA-256, so this is the only directory that login ever reads
    or writes for that account. ``logout`` must resolve the same one, or it
    deletes a different profile's cached pages (grade data and the MNN-hub
    JWT) while leaving the ones it meant to clear in place.
    """
    from .cache import DEFAULT_CACHE_DIR

    if not email:
        return DEFAULT_CACHE_DIR
    email_hash = hashlib.sha256(email.encode()).hexdigest()[:16]
    return DEFAULT_CACHE_DIR / email_hash


def _login_email(state) -> str | None:
    """Resolve the account a login acted on, so ``logout`` can undo it.

    Delegates to ``auth.session_email`` rather than restating the precedence
    rule. The rule has to live in exactly one place because two things are keyed
    by it — the response-cache directory is a hash of it, and the OS-keychain
    item is filed under it. When ``build_client`` preferred an explicit
    ``--email``, then the profile's, then the session's, while this read the
    session's first, a profile whose two fields disagreed made ``logout`` delete
    a *different* profile's hash directory and leave the JWT-bearing entries in
    place, while still reporting success.

    ``logout`` passes no override (its subparser defines no ``--email``), which
    is why delegating is safe: the two agree on ``profile.email or
    session.email``. Returns ``None`` rather than ``""`` for the neither-set
    case, which is what the `if email:` guards below were written against.
    """
    return session_email(state) or None


DEFAULT_SNAPSHOT_PATH = config_dir() / SNAPSHOT_FILENAME


def load_snapshot(path: Path) -> dict:
    if not path.exists():
        return {"upcoming": [], "past": [], "overdue": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"upcoming": [], "past": [], "overdue": []}


def save_snapshot(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _harden_dir(path.parent)
        # Write to a temp file in the same directory, restrict permissions,
        # then atomically replace — avoids a world-readable window entirely.
        import tempfile
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".snapshot_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as e:
        log.warning("Failed to save snapshot: %s", e)


def _harden_dir(path: Path) -> None:
    """Best-effort restrict a directory to the current user."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _snapshot_path(state) -> Path:
    """Return the snapshot path that belongs to *state*'s config directory.

    Delegates to :func:`tahuti.config.snapshot_path` rather than restating the
    rule, because the submit containment check derives the very same path to
    refuse it and the MCP server reads it. See that function for the full
    reason the filename has to be stated once.

    Kept as a private name because this module's call sites all read
    ``_snapshot_path(state)``; the public spelling is the one to import.

    Called through the module rather than imported by name: ten functions below
    bind ``snapshot_path`` as a parameter or a local, so a module-level function
    of that name would be compiled as a local inside each of them and raise
    ``UnboundLocalError`` the first time one of them called it.
    """
    return config.snapshot_path(state)


def _set_submission_state(
    snapshot_path: Path,
    task_id: str,
    submitted: bool,
    client: ManageBacClient | None = None,
) -> None:
    """Force a task's submission state in the local snapshot and persist it.

    ``submitted`` and the presence of a submit button are two views of the
    same fact, so they are always written together — a snapshot with
    ``status="submitted"`` but a live submit button would re-offer the upload.

    *client* is forwarded so a status change invalidates the task's cached
    detail pages, exactly as a crawl-detected change would.
    """
    snapshot = load_snapshot(snapshot_path)
    task = find_task_by_id(snapshot, task_id)
    if not task:
        return
    task["status"] = "submitted" if submitted else "not-submitted"
    task["has_submit_button"] = not submitted
    update_snapshot_with_class_tasks(snapshot_path, [task], client=client)


def _redact_daemon_config(config: dict) -> dict:
    """Return a copy of a daemon config safe to echo into CLI/MCP output.

    The HMAC secret must never appear in stdout, an --output file, or a
    journal, since those are far less protected than the config file itself.
    """
    redacted = copy.deepcopy(config)
    for wh in redacted.get("webhooks") or []:
        if isinstance(wh, dict) and wh.get("secret"):
            wh["secret"] = "***redacted***"
    delivery = redacted.get("delivery")
    if isinstance(delivery, dict) and delivery.get("secret"):
        delivery["secret"] = "***redacted***"
    return redacted


def merge_snapshot(old: dict, new: dict, client=None, partial: bool = False) -> dict:
    """Merge new crawl results into the old snapshot.

    1. Tasks present in new crawl overwrite those in the old snapshot.
    2. Tasks present in old snapshot but missing in the new crawl are preserved,
       and marked with "deleted_from_server": True — but only when the crawl
       actually covered the whole account. *partial* marks a crawl that was
       deliberately narrowed (``list --pages N``): tasks missing from it are
       simply not on the pages we asked for, and inferring a deletion from that
       would flag most of the account as deleted and persist it, hiding them
       from every later ``list`` until a full crawl happened to reach them.
    3. If client is provided, invalidate cache for task details if grade or status changes.
    """
    merged_map = {}

    # Determine reference datetime for date classifications
    now_ref = datetime.now()
    crawled_at_str = new.get("crawled_at") or old.get("crawled_at")
    if crawled_at_str:
        try:
            now_ref = datetime.fromisoformat(crawled_at_str)
        except Exception:
            pass

    # helper to build map from snapshot sections
    for section in ("upcoming", "past", "overdue"):
        for t in old.get(section, []):
            tid = t.get("id")
            if tid:
                merged_map[tid] = t

    # Update with new results
    new_tids = set()
    for section in ("upcoming", "past", "overdue"):
        for t in new.get(section, []):
            tid = t.get("id")
            if tid:
                new_tids.add(tid)
                old_t = merged_map.get(tid)
                if old_t:
                    # Opportunistic cache invalidation
                    # Check if grade or status/labels changed
                    grade_changed = old_t.get("grade_letter") != t.get("grade_letter") or old_t.get("grade_score") != t.get("grade_score")
                    old_labels = old_t.get("labels") or []
                    new_labels = t.get("labels") or []
                    labels_changed = set(old_labels) != set(new_labels) or old_t.get("status") != t.get("status")

                    if (grade_changed or labels_changed) and client:
                        class_link = t.get("link") or ""
                        cid, task_id = parse_task_url(class_link)
                        if cid and task_id:
                            detail_url = f"{client.base}/student/classes/{cid}/core_tasks/{task_id}"
                            hint_url = f"{client.base}/student/classes/{cid}/events/{task_id}/hint"
                            dropbox_url = f"{client.base}/student/classes/{cid}/core_tasks/{task_id}/dropbox"
                            client.cache.invalidate(detail_url)
                            client.cache.invalidate(hint_url)
                            client.cache.invalidate(dropbox_url)
                            log.info("Task %s state changed; invalidated cached details.", task_id)
                merged_map[tid] = t

    # Mark tasks in snapshot that were NOT in the new crawl as deleted from
    # server — but only when this crawl saw the whole account. A `--pages`-
    # limited crawl is silent about everything past its last page, so treating
    # absence there as a confirmed deletion deletes the rest of the account.
    if not partial:
        for tid, t in merged_map.items():
            if tid not in new_tids:
                t["deleted_from_server"] = True

    # Reclassify all merged tasks into upcoming, past, overdue based on due_date and status
    reclassified = _reclassify_tasks(merged_map, now_ref=now_ref)

    return {
        "student_name": new.get("student_name") or old.get("student_name"),
        "school": new.get("school") or old.get("school"),
        "base_url": new.get("base_url") or old.get("base_url"),
        "crawled_at": new.get("crawled_at") or old.get("crawled_at"),
        "upcoming": reclassified["upcoming"],
        "past": reclassified["past"],
        "overdue": reclassified["overdue"],
    }


def _reclassify_tasks(
    tasks: dict[str, dict] | list[dict], now_ref: datetime | None = None
) -> dict[str, list[dict]]:
    """Reclassify tasks into upcoming, past, overdue based on due_date and status."""
    upcoming = []
    past = []
    overdue = []

    task_list = tasks.values() if isinstance(tasks, dict) else tasks
    for t in task_list:
        view = classify_task_view(t, now_ref=now_ref)
        t["view"] = view
        if view == "upcoming":
            upcoming.append(t)
        elif view == "overdue":
            overdue.append(t)
        else:
            past.append(t)

    return {
        "upcoming": upcoming,
        "past": past,
        "overdue": overdue,
    }


def update_snapshot_with_class_tasks(
    snapshot_path: Path,
    class_tasks: list[dict],
    client: ManageBacClient | None = None,
) -> dict:
    """Update snapshot in-place with freshly fetched tasks for a class.

    Merges updated tasks into the snapshot and reclassifies them into
    upcoming, past, and overdue.
    """
    old_snapshot = load_snapshot(snapshot_path)
    merged_map = {}

    for section in ("upcoming", "past", "overdue"):
        for t in old_snapshot.get(section, []):
            tid = t.get("id")
            if tid:
                merged_map[tid] = t

    for t in class_tasks:
        tid = t.get("id")
        if tid:
            old_t = merged_map.get(tid)
            if old_t:
                grade_changed = (
                    old_t.get("grade_letter") != t.get("grade_letter")
                    or old_t.get("grade_score") != t.get("grade_score")
                )
                old_labels = old_t.get("labels") or []
                new_labels = t.get("labels") or []
                labels_changed = (
                    set(old_labels) != set(new_labels)
                    or old_t.get("status") != t.get("status")
                )
                if (grade_changed or labels_changed) and client:
                    class_link = t.get("link") or old_t.get("link") or ""
                    cid, task_id = parse_task_url(class_link)
                    if cid and task_id:
                        client.invalidate_task_cache(cid, task_id)
                        log.info("Task %s state changed; invalidated cached details.", task_id)

                if not t.get("class_name") and old_t.get("class_name"):
                    t["class_name"] = old_t["class_name"]
            merged_map[tid] = t

    reclassified = _reclassify_tasks(merged_map, now_ref=datetime.now())

    base_url = old_snapshot.get("base_url")
    if not base_url and client and hasattr(client, "base") and isinstance(client.base, str):
        base_url = client.base

    updated = {
        "student_name": old_snapshot.get("student_name"),
        "school": old_snapshot.get("school"),
        "base_url": base_url,
        "crawled_at": old_snapshot.get("crawled_at") or datetime.now().isoformat(),
        "upcoming": reclassified["upcoming"],
        "past": reclassified["past"],
        "overdue": reclassified["overdue"],
    }
    save_snapshot(snapshot_path, updated)
    return updated


# ── Commands ────────────────────────────────────────────────────────────


def cmd_login(args) -> int:
    # What the user is told afterwards is what actually happened, so each field
    # is read from the same flag `_build_client` passed down rather than
    # restated here. `remember_me: null` means the field was left out of the
    # POST; `credentials_saved: false` is the default and is the whole point of
    # the redesign — the session is saved so you are not asked again, the
    # password is not saved unless you ask for it.
    remember_me = _remember_me(args)
    keep_credentials = bool(getattr(args, "keep_credentials", False))
    state, client, email = _build_client(args, "login")
    email = _authenticate_client(state, client, email)
    payload = ok(
        "login",
        state.active_profile,
        {
            "school": client.school,
            "domain": client.domain,
            "email": email,
            "base_url": client.base,
            "auth_method": "cookie" if args.cookie else "password",
            "remember_me": remember_me,
            # Policy, not outcome: a kept password lands in the OS keychain when
            # one is selected and available, and in `creds.<profile>.json`
            # otherwise. Naming the backend here would mean re-deriving
            # `_store_password`'s fallback, and the one thing worth reporting
            # honestly is that the password was kept at all.
            "credentials_saved": keep_credentials and not args.cookie,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_list(args) -> int:
    state, client, email = _build_client(args, "list")
    _authenticate_client(state, client, email)

    pages = args.pages or state.profile.default_pages
    details = (
        args.details if args.details is not None else state.profile.default_details
    )
    view = args.view or state.profile.default_view
    subject = args.subject or state.profile.default_subject or None

    from .filters import filter_result_by_subject, filter_result_by_status

    # Load local snapshot
    snapshot_path = _snapshot_path(state)
    old_snapshot = load_snapshot(snapshot_path)

    # Check if we can reuse the snapshot, using the same TTL that governs the
    # HTTP response cache. This used to be a hardcoded 900, so `--cache-ttl 30`
    # (and `defaults.cache_ttl`) were accepted and then ignored here: five
    # minutes after a full crawl `list` still re-rendered the stale snapshot.
    #
    # There is deliberately no third fallback below. `load_state` is the only
    # way a ProfileConfig is built, and it passes `default_cache_ttl` through
    # `config._coerce_cache_ttl`, which substitutes `DEFAULT_CACHE_TTL` for
    # anything non-numeric — a null config key included. The field's own
    # default is that same constant, so a None TTL cannot reach this line, and
    # a `DEFAULT_SNAPSHOT_TTL` sitting here as a belt-and-braces default was
    # unreachable and a fourth spelling of 900.
    snapshot_ttl = args.cache_ttl
    if snapshot_ttl is None:
        snapshot_ttl = state.profile.default_cache_ttl

    use_cached_snapshot = False
    if old_snapshot and not args.refresh:
        crawled_at_str = old_snapshot.get("crawled_at")
        if crawled_at_str:
            try:
                crawled_at = datetime.fromisoformat(crawled_at_str)
                age = (datetime.now() - crawled_at).total_seconds()
                if age < snapshot_ttl:
                    use_cached_snapshot = True
                    log.info(
                        "Using cached snapshot (age: %d seconds, ttl: %d)",
                        int(age),
                        snapshot_ttl,
                    )
            except Exception:
                pass

    if use_cached_snapshot:
        merged_result = old_snapshot
    else:
        # Fetch fresh results. An explicit --pages narrows the crawl, so the
        # merge must not read absence from it as a deletion.
        partial = args.pages is not None
        # `fetch_notifications=False`: `merge_snapshot` returns only the seven
        # task/identity keys and drops `notifications`, and the result dict
        # built below names its keys explicitly, so nothing downstream of here
        # reads it.  The three MNN-hub requests were being made and discarded on
        # every cold `list` run.  `tahuti notifications` is the command that
        # surfaces them, and it fetches them itself.
        new_result = client.crawl_all(
            max_pages=pages, fetch_details=details, fetch_notifications=False
        )
        # Merge with local snapshot and save
        merged_result = merge_snapshot(
            old_snapshot, new_result, client=client, partial=partial
        )
        save_snapshot(snapshot_path, merged_result)

    # Filter out tasks that were deleted from the server (unless --deleted is specified)
    show_deleted = getattr(args, "deleted", False)
    result = {
        "student_name": merged_result.get("student_name"),
        "school": merged_result.get("school"),
        "base_url": merged_result.get("base_url"),
        "crawled_at": merged_result.get("crawled_at"),
        "upcoming": [t for t in merged_result.get("upcoming", []) if show_deleted or not t.get("deleted_from_server")],
        "past": [t for t in merged_result.get("past", []) if show_deleted or not t.get("deleted_from_server")],
        "overdue": [t for t in merged_result.get("overdue", []) if show_deleted or not t.get("deleted_from_server")]
    }

    if subject:
        result = filter_result_by_subject(result, subject)

    # Apply status and tag filters (graded, submitted, grade, tag, completed)
    completed_val = None
    if args.completed:
        completed_val = True
    elif args.todo:
        completed_val = False

    if (
        args.graded is not None
        or args.submitted is not None
        or args.grade is not None
        or args.tag is not None
        or completed_val is not None
    ):
        result = filter_result_by_status(
            result,
            graded=args.graded,
            submitted=args.submitted,
            grade=args.grade,
            tag=args.tag,
            completed=completed_val,
        )

    views = result_views(result, view)
    summary = summary_of(views)
    payload = ok(
        "list",
        state.active_profile,
        {
            "meta": {
                "student_name": result["student_name"],
                "school": result["school"],
                "domain": client.domain,
                "base_url": result["base_url"],
                "crawled_at": result["crawled_at"],
                "view": view,
                "subject_filter": subject,
                "graded_filter": args.graded,
                "submitted_filter": args.submitted,
                "grade_filter": args.grade,
                "tag_filter": args.tag,
                "todo_filter": args.todo,
                "completed_filter": args.completed,
                # Truthful: a payload served from the cached snapshot was not
                # detail-enriched by *this* run, so claiming `details: true`
                # there told consumers a detail fetch happened when none did.
                "details": bool(details) and not use_cached_snapshot,
                "snapshot_source": "cache" if use_cached_snapshot else "crawl",
            },
            "summary": summary,
            "tasks": views,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_view(args) -> int:
    state, client, email = _build_client(args, "view")
    _authenticate_client(state, client, email)

    target = args.target or args.id or args.url
    task = None
    detail = None

    if not target:
        payload = error("view", "missing_target", "Provide a task id or task url")
        print_payload(payload, args.output, args.format)
        return 1

    # One derivation of "read a task id from this target", shared with the MCP
    # tools through `client.task_id_from_target`.  The gate used to be
    # `startswith("http")` OR `"/core_tasks/" in target`, which admitted *any*
    # URL and then split it on a separator it did not contain — so a class URL
    # or an unrelated link produced the whole string as the "id" and a detail
    # fetch against it, while the MCP tool refused the same input.  Two
    # derivations of one rule with opposite acceptance.
    #
    # The branch still keys off "/core_tasks/" because that is what decides
    # whether the target already names a detail page: a URL is fetched verbatim
    # and only borrows the snapshot's metadata, whereas a bare id has to find a
    # link before there is anything to fetch.  Those are genuinely different
    # fetches, so the branches stay; only the reading of the id is shared.
    try:
        task_id = task_id_from_target(target)
    except CommandError as exc:
        payload = error("view", exc.code, exc.message)
        print_payload(payload, args.output, args.format)
        return 1

    if "/core_tasks/" in target:
        # Search local snapshot first to populate standard fields
        snapshot_path = _snapshot_path(state)
        snapshot = load_snapshot(snapshot_path)
        task = find_task_by_id(snapshot, task_id)

        detail = client.get_task_detail(target, bypass_cache=args.refresh)
        if not task:
            task = {"id": task_id, "link": target}
    else:
        # 1. Search local snapshot first
        snapshot_path = _snapshot_path(state)
        snapshot = load_snapshot(snapshot_path)
        task = find_task_by_id(snapshot, task_id)

        # 2. Fall back to sequential web crawl scan if not found
        if not task:
            log.info("Task %s not found in local snapshot. Performing sequential web crawl fallback...", task_id)
            fallback_task = client.find_task_by_id(task_id, max_pages=args.pages or 20)
            if isinstance(fallback_task, dict):
                task = fallback_task

        if not task:
            payload = error("view", "task_not_found", f"No task found for id {task_id}")
            print_payload(payload, args.output, args.format)
            return 1
        if task.get("link"):
            detail = client.get_task_detail(task["link"], from_hint=False, bypass_cache=args.refresh)
        else:
            detail = {}

    # `get_task_detail` reports a fetch failure by returning a *truthy*
    # `{"error": ...}` dict rather than by raising, so without this check the
    # success envelope below would nest that error inside `ok: true` and exit 0.
    if isinstance(detail, dict) and detail.get("error"):
        payload = error("view", "detail_fetch_failed", str(detail["error"]))
        print_payload(payload, args.output, args.format)
        return EXIT_FAILURE

    # Merge parsed card details from detail page back into task metadata
    if detail and isinstance(detail, dict):
        for k, dest_key in (
            ("grade_letter", "grade_letter"),
            ("grade_score", "grade_score"),
            ("status", "status"),
            ("labels", "labels"),
            ("has_submit_button", "has_submit_button")
        ):
            if k in detail and task.get(dest_key) is None:
                task[dest_key] = detail[k]

    # `--subject` narrows an id lookup to the class it is meant to belong to:
    # resolving an id that turns out to live under a different class is a
    # mismatch the caller asked us to rule out, so say so instead of showing
    # the wrong task's detail page.
    subject = getattr(args, "subject", None)
    if subject and not matches_subject(task, subject):
        payload = error(
            "view",
            "subject_mismatch",
            f"Task {task_id} is not in a class matching {subject!r}",
        )
        print_payload(payload, args.output, args.format)
        return 1

    payload = ok(
        "view",
        state.active_profile,
        {
            "task": task,
            "detail": detail,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def _reject_purge_with_keep_credentials(args) -> None:
    """Refuse ``--purge --keep-credentials`` instead of silently doing one.

    The two flags ask for opposite things: ``--purge`` deletes the profile's
    password file(s), its keychain entry *and* its entire ``config.json`` entry,
    while ``--keep-credentials`` asks for the password to survive so the next
    command can log in silently. Picking a winner would leave the operator
    believing they had asked for the other one — and the dangerous direction is
    the silent one, where a password outlives a logout the user thought was
    complete. Refusing costs one re-run.
    """
    if not (getattr(args, "purge", False) and getattr(args, "keep_credentials", False)):
        return
    raise CommandError(
        "conflicting_flags",
        "--purge and --keep-credentials contradict each other: --purge deletes "
        "this profile's password file, its keychain entry and its entire "
        "config.json entry, while --keep-credentials asks for the password to "
        "be kept for silent re-login. Pass one or the other.",
    )


def cmd_logout(args) -> int:
    # Refuse before anything is cleared: a contradiction discovered halfway
    # through would leave the session gone and the credentials half-kept.
    _reject_purge_with_keep_credentials(args)

    state = load_state(args.profile, args.config, args.session_file)
    clear_session(state, all_profiles=args.all)

    # `logout` must actually mean logout: the response cache holds full grade
    # pages and the MNN-hub Bearer JWT, which would otherwise survive.
    cache_cleared = None
    if not getattr(args, "keep_cache", False):
        try:
            from .cache import ResponseCache

            cache_cleared = ResponseCache(
                cache_dir=_cache_dir_for_email(_login_email(state))
            ).clear()
        except Exception as e:
            log.warning("Failed to clear response cache on logout: %s", e)

    # `logout` must mean logout for the password too. Leaving creds.json behind
    # would keep the cleartext password on disk after the user asked to be
    # logged out, so it goes by default; --keep-credentials opts back into
    # silent re-login for users who find the prompt more annoying than the risk.
    #
    # The files cleared are the ones *this profile* authenticates from — its own
    # `creds.<profile>.json`, plus the pre-per-profile global `creds.json` when
    # this profile has none of its own and would otherwise read that. Clearing
    # another profile's file would be the bug per-profile credentials exists to
    # remove; leaving the fallback behind would let the next command silently
    # re-login from a password the user just asked to delete.
    creds_removed = False
    keychain_removed = False
    cleared_paths: list[str] = []
    if not getattr(args, "keep_credentials", False):
        targets = (
            all_creds_paths() if args.all else creds_paths(profile=state.active_profile)
        )
        for path in targets:
            if clear_creds(path):
                creds_removed = True
                cleared_paths.append(str(path))
        email = _login_email(state)
        if email:
            keychain_removed = keychain.delete(email)

    # The half of "logout" that used to be missing: the session, the cache and
    # the password went, but the `profiles.<name>` entry in config.json stayed —
    # so the machine still knew which school it belonged to and the next command
    # silently re-authenticated against it. `--purge` removes that entry too,
    # wholesale (school, domain, email and the `defaults` block), which is what
    # makes the profile genuinely cease to exist rather than merely go quiet.
    #
    # `--all` purges every profile's entry; what is left is an empty `profiles`
    # map with no `active_profile`, which `load_state` already tolerates.
    purged_profiles = (
        purge_profiles(state, all_profiles=args.all)
        if getattr(args, "purge", False)
        else []
    )

    payload = ok(
        "logout",
        state.active_profile,
        {
            "logged_out": True,
            "all_profiles": args.all,
            "cache_entries_removed": cache_cleared,
            "credentials_removed": creds_removed,
            "credential_files_removed": cleared_paths,
            "keychain_entry_removed": keychain_removed,
            "credentials_kept": bool(getattr(args, "keep_credentials", False)),
            # New keys, so no existing key changes meaning for a script reading
            # this payload. `profile_purged` is the policy (was --purge asked
            # for); `profile_entry_removed` is the outcome (did a config.json
            # entry actually go); `profiles_purged` names them, which under
            # `--all` is the only way to tell which profiles existed.
            "profile_purged": bool(getattr(args, "purge", False)),
            "profile_entry_removed": bool(purged_profiles),
            "profiles_purged": purged_profiles,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def _reject_channel_delivery(args) -> None:
    """Refuse `--channel-id`/`--recipient` instead of silently not delivering.

    Channel-send delivery has no implementation anywhere: nothing invokes a
    zeroclaw binary, and ``DaemonConfig`` has no ``delivery`` field, so
    ``from_dict`` never reads the ``delivery.mode`` these flags write. A run
    that asked for QQ delivery therefore fell through to
    ``delivery.webhook_url or DEFAULT_WEBHOOK_URL`` and POSTed the alert payload
    to ``http://127.0.0.1:42617/webhook`` with no QQ message and no error.

    Failing loudly is the honest option: the caller asked for a transport that
    does not exist, and a silent localhost POST is worse than a refusal.
    """
    channel_id = getattr(args, "channel_id", None)
    recipient = getattr(args, "recipient", None)
    if not (channel_id and recipient):
        return
    raise CommandError(
        "channel_delivery_not_implemented",
        "Channel-send delivery is not implemented: no zeroclaw binary is "
        "invoked and daemon configs carry no channel transport, so "
        f"--channel-id {channel_id!r} would silently fall back to the HTTP "
        f"webhook (default {DEFAULT_WEBHOOK_URL}). Use --webhook-url to deliver "
        "over HTTP.",
    )


def _apply_daemon_overrides(daemon_config: dict, args) -> dict:
    """Fold the shared daemon CLI flags into a daemon config dict, in place.

    ``daemon run`` and ``daemon start`` expose the same delivery, interval and
    active-window settings, so both resolve them through here. Sharing one code
    path is the only thing that stops the two sibling commands from drifting
    into meaning different things by the same flag name — which is exactly how
    ``--interval`` ended up writing a key nothing reads.
    """
    if getattr(args, "webhook_url", None):
        daemon_config["webhooks"] = [
            {
                "url": args.webhook_url,
                "secret": _resolve_secret(getattr(args, "secret", None)),
                "events": ["*"],
                "enabled": True,
            }
        ]
        daemon_config["delivery"] = {"mode": "webhook", "webhook_url": args.webhook_url}

    # Channel delivery is refused before it can be written: a `delivery` dict
    # with mode "channel_send" is read by nothing, so writing it here is exactly
    # the silent no-op this guard exists to prevent.
    _reject_channel_delivery(args)

    # `run` spells this `--poll-interval`, `start` spells it `--interval`; both
    # names are accepted on both commands, but the config key is the one
    # DaemonConfig.from_dict actually reads.
    interval = getattr(args, "poll_interval", None)
    if interval is None:
        interval = getattr(args, "interval", None)
    if interval is not None:
        daemon_config["poll_interval_seconds"] = interval
    daemon_config.pop("interval", None)

    # Active hours are stored as `active_windows`, matching what
    # load_daemon_config() derives from the same keys in daemon.json.
    hours_start = getattr(args, "active_hours_start", None)
    hours_end = getattr(args, "active_hours_end", None)
    if hours_start is not None or hours_end is not None:
        start = 7 if hours_start is None else hours_start
        end = 23 if hours_end is None else hours_end
        daemon_config["active_windows"] = [[f"{start:02d}:00", f"{end:02d}:00"]]
    daemon_config.pop("active_hours_start", None)
    daemon_config.pop("active_hours_end", None)
    return daemon_config


def _error_payload(command: str, profile: str, code: str, message: str, data=None):
    """An ``ok:false`` payload that still carries the run's data.

    ``formatters.error`` has nowhere to put the alerts/summary a failed daemon
    cycle computed, so the choice used to be "report failure and throw the
    evidence away" or "report success". Neither is acceptable: a shell caller
    needs a non-zero exit *and* the payload needs the machine-readable
    ``error.code`` alongside what actually happened.
    """
    payload = error(command, code, message)
    payload["profile"] = profile
    if data is not None:
        payload["data"] = data
    return payload


def _daemon_renewal_sources(args, state=None) -> tuple[bool, str]:
    """Whether the daemon can renew its own session, and how.

    Returns ``(can_renew, description)``. Three sources, in the order a daemon
    would use them: a password or cookie handed to it for this run, then a
    password a previous ``tahuti login --keep-credentials`` left on disk or in
    the OS keychain.

    The daemon deliberately keeps no credential of its own — it is a long-lived
    process, and a copy of the password in its config file would outlive every
    reason to have it. The cost of that choice is visible here: with none of the
    three sources, the daemon works until the current cookie expires and then
    stops, and nothing in its output says why.
    """
    profile = getattr(state, "active_profile", None) or getattr(args, "profile", None)
    if getattr(args, "password", None) or getattr(args, "cookie", None):
        return True, "a credential passed on the command line"
    if env_value(PASSWORD_ENV, PASSWORD_ENV_LEGACY) or env_value(
        COOKIE_ENV, COOKIE_ENV_LEGACY
    ):
        return True, f"{PASSWORD_ENV}/{COOKIE_ENV} in the environment"
    from .auth import _load_creds

    try:
        email = session_email(state) if state is not None else None
        if _load_creds(email, profile=profile):
            return True, "a saved password from an earlier login"
    except Exception as exc:  # a broken/unreadable creds file is not a crash
        log.debug("Could not check for a saved password: %s", exc)
    return False, "nothing"


def _warn_if_daemon_cannot_renew(args, state=None) -> bool:
    """Say plainly, at startup, when the daemon will stop at the next expiry.

    Returns *True* when the warning was emitted. Written to stderr, not logged
    and not put in the payload: ``--format json`` stdout stays machine-readable,
    and this is a fact about the *next* few hours that a single log line at
    DEBUG would bury.
    """
    can_renew, source = _daemon_renewal_sources(args, state)
    if can_renew:
        return False
    profile = getattr(state, "active_profile", None) or getattr(args, "profile", None)
    print(
        f"warning: the daemon has no password and no saved credential for "
        f"profile {profile or 'default'!r} ({source}), so it cannot renew the "
        f"session when the cookie expires — it will stop working then instead "
        f"of recovering. Run `tahuti login --keep-credentials` once to store "
        f"the password, or pass --password/--cookie to `daemon start`.",
        file=sys.stderr,
    )
    return True


def cmd_daemon_run(args) -> int:
    _reject_channel_delivery(args)
    state, client, email = _build_client(args, "daemon")
    _authenticate_client(state, client, email)
    _warn_if_daemon_cannot_renew(args, state)
    daemon_config = load_daemon_config(getattr(args, "daemon_config", None))
    _apply_daemon_overrides(daemon_config, args)
    config = DaemonConfig.from_dict(daemon_config)

    def refresh_fn() -> bool:
        from .auth import refresh_session
        try:
            refresh_session(client, state)
            return True
        except Exception as err:
            # A missing password is the expected failure here, and its message
            # names the command that fixes it — so it is logged verbatim rather
            # than replaced by a generic "re-login failed".
            log.warning("Silent re-login failed: %s", err)
            return False

    service = DaemonService(
        client,
        config=config,
        auth_refresh_fn=refresh_fn,
        dry_run=getattr(args, "dry_run", False),
    )
    if getattr(args, "once", False):
        res = service.run_check_cycle()
        payload = ok("daemon.run", state.active_profile, res)
        print_payload(payload, args.output, args.format)
        return 0
    service.run_forever()
    return 0


def cmd_daemon_start(args) -> int:
    # Refuse before anything is spawned or configured: a detached child that
    # died on startup would still leave the parent reporting success.
    _reject_channel_delivery(args)
    if getattr(args, "background", False):
        mgr = ServiceManager(
            pid_path=getattr(args, "pid_file", None),
            log_path=getattr(args, "log_file", None),
        )
        extra_args = []
        # Secrets go to the child through its environment, never argv: argv is
        # readable by any local user via `ps` for the life of the daemon.
        daemon_secret_env: dict[str, str] = {}
        if getattr(args, "profile", None):
            extra_args.extend(["--profile", args.profile])
        if getattr(args, "config", None):
            extra_args.extend(["--config", args.config])
        if getattr(args, "session_file", None):
            extra_args.extend(["--session-file", args.session_file])
        if getattr(args, "school", None):
            extra_args.extend(["--school", args.school])
        # Falsy, not `is not None`: `--domain` has no argparse default, so it is
        # `None` when unset, and `["--domain", None]` would put the literal
        # string "None" in the detached child's argv — where it reaches
        # `_validate_school_domain` and fails as an unsupported domain. Leaving
        # the flag off lets the child resolve the domain itself from the profile
        # it loads, exactly as an interactive shell would.
        if getattr(args, "domain", None):
            extra_args.extend(["--domain", args.domain])
        if getattr(args, "email", None):
            extra_args.extend(["--email", args.email])
        if getattr(args, "password", None):
            daemon_secret_env[PASSWORD_ENV] = args.password
        if getattr(args, "cookie", None):
            daemon_secret_env[COOKIE_ENV] = args.cookie
        if getattr(args, "daemon_config", None):
            extra_args.extend(["--daemon-config", args.daemon_config])
        if getattr(args, "webhook_url", None):
            extra_args.extend(["--webhook-url", args.webhook_url])
        if getattr(args, "channel_id", None) and getattr(args, "recipient", None):
            extra_args.extend(["--channel-id", args.channel_id])
            extra_args.extend(["--recipient", args.recipient])
        if getattr(args, "secret", None):
            daemon_secret_env["MB_WEBHOOK_SECRET"] = args.secret
        interval = getattr(args, "interval", None)
        if interval is None:
            interval = getattr(args, "poll_interval", None)
        if interval is not None:
            extra_args.extend(["--poll-interval", str(interval)])
        if getattr(args, "active_hours_start", None) is not None:
            extra_args.extend(["--active-hours-start", str(args.active_hours_start)])
        if getattr(args, "active_hours_end", None) is not None:
            extra_args.extend(["--active-hours-end", str(args.active_hours_end)])
        # Without forwarding these the detached child would ignore them: `-b
        # --once` would loop forever and `-b --dry-run` would POST webhooks.
        if getattr(args, "once", False):
            extra_args.append("--once")
        if getattr(args, "dry_run", False):
            extra_args.append("--dry-run")
        if getattr(args, "no_verify_tls", False):
            extra_args.append("--no-verify-tls")

        # Before the spawn, because this is the moment the user can still act on
        # it. A detached child that silently dies at the next cookie expiry
        # leaves nothing to diagnose but a log file.
        _warn_if_daemon_cannot_renew(args)

        res = mgr.start_background(extra_args=extra_args, env=daemon_secret_env)
        payload = ok("daemon.start", getattr(args, "profile", "default") or "default", res)
        print_payload(payload, args.output, args.format)
        return 0 if res.get("started") else 1

    state, client, email = _build_client(args, "daemon")
    _authenticate_client(state, client, email)
    _warn_if_daemon_cannot_renew(args, state)
    daemon_config = load_daemon_config(args.daemon_config)
    _apply_daemon_overrides(daemon_config, args)
    once = bool(getattr(args, "once", False))
    dry_run = bool(getattr(args, "dry_run", False))
    result = start_loop(client, daemon_config, dry_run=dry_run, once=once)
    data = result | {"daemon": _redact_daemon_config(daemon_config)}
    if dry_run:
        data["delivered"] = False
        data["dry_run"] = True

    # `start_loop`'s `once` branch computes alerts, logs `delivered=False`
    # literally and returns before `DaemonService` — the only thing that owns a
    # `WebhookDispatcher` — is ever constructed. So `daemon start --once`
    # --webhook-url …` sent nothing while exiting 0 with `delivered: false`.
    # A dry run is *supposed* to deliver nothing; anything else that computed
    # alerts and delivered none has failed and must say so.
    alerts = result.get("alerts") or []
    undelivered = bool(alerts) and not result.get("delivered") and not dry_run
    if undelivered:
        payload = _error_payload(
            "daemon.start",
            state.active_profile,
            "delivery_failed",
            f"{len(alerts)} alert(s) were computed but nothing was delivered "
            "(no webhook was dispatched). Re-run with --webhook-url, or use "
            "`tahuti daemon run --once`, which dispatches through DaemonService.",
            data,
        )
        print_payload(payload, args.output, args.format)
        return 1
    payload = ok("daemon.start", state.active_profile, data)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_stop(args) -> int:
    # The pid file the *user* named, kept separate from whatever the fallback
    # consults below. `--pid-file` is honoured when a process really is running
    # there; only the not-running fallback cross-wires, and it used to report
    # the config's path as if it were the one that had been checked.
    requested_pid_file = getattr(args, "pid_file", None)
    mgr = ServiceManager(pid_path=requested_pid_file)
    result = mgr.stop_background()
    result["pid_file_requested"] = requested_pid_file
    if not result.get("stopped") and result.get("reason") == "not_running":
        # Fall back to legacy stop_daemon logic, which resolves daemon.json's
        # pid_file — a different file from the one the user asked about.
        result = stop_daemon(getattr(args, "daemon_config", None))
        result["pid_file_requested"] = requested_pid_file
        result["pid_file_fallback"] = result.get("pid_file")
    payload = ok("daemon.stop", "default", result)
    print_payload(payload, args.output, args.format)
    # `stop_background` reports "there was nothing to stop" in-band
    # (`stopped: false, reason: not_running|pid_file_missing|...`). A caller
    # doing stop-then-start must be able to see that the stop did not happen,
    # or it silently supervises two daemons at once.
    return EXIT_OK if result.get("stopped") else EXIT_FAILURE


def cmd_daemon_status(args) -> int:
    mgr = ServiceManager(
        pid_path=getattr(args, "pid_file", None),
        log_path=getattr(args, "log_file", None),
    )
    res = mgr.status()
    payload = ok("daemon.status", "default", res)
    print_payload(payload, args.output, args.format)
    # The status *query* succeeded either way, so the envelope stays `ok` and
    # `data.running` is the answer. The exit code carries it too, because
    # `systemctl is-active`-style callers need "no daemon" (3) to be distinct
    # from "the status call itself failed" (1).
    return EXIT_OK if res.get("running") else EXIT_NOT_RUNNING


def cmd_daemon_test_webhook(args) -> int:
    url = getattr(args, "url", None)
    secret = getattr(args, "secret", None)
    if not url:
        config = load_daemon_config(getattr(args, "daemon_config", None))
        webhooks = config.get("webhooks", [])
        if webhooks:
            url = webhooks[0].get("url")
            secret = secret or webhooks[0].get("secret")
        else:
            url = config.get("delivery", {}).get("webhook_url")
    if not url:
        raise CommandError("missing_argument", "No webhook URL provided or configured")
    dispatcher = WebhookDispatcher()
    res = dispatcher.test_ping(url, secret=secret)
    payload = ok("daemon.test-webhook", "default", res)
    print_payload(payload, args.output, args.format)
    return 0 if res.get("success") else 1


def cmd_daemon_install(args) -> int:
    mgr = ServiceManager(log_path=getattr(args, "log_file", None))
    res = mgr.install_service()
    payload = ok("daemon.install", "default", res)
    print_payload(payload, args.output, args.format)
    return 0 if res.get("installed") else 1


def cmd_daemon_uninstall(args) -> int:
    mgr = ServiceManager()
    res = mgr.uninstall_service()
    payload = ok("daemon.uninstall", "default", res)
    print_payload(payload, args.output, args.format)
    return 0 if res.get("uninstalled") else 1


def cmd_daemon_configure_webhook(args) -> int:
    config = configure_webhook(args.url, args.daemon_config)
    payload = ok("daemon.configure-webhook", "default", config)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_configure_channel(args) -> int:
    """Persist channel-send delivery — refused, because it does not exist.

    The old body called ``configure_channel_send``, which wrote
    ``daemon_config["delivery"] = {"mode": "channel_send", ...}`` and reported
    success. ``DaemonConfig`` has no ``delivery`` field and ``from_dict`` never
    reads it, and nothing in the tree invokes a zeroclaw binary — so the config
    it printed back was decoration, and the next ``daemon run`` POSTed the alert
    payload to the localhost webhook default with no error. Say so and fail
    rather than write a key nothing reads.
    """
    payload = error(
        "daemon.configure-channel",
        "channel_delivery_not_implemented",
        "Channel-send delivery is not implemented: no zeroclaw binary is "
        "invoked and daemon configs carry no channel transport, so "
        f"--channel {args.channel_id!r} -> {args.recipient!r} would write a "
        f"config key nothing reads and then fall back to the HTTP webhook "
        f"(default {DEFAULT_WEBHOOK_URL}). Nothing was written. Use "
        "`tahuti daemon configure-webhook URL` to deliver over HTTP.",
    )
    print_payload(payload, args.output, args.format)
    return 1


def _resolve_task_ids(
    client: ManageBacClient,
    target: str,
    pages: int = 10,
    snapshot_path: Path | None = None,
) -> tuple[str, str]:
    """Resolve a task target (id, URL, or class/task pair) to (class_id, task_id)."""
    cid, tid = parse_task_url(target)
    if cid and tid:
        return cid, tid
    task_id = tid or target

    # 1. Search local snapshot first for instant resolution
    snap_path = snapshot_path or DEFAULT_SNAPSHOT_PATH
    if snap_path.exists():
        snapshot = load_snapshot(snap_path)
        task = find_task_by_id(snapshot, task_id)
        if task and task.get("link"):
            cid, tid = parse_task_url(task["link"])
            if cid and tid:
                return cid, tid

    # 2. Fall back to crawling.  `fetch_notifications=False`: this resolves a
    # task id, so it reads only the three task sections, and a resolution that
    # has to reach the network at all is already the slow path — spending three
    # more requests on a hub payload it will not look at is pure waste.
    result = client.crawl_all(
        max_pages=pages, fetch_details=False, fetch_notifications=False
    )
    for task in result["upcoming"] + result["past"] + result["overdue"]:
        if task.get("id") == task_id:
            cid, tid = parse_task_url(task.get("link", ""))
            if cid and tid:
                return cid, tid

    found = client.find_task_by_id(task_id, max_pages=pages)
    if found and found.get("link"):
        cid, tid = parse_task_url(found["link"])
        if cid and tid:
            return cid, tid

    raise CommandError("task_not_found", f"Could not find task with id {task_id}")


def cmd_submit(args) -> int:
    # `--id` is the alternate spelling of the positional `target`; sibling
    # commands (`view`, `submissions`) accept both, so honour it here too.
    target = args.target or getattr(args, "id", None)
    if not target:
        payload = error(
            "submit", "missing_target", "Provide a task id or URL and file path"
        )
        print_payload(payload, args.output, args.format)
        return 1

    file_path = args.file
    if not file_path:
        payload = error("submit", "missing_file", "Provide a file path to upload")
        print_payload(payload, args.output, args.format)
        return 1

    # The MCP tool refuses the same files inside `_require_readable_file`; both
    # entry points call this one helper so they cannot drift apart.  It runs
    # before the client is built so a refused path costs no auth round-trip.
    refusal = own_state_refusal(file_path, field="file")
    if refusal:
        payload = error("submit", "state_file_refused", refusal)
        print_payload(payload, args.output, args.format)
        return 1

    state, client, email = _build_client(args, "submit")
    _authenticate_client(state, client, email)

    snapshot_path = _snapshot_path(state)

    try:
        class_id, task_id = _resolve_task_ids(
            client, target, args.pages, snapshot_path=snapshot_path
        )
    except CommandError as exc:
        payload = error("submit", exc.code, exc.message)
        print_payload(payload, args.output, args.format)
        return 1

    try:
        result = client.submit_file(class_id, task_id, file_path)
    except (FileNotFoundError, RuntimeError) as exc:
        payload = error("submit", "upload_failed", str(exc))
        print_payload(payload, args.output, args.format)
        return 1

    # Eagerly refresh the class in the snapshot so subsequent commands reflect submission immediately
    try:
        old_snapshot = load_snapshot(snapshot_path)
        existing_task = find_task_by_id(old_snapshot, task_id)
        class_name = existing_task.get("class_name") if existing_task else None

        fresh_tasks = client.get_class_tasks(
            class_id, class_name=class_name, bypass_cache=True
        )
        if fresh_tasks:
            update_snapshot_with_class_tasks(
                snapshot_path, fresh_tasks, client=client
            )
            log.info(
                "Eagerly refreshed snapshot for class %s (%d tasks)",
                class_id,
                len(fresh_tasks),
            )
        elif existing_task:
            _set_submission_state(snapshot_path, task_id, True, client=client)
    except Exception as exc:
        log.warning("Failed to eagerly refresh snapshot after submit: %s", exc)
        try:
            _set_submission_state(snapshot_path, task_id, True, client=client)
        except Exception:
            pass

    payload = ok("submit", state.active_profile, result)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_submissions(args) -> int:
    state, client, email = _build_client(args, "submissions")
    _authenticate_client(state, client, email)

    target = args.target or getattr(args, "id", None)
    if not target:
        payload = error(
            "submissions", "missing_target", "Provide a task id or URL"
        )
        print_payload(payload, args.output, args.format)
        return 1

    snapshot_path = _snapshot_path(state)
    pages = getattr(args, "pages", 10)
    try:
        class_id, task_id = _resolve_task_ids(
            client, target, pages, snapshot_path=snapshot_path
        )
    except CommandError as exc:
        payload = error("submissions", exc.code, exc.message)
        print_payload(payload, args.output, args.format)
        return 1

    task_title = None
    try:
        snap = load_snapshot(snapshot_path)
        t_info = find_task_by_id(snap, task_id)
        if t_info:
            task_title = t_info.get("title")
    except Exception:
        pass

    # 1. Action: --add / --submit
    if getattr(args, "add", None):
        file_path = args.add
        try:
            result = client.submit_file(class_id, task_id, file_path)
        except (FileNotFoundError, RuntimeError) as exc:
            payload = error("submissions", "upload_failed", str(exc))
            print_payload(payload, args.output, args.format)
            return 1

        # Eagerly refresh snapshot
        try:
            _set_submission_state(snapshot_path, task_id, True, client=client)
        except Exception:
            pass

        data = {
            "action": "add",
            "task_id": task_id,
            "filename": result.get("filename"),
            "task_url": result.get("task_url"),
        }
        payload = ok("submissions", state.active_profile, data)
        print_payload(payload, args.output, args.format)
        return 0

    # 2. Action: --delete
    if getattr(args, "delete", None):
        asset_ident = args.delete
        try:
            result = client.delete_submission(class_id, task_id, asset_ident)
        except (ValueError, RuntimeError) as exc:
            payload = error("submissions", "delete_failed", str(exc))
            print_payload(payload, args.output, args.format)
            return 1

        # Refresh snapshot: if 0 submissions remaining, mark not-submitted
        try:
            if result.get("remaining_submissions", 0) == 0:
                _set_submission_state(snapshot_path, task_id, False, client=client)
        except Exception:
            pass

        data = {
            "action": "delete",
            "task_id": task_id,
            "filename": result.get("filename"),
            "asset_id": result.get("asset_id"),
            "remaining_submissions": result.get("remaining_submissions", 0),
            "task_url": result.get("task_url"),
        }
        payload = ok("submissions", state.active_profile, data)
        print_payload(payload, args.output, args.format)
        return 0

    # 3. Action: --check-feedback
    if getattr(args, "check_feedback", None) is not None:
        target_asset = (
            args.check_feedback
            if isinstance(args.check_feedback, str)
            else None
        )
        # get_teacher_feedback returns a *dict* (`feedback_items` holds the list),
        # so filtering has to reach into that key — iterating the dict yields
        # only its keys, which is what used to crash with AttributeError.
        feedback_result = client.get_teacher_feedback(class_id, task_id)
        if target_asset:
            items = feedback_result.get("feedback_items") or []
            needle = target_asset.lower()
            matched = []
            for item in items:
                sub_name = (item.get("submission_name") or "").lower()
                att_names = [
                    (a.get("name") or "").lower()
                    for a in (item.get("attachments") or [])
                ]
                if needle in sub_name or any(needle in a for a in att_names):
                    matched.append(item)
            feedback_result = dict(feedback_result)
            feedback_result["feedback_items"] = matched
            feedback_result["feedback_count"] = len(matched)
        payload = ok("feedback", state.active_profile, feedback_result)
        print_payload(payload, args.output, args.format)
        return 0

    # 4. Action: --list or Default (when task ID is provided)
    # `--list` is the explicit spelling of what already happens by default; the
    # flag is read rather than ignored so it is echoed back and a future change
    # to the default cannot silently change what the flag does.
    explicit_list = bool(getattr(args, "list", False))
    submissions = client.get_submissions(class_id, task_id)
    data = {
        "action": "list",
        "task_id": task_id,
        "task_title": task_title,
        "submissions": submissions,
    }
    if explicit_list:
        data["requested"] = "list"
    payload = ok("submissions", state.active_profile, data)
    print_payload(payload, args.output, args.format)
    return 0


def _notification_mutation_payload(
    state, action: str, notification_id, succeeded: bool
) -> dict:
    """Envelope for a ``--read``/``--unread``/``--read-all`` mutation.

    ``hub.mark_read()`` and friends return a bare ``bool``
    (``status_code in (200, 204)``), so a rejected mutation — expired MNN-hub
    JWT, unknown notification id — arrives here as ``False`` with no other
    trace. Wrapping that in ``ok(...)`` is what made the envelope claim
    ``"ok": true`` over a ``false`` outcome while the process exited 0, so the
    envelope now follows the boolean and ``cmd_notifications`` reads the exit
    status back out of it.
    """
    if succeeded:
        return ok(
            "notifications.mutate",
            state.active_profile,
            {"action": action, "notification_id": notification_id, "ok": True},
        )
    return error(
        "notifications.mutate",
        f"{action}_failed",
        f"Notification mutation {action!r} was rejected by the MNN hub.",
    )


def cmd_notifications(args) -> int:
    state, client, email = _build_client(args, "notifications")
    _authenticate_client(state, client, email)

    hub_endpoint, token = client.get_notification_token()
    # `data-mnn-hub-endpoint` is scraped HTML, and the token goes out as
    # `Authorization: Bearer <jwt>`; the validator confines it to a known Faria
    # hub over https. See ManageBacClient._validated_hub_endpoint.
    hub = hub_client(
        client._validated_hub_endpoint(hub_endpoint), token, verify=client.session.verify
    )

    if args.read is not None:
        payload = _notification_mutation_payload(
            state, "read", args.read, hub.mark_read(args.read)
        )
        print_payload(payload, args.output, args.format)
        return EXIT_OK if payload["ok"] else EXIT_FAILURE

    if args.unread is not None:
        payload = _notification_mutation_payload(
            state, "unread", args.unread, hub.mark_unread(args.unread)
        )
        print_payload(payload, args.output, args.format)
        return EXIT_OK if payload["ok"] else EXIT_FAILURE

    if args.read_all:
        payload = _notification_mutation_payload(
            state, "read_all", None, hub.mark_all_read()
        )
        print_payload(payload, args.output, args.format)
        return EXIT_OK if payload["ok"] else EXIT_FAILURE

    stats = hub.stats()
    result = hub.list(
        page=args.page,
        per_page=args.per_page,
        filter_="unread" if getattr(args, "unread_only", False) else "all",
    )
    payload = ok(
        "notifications",
        state.active_profile,
        {
            "stats": stats,
            "items": result["items"],
            "meta": result["meta"],
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_calendar(args) -> int:
    state, client, email = _build_client(args, "calendar")
    _authenticate_client(state, client, email)

    today = date.today()

    if args.ical:
        ical_text = client.get_ical_feed()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(ical_text)
        else:
            print(ical_text)
        return 0

    if args.today:
        start = today.isoformat()
        end = today.isoformat()
    elif args.start and args.end:
        start = args.start
        end = args.end
    elif args.start:
        start = args.start
        d = date.fromisoformat(start)
        end = (d + timedelta(days=6)).isoformat()
    else:
        start = today.isoformat()
        end = (today + timedelta(days=6)).isoformat()

    events = client.get_calendar_events(start, end)
    payload = ok(
        "calendar",
        state.active_profile,
        {
            "start": start,
            "end": end,
            "events": events,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_timetable(args) -> int:
    state, client, email = _build_client(args, "timetable")
    _authenticate_client(state, client, email)

    start_date = args.date
    if args.today:
        start_date = date.today().isoformat()

    result = client.get_timetable(start_date)
    payload = ok(
        "timetable",
        state.active_profile,
        {
            "start_date": start_date or "this week",
            "days": result["days"],
            "lessons": result["lessons"],
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_grades(args) -> int:
    state, client, email = _build_client(args, "grades")
    _authenticate_client(state, client, email)

    class_id = args.class_id
    if not class_id:
        # The roster `crawl_all` itself discovers classes with: the dashboard
        # scrape. Deriving it from task links instead — which this did, and
        # which the MCP `list_classes` tool no longer does — silently drops
        # every class with no tasks, because an empty class contributes no link
        # to parse, so the two surfaces answered "which classes exist"
        # differently. It also cost a full crawl (dashboard, every class page
        # and the notification hub) to answer a question the dashboard already
        # answers.
        classes_map = client.get_classes()
        if not classes_map:
            payload = error("grades", "no_classes", "No classes found")
            print_payload(payload, args.output, args.format)
            return 1
        if args.subject:
            for cid, cname in classes_map.items():
                if args.subject.lower() in cname.lower():
                    class_id = cid
                    break
            if not class_id:
                payload = error(
                    "grades",
                    "class_not_found",
                    f"No class matching '{args.subject}'",
                )
                print_payload(payload, args.output, args.format)
                return 1
        else:
            # Gather grades for ALL classes
            all_grades = {}
            failed: dict[str, str] = {}
            for cid, cname in classes_map.items():
                try:
                    c_grades = client.get_class_grades(cid)
                    c_grades["class_name"] = cname
                    all_grades[cid] = c_grades
                except Exception as e:
                    log.warning("failed to fetch grades for class %s: %s", cid, e)
                    failed[cid] = str(e)
            payload = ok(
                "grades.all",
                state.active_profile,
                {
                    "classes_grades": all_grades,
                    "failed_classes": failed,
                },
            )
            print_payload(payload, args.output, args.format)
            # Some classes failing is a usable partial run, but a run where
            # *every* class failed returned no grades at all and must not look
            # like one where the account simply has none.
            return EXIT_OK if all_grades else EXIT_FAILURE

    grades = client.get_class_grades(class_id)
    grades["class_id"] = class_id
    payload = ok("grades", state.active_profile, grades)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_count_grade_freq(args) -> int:
    state, client, email = _build_client(args, "count-grade-freq")
    _authenticate_client(state, client, email)

    result = client.count_grade_frequencies(class_filter=args.subject)
    if "error" in result:
        payload = error("count-grade-freq", "class_not_found", result["error"])
        print_payload(payload, args.output, args.format)
        return 1

    payload = ok("count-grade-freq", state.active_profile, result)
    print_payload(payload, args.output, args.format)
    return 0


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9_\-]+", "_", text)
    return text.strip("_")


def cmd_feedback(args) -> int:
    """Fetch teacher feedback for all submitted files on a task's dropbox."""
    state, client, email = _build_client(args, "feedback")
    _authenticate_client(state, client, email)

    target = args.task_id
    snapshot_path = _snapshot_path(state)
    try:
        class_id, task_id = _resolve_task_ids(
            client, target, getattr(args, "pages", 10), snapshot_path=snapshot_path
        )
    except CommandError as exc:
        payload = error("feedback", exc.code, exc.message)
        print_payload(payload, args.output, args.format)
        return 1

    result = client.get_teacher_feedback(class_id, task_id)
    payload = ok("feedback", state.active_profile, result)
    print_payload(payload, args.output, args.format)
    return 0


# ── CLI parser ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tahuti",
        description="Crawl ManageBac tasks, grades & submissions",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"tahuti {__version__}",
        help="Show program version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_auth_flags(subparser, include_password: bool = True) -> None:
        subparser.add_argument(
            "--profile",
            default=None,
            help="Profile name (default: active_profile or default)",
        )
        subparser.add_argument("--config", help="Path to config JSON")
        subparser.add_argument("--session-file", help="Path to session JSON")
        subparser.add_argument("--school", help="School subdomain (e.g. myschool)")
        subparser.add_argument(
            "--domain",
            "-d",
            help="Base domain (default: managebac.com)",
        )
        subparser.add_argument("--email", "-e", help="Login email")
        if include_password:
            subparser.add_argument("--password", "-p", help="Login password")
        subparser.add_argument(
            "--cookie", "-c", help="Session cookie (_managebac_session)"
        )
        subparser.add_argument(
            "--reauth",
            action="store_true",
            help="Force re-login instead of reusing saved session",
        )
        subparser.add_argument(
            "--refresh",
            action="store_true",
            help="Bypass response cache and fetch fresh data",
        )
        subparser.add_argument(
            "--cache-ttl",
            type=int,
            default=None,
            help="Cache TTL in seconds (default: 900, i.e. 15 min)",
        )
        subparser.add_argument(
            "--no-verify-tls",
            action="store_true",
            help="Disable TLS certificate verification (for self-hosted instances)",
        )
        subparser.add_argument(
            "--retry",
            type=int,
            default=3,
            metavar="N",
            help="Max retries with exponential backoff on transient errors (default: 3, 0=off)",
        )
        subparser.add_argument("--output", "-o", help="Write output to file")
        subparser.add_argument(
            "--format",
            choices=["pretty", "json"],
            default=None,
            help="Output format (default: pretty for TTY, json otherwise)",
        )

    login = subparsers.add_parser(
        "login",
        help="Authenticate: saves the session cookie, and the password only on request",
    )
    add_common_auth_flags(login)
    login.add_argument(
        "--keep-credentials",
        action="store_true",
        help="Also save the password so an expired cookie can be renewed without "
        "a prompt. Off by default: the session cookie is saved either way, so "
        "you are not asked for your password on every command, but nothing "
        "writes your password to disk unless you pass this. Goes to the OS "
        "keychain when --keychain is given and one is available, and to the "
        "cleartext 0600 creds file otherwise. Same flag, same sense, as "
        "`logout --keep-credentials`.",
    )
    login.add_argument(
        "--no-remember-me",
        action="store_true",
        help="Omit remember_me from the login POST, leaving the cookie's lifetime "
        "to ManageBac's default instead of asking for a persistent one. A "
        "server-side setting only — it changes nothing on disk, and composes "
        "with --keep-credentials.",
    )
    login.add_argument(
        "--keychain",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Where a password kept by --keep-credentials goes: the OS keychain "
        "(macOS Keychain / Linux Secret Service / Windows Credential Locker) "
        "instead of cleartext creds.<profile>.json. Overrides "
        "MANAGEBAC_KEYCHAIN. Decides only *where*, never *whether*.",
    )
    login.set_defaults(func=cmd_login)

    list_parser = subparsers.add_parser("list", help="List ManageBac tasks")
    add_common_auth_flags(list_parser)
    list_parser.add_argument(
        "--subject", "-s", help="Filter tasks by subject/class name"
    )
    list_parser.add_argument(
        "--pages",
        type=int,
        default=None,
        help="Max pages per view (default: from config, 10)",
    )
    list_parser.add_argument(
        "--details",
        action="store_true",
        default=None,
        help="Fetch task detail pages",
    )
    list_parser.add_argument(
        "--view",
        choices=["all", "upcoming", "past", "overdue"],
        default=None,
        help="Restrict output to one view or all views (default: from config, all)",
    )
    
    graded_group = list_parser.add_mutually_exclusive_group()
    graded_group.add_argument(
        "--graded",
        action="store_true",
        default=None,
        help="Show only graded tasks",
    )
    graded_group.add_argument(
        "--not-graded",
        action="store_false",
        dest="graded",
        help="Show only non-graded tasks",
    )

    submitted_group = list_parser.add_mutually_exclusive_group()
    submitted_group.add_argument(
        "--submitted",
        action="store_true",
        default=None,
        help="Show only submitted tasks",
    )
    submitted_group.add_argument(
        "--not-submitted",
        action="store_false",
        dest="submitted",
        help="Show only non-submitted tasks",
    )
    list_parser.add_argument(
        "--grade",
        help="Filter tasks by grade (e.g. 'B', 'B-', '4.0')",
    )
    list_parser.add_argument(
        "--tag", "-t",
        help="Filter tasks by tag/label (e.g. 'Exam', 'Summative')",
    )
    completed_group = list_parser.add_mutually_exclusive_group()
    completed_group.add_argument(
        "--completed",
        action="store_true",
        default=None,
        help="Show only completed tasks (either submitted or passing grade)",
    )
    completed_group.add_argument(
        "--todo",
        action="store_true",
        default=None,
        help="Show only uncompleted/todo tasks (not submitted and ungraded/F)",
    )
    list_parser.add_argument(
        "--deleted",
        action="store_true",
        help="Include tasks that were deleted from the server",
    )
    list_parser.set_defaults(func=cmd_list)

    view = subparsers.add_parser("view", help="View one task in detail")
    add_common_auth_flags(view)
    view.add_argument("target", nargs="?", help="Task id or task URL")
    view.add_argument("--id", help="Task id")
    view.add_argument("--url", help="Task URL")
    view.add_argument("--subject", help="Optional subject filter when resolving by id")
    view.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id",
    )
    view.set_defaults(func=cmd_view)

    logout = subparsers.add_parser("logout", help="Clear persisted session")
    logout.add_argument("--profile", default=None, help="Profile name")
    logout.add_argument("--config", help="Path to config JSON")
    logout.add_argument("--session-file", help="Path to session JSON")
    logout.add_argument("--all", action="store_true", help="Remove all saved sessions")
    logout.add_argument(
        "--keep-cache",
        action="store_true",
        help="Keep the on-disk response cache (it holds grade pages and a hub JWT)",
    )
    logout.add_argument(
        "--keep-credentials",
        action="store_true",
        help="Keep the saved password so later commands can log in silently "
        "(by default `logout` deletes this profile's creds file and any keychain "
        "entry; `logout --all` deletes every profile's)",
    )
    logout.add_argument(
        "--purge",
        action="store_true",
        help="Also delete this profile's entry from config.json — school, "
        "domain, email and the defaults block — so the machine stops "
        "remembering which school it belongs to. Implies the credential "
        "deletion above, so it cannot be combined with --keep-credentials. "
        "With --all, every profile's entry goes and config.json is left with "
        "an empty profiles map",
    )
    logout.add_argument("--output", "-o", help="Write output to file")
    logout.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    logout.set_defaults(func=cmd_logout)

    daemon = subparsers.add_parser("daemon", help="Manage webhook daemon")
    daemon_subparsers = daemon.add_subparsers(dest="daemon_command", required=True)

    daemon_run = daemon_subparsers.add_parser(
        "run", help="Run real-time notification daemon loop in foreground"
    )
    add_common_auth_flags(daemon_run)
    daemon_run.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_run.add_argument("--webhook-url", help="Webhook destination URL")
    daemon_run.add_argument("--secret", help="HMAC secret for webhook signatures")
    # `--interval` is the spelling `daemon start` uses for the same setting; both
    # names resolve to one dest so neither sibling can drift out of sync again.
    daemon_run.add_argument(
        "--poll-interval",
        "--interval",
        dest="poll_interval",
        type=int,
        help="Poll interval in seconds (alias: --interval)",
    )
    daemon_run.add_argument(
        "--channel-id",
        help="NOT IMPLEMENTED: channel-send delivery has no zeroclaw transport, so "
        "using it with --recipient fails rather than silently falling back to "
        "the HTTP webhook (e.g. qq, telegram)",
    )
    daemon_run.add_argument(
        "--recipient", help="Channel recipient ID (used with --channel-id)"
    )
    daemon_run.add_argument(
        "--active-hours-start",
        type=int,
        metavar="HOUR",
        help="First hour the daemon polls, 0-23 local time (default: 7); "
        "outside the active window it sleeps instead of polling",
    )
    daemon_run.add_argument(
        "--active-hours-end",
        type=int,
        metavar="HOUR",
        help="Last hour the daemon polls, 0-23 local time (default: 23)",
    )
    daemon_run.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute alerts but deliver nothing and change no state file: no "
            "webhook POST, and the snapshot and dedup state are left exactly as "
            "they were so the next real run still sees every alert"
        ),
    )
    daemon_run.add_argument("--once", action="store_true", help="Run one cycle and exit")
    daemon_run.set_defaults(func=cmd_daemon_run)

    daemon_start = daemon_subparsers.add_parser(
        "start", help="Start daemon loop or run one cycle"
    )
    add_common_auth_flags(daemon_start)
    daemon_start.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_start.add_argument("--webhook-url", help="Override webhook URL for this run")
    daemon_start.add_argument(
        "--secret", help="HMAC secret for webhook signatures"
    )
    daemon_start.add_argument(
        "--channel-id",
        help="NOT IMPLEMENTED: channel-send delivery has no zeroclaw transport, so "
        "using it with --recipient fails rather than silently falling back to "
        "the HTTP webhook (e.g. qq, telegram)",
    )
    daemon_start.add_argument(
        "--recipient", help="Channel recipient ID (used with --channel-id)"
    )
    daemon_start.add_argument(
        "--interval",
        "--poll-interval",
        dest="interval",
        type=int,
        help="Polling interval in seconds (alias: --poll-interval)",
    )
    daemon_start.add_argument(
        "--active-hours-start",
        type=int,
        metavar="HOUR",
        help="First hour the daemon polls, 0-23 local time (default: 7); "
        "outside the active window it sleeps instead of polling",
    )
    daemon_start.add_argument(
        "--active-hours-end",
        type=int,
        metavar="HOUR",
        help="Last hour the daemon polls, 0-23 local time (default: 23)",
    )
    daemon_start.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute alerts but deliver nothing and change no state file: no "
            "webhook POST, and the snapshot and dedup state are left exactly as "
            "they were so the next real run still sees every alert"
        ),
    )
    daemon_start.add_argument(
        "--once", action="store_true", help="Run one cycle and exit"
    )
    daemon_start.add_argument(
        "--background", "-b", action="store_true", help="Run as detached background process"
    )
    daemon_start.add_argument("--pid-file", help="Custom PID file path")
    daemon_start.add_argument("--log-file", help="Custom log file path")
    daemon_start.set_defaults(func=cmd_daemon_start)

    daemon_stop = daemon_subparsers.add_parser("stop", help="Stop daemon loop")
    daemon_stop.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_stop.add_argument("--pid-file", help="Custom PID file path")
    daemon_stop.add_argument("--output", "-o", help="Write output to file")
    daemon_stop.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_stop.set_defaults(func=cmd_daemon_stop)

    daemon_status = daemon_subparsers.add_parser("status", help="Show daemon process status")
    daemon_status.add_argument("--pid-file", help="Custom PID file path")
    daemon_status.add_argument("--log-file", help="Custom log file path")
    daemon_status.add_argument("--output", "-o", help="Write output to file")
    daemon_status.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_status.set_defaults(func=cmd_daemon_status)

    daemon_test_wh = daemon_subparsers.add_parser("test-webhook", help="Test webhook endpoint with ping event")
    daemon_test_wh.add_argument("url", nargs="?", help="Webhook URL to test")
    daemon_test_wh.add_argument("--secret", help="Optional HMAC secret")
    daemon_test_wh.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_test_wh.add_argument("--output", "-o", help="Write output to file")
    daemon_test_wh.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_test_wh.set_defaults(func=cmd_daemon_test_webhook)

    daemon_install = daemon_subparsers.add_parser("install", help="Install auto-start system service (launchd/systemd)")
    daemon_install.add_argument("--log-file", help="Custom log file path")
    daemon_install.add_argument("--output", "-o", help="Write output to file")
    daemon_install.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_install.set_defaults(func=cmd_daemon_install)

    daemon_uninstall = daemon_subparsers.add_parser("uninstall", help="Uninstall auto-start system service")
    daemon_uninstall.add_argument("--output", "-o", help="Write output to file")
    daemon_uninstall.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_uninstall.set_defaults(func=cmd_daemon_uninstall)

    daemon_configure = daemon_subparsers.add_parser(
        "configure-webhook", help="Persist daemon webhook URL"
    )
    daemon_configure.add_argument("url", help="Webhook URL")
    daemon_configure.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_configure.add_argument("--output", "-o", help="Write output to file")
    daemon_configure.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_configure.set_defaults(func=cmd_daemon_configure_webhook)

    daemon_configure_ch = daemon_subparsers.add_parser(
        "configure-channel",
        help="NOT IMPLEMENTED: channel-send delivery has no zeroclaw transport, so "
        "this command fails instead of writing a config key nothing reads",
    )
    daemon_configure_ch.add_argument(
        "channel_id", help="Channel name (e.g. qq, telegram)"
    )
    daemon_configure_ch.add_argument(
        "recipient", help="Recipient ID (platform-specific)"
    )
    daemon_configure_ch.add_argument(
        "--daemon-config", help="Path to daemon JSON config"
    )
    daemon_configure_ch.add_argument("--output", "-o", help="Write output to file")
    daemon_configure_ch.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_configure_ch.set_defaults(func=cmd_daemon_configure_channel)

    submit = subparsers.add_parser("submit", help="Upload a file to a task dropbox")
    add_common_auth_flags(submit)
    submit.add_argument("target", nargs="?", help="Task id or URL")
    submit.add_argument("file", nargs="?", help="File path to upload")
    submit.add_argument("--id", help="Task id")
    submit.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id",
    )
    submit.set_defaults(func=cmd_submit)

    submissions_p = subparsers.add_parser(
        "submissions",
        help="Manage task submissions (list, add, delete, check-feedback)",
    )
    add_common_auth_flags(submissions_p)
    submissions_p.add_argument("target", nargs="?", help="Task id or URL")
    submissions_p.add_argument("--id", help="Task id")
    submissions_p.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id",
    )
    submissions_p.add_argument(
        "--list", action="store_true", help="List current submissions for the task"
    )
    submissions_p.add_argument(
        "--add", "--submit", dest="add", help="Upload a file to the task dropbox"
    )
    submissions_p.add_argument(
        "--delete", help="Delete a submitted file by asset ID or filename"
    )
    submissions_p.add_argument(
        "--check-feedback",
        nargs="?",
        const=True,
        default=None,
        help="Check teacher feedback (optionally filter by asset ID or name)",
    )
    submissions_p.set_defaults(func=cmd_submissions)

    notifications = subparsers.add_parser(
        "notifications", help="View and manage notifications"
    )
    add_common_auth_flags(notifications)
    notifications.add_argument(
        "--page", type=int, default=1, help="Page number (default: 1)"
    )
    notifications.add_argument(
        "--per-page", type=int, default=20, help="Items per page (default: 20)"
    )
    notifications.add_argument(
        "--read", type=int, metavar="ID", help="Mark notification as read"
    )
    notifications.add_argument(
        "--unread", type=int, metavar="ID", help="Mark notification as unread"
    )
    notifications.add_argument(
        "--read-all",
        action="store_true",
        help="Mark all notifications as read",
    )
    notifications.add_argument(
        "--unread-only",
        action="store_true",
        help="Only list unread notifications",
    )
    notifications.set_defaults(func=cmd_notifications)

    calendar_p = subparsers.add_parser("calendar", help="View calendar events")
    add_common_auth_flags(calendar_p)
    calendar_p.add_argument("--start", help="Start date (YYYY-MM-DD)")
    calendar_p.add_argument("--end", help="End date (YYYY-MM-DD)")
    calendar_p.add_argument("--today", action="store_true", help="Show today only")
    calendar_p.add_argument("--ical", action="store_true", help="Output raw iCal feed")
    calendar_p.set_defaults(func=cmd_calendar)

    timetable_p = subparsers.add_parser("timetable", help="View weekly timetable")
    add_common_auth_flags(timetable_p)
    timetable_p.add_argument("--date", help="Start date of week (YYYY-MM-DD)")
    timetable_p.add_argument("--today", action="store_true", help="Show this week")
    timetable_p.set_defaults(func=cmd_timetable)

    grades_p = subparsers.add_parser(
        "grades", help="View class grades and expected grade"
    )
    add_common_auth_flags(grades_p)
    grades_p.add_argument("--class-id", help="Class ID (numeric)")
    grades_p.add_argument("--subject", "-s", help="Fuzzy match class name")
    grades_p.set_defaults(func=cmd_grades)

    count_freq_p = subparsers.add_parser(
        "count-grade-freq", help="Count frequency of each grade letter"
    )
    add_common_auth_flags(count_freq_p)
    count_freq_p.add_argument(
        "--subject", "-s", help="Restrict to one class (fuzzy match)"
    )
    count_freq_p.set_defaults(func=cmd_count_grade_freq)

    feedback_p = subparsers.add_parser(
        "feedback", help="Fetch teacher feedback for a submitted task"
    )
    add_common_auth_flags(feedback_p)
    feedback_p.add_argument(
        "task_id",
        help="Task numeric ID or full ManageBac URL",
    )
    feedback_p.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id (default: 10)",
    )
    feedback_p.set_defaults(func=cmd_feedback)

    return parser


# ── Entry point ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    # File permissions are the only barrier protecting a cleartext creds.json,
    # so say so loudly if something outside `mb` loosened them.
    warn_on_weak_permissions()
    try:
        # The handler's return value is the exit status, and `SystemExit(None)`
        # would be a silent 0 — so every handler must return an int. All 22 do;
        # `tests/test_exit_codes.py` covers the dispatch itself.
        raise SystemExit(args.func(args))
    except KeyboardInterrupt:
        # A Ctrl-C at the `ManageBac password:` prompt — or during any long
        # poll — reached the user as a raw traceback, which reads like a crash
        # and hides the fact that nothing went wrong. Exit the conventional
        # 130 (128 + SIGINT) instead, so a shell can tell an interrupt from a
        # failure. Deliberately not folded into the `Exception` clause below:
        # KeyboardInterrupt is a BaseException, and treating a deliberate
        # cancel as an internal error would print a JSON error envelope for a
        # user who simply changed their mind.
        print(file=sys.stderr)
        raise SystemExit(EXIT_INTERRUPTED)
    except BrokenPipeError:
        # `tahuti list | head` closes stdout early, and Python then reports the
        # dead pipe a second time at interpreter shutdown as a confusing error
        # after the real work already succeeded. Point fd 1 at devnull so that
        # shutdown flush has somewhere to go, then exit 0 — the output the
        # consumer did ask for was already delivered.
        #
        # Rebind fd 1 directly rather than through `sys.stdout`: under a test
        # harness's capture that stream is not ours to redirect, and dup2'ing
        # over its descriptor breaks the capture.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        except OSError:
            pass
        raise SystemExit(EXIT_OK)
    except CommandError as exc:
        payload = error(args.command, exc.code, exc.message)
        print_payload(payload, args.output, getattr(args, "format", None))
        raise SystemExit(1)
    except Exception as exc:
        # Anything else (RuntimeError from the client, a socket error, a bug)
        # used to reach the user as a raw traceback, which neither a shell
        # caller nor a `--format json` consumer can act on. Emit the same
        # machine-readable envelope CommandError produces and keep exit 1.
        # SystemExit is a BaseException, so it is not caught here.
        log.exception("Unexpected failure in command %s", args.command)
        payload = error(
            args.command, "internal_error", f"{type(exc).__name__}: {exc}"
        )
        print_payload(payload, args.output, getattr(args, "format", None))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
