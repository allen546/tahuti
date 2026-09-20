"""MCP server for ManageBac — stdio transport."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .auth import build_client, hub_client
from .client import task_id_from_target
from .config import own_state_refusal
from .filters import InvalidViewError, normalize_view, result_views, summary_of
from .exceptions import CommandError
from .filters import InvalidViewError, normalize_view, result_views
from .notifications import MNNHubClient

log = logging.getLogger(__name__)

mcp = FastMCP(
    "tahuti",
    instructions=(
        "ManageBac MCP server. Provides tools to interact with ManageBac: "
        "list/view tasks, submit files, view notifications, calendar events, "
        "timetables, and class grades."
    ),
)


# ── Error sanitisation ───────────────────────────────────────────────
# Tool results land in the assistant's context, which may be influenced by
# content scraped from ManageBac.  Raw exception text can therefore leak
# local paths, session cookies, or a remote endpoint's response body.
_SENSITIVE_KEY_RE = re.compile(
    r"(cookie|token|password|passwd|secret|authorization|bearer|session)",
    re.IGNORECASE,
)


def _sanitize_error(exc: Exception) -> str:
    """Return a short, redacted description of *exc* safe for tool output."""
    text = str(exc)
    # Drop obvious credential material and keep the message short.
    if _SENSITIVE_KEY_RE.search(text):
        text = "an authentication or credential error occurred"
    text = "".join(
        ch for ch in text if ch == "\t" or (0x20 <= ord(ch) != 0x7F)
    )
    return text[:200]


def _error_payload(exc: Exception) -> str:
    """Build the JSON error string returned by MCP tools."""
    return json.dumps({"error": _sanitize_error(exc)})


def _hub_for(client) -> MNNHubClient:
    """Build a hub client whose endpoint the scraped page cannot choose.

    ``data-mnn-hub-endpoint`` comes out of scraped ManageBac HTML, and the hub
    token is sent as ``Authorization: Bearer <jwt>``.  Routing the raw value
    into ``MNNHubClient`` therefore let a poisoned page (or a TLS-stripping
    MITM, which ``verify_tls=False`` makes possible) pick the host that
    receives the JWT — including a cleartext ``http://`` one.

    ``client._validated_hub_endpoint`` is the guard that already protects the
    CLI path; it accepts the scraped value only when it is https, carries no
    userinfo or port, and names a known Faria hub, falling back to the host
    this domain expects.  All three notification tools go through here.
    """
    hub_endpoint, token = client.get_notification_token()
    return hub_client(
        client._validated_hub_endpoint(hub_endpoint),
        token,
        verify=client.session.verify,
    )


# ── Input validation ───────────────────────────────────────────────────
# Tool arguments come from a model, not a human reading an error message, so a
# malformed value must produce a short, actionable error rather than a 404 from
# ManageBac or a confusing traceback.  Every validator returns the coerced
# value and raises :class:`InvalidToolInput` otherwise.


class InvalidToolInput(Exception):
    """Raised when an MCP tool argument fails validation."""


# ManageBac object ids are plain integers.
_ID_RE = re.compile(r"^\d+$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_numeric_id(value: object, field: str, example: str) -> str:
    """Return *value* as a bare numeric id string, or raise."""
    text = str(value or "").strip()
    if not _ID_RE.match(text):
        raise InvalidToolInput(
            f"{field} must be a numeric ManageBac id (e.g. {example!r}), got {text[:60]!r}"
        )
    return text


def _require_iso_date(value: object, field: str) -> str:
    """Return *value* as a valid ``YYYY-MM-DD`` date string, or raise."""
    text = str(value or "").strip()
    if not _ISO_DATE_RE.match(text):
        raise InvalidToolInput(
            f"{field} must be a YYYY-MM-DD date, got {text[:60]!r}"
        )
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise InvalidToolInput(f"{field} is not a real calendar date: {text!r}") from exc
    return text


def _require_readable_file(value: object, field: str = "file_path") -> str:
    """Return the absolute path of an existing regular file, or raise.

    Symlinks are followed so the check applies to whatever would actually be
    uploaded, but nothing outside an existing regular file is accepted: this
    keeps a confused tool call from pointing at ``/dev/…``, a directory, or a
    FIFO.
    """
    text = str(value or "").strip()
    if not text:
        raise InvalidToolInput(f"{field} is required and must be a local file path")
    if "\x00" in text:
        raise InvalidToolInput(f"{field} contains a NUL byte")
    try:
        path = Path(text).expanduser()
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidToolInput(f"{field} is not a usable path: {text[:60]!r}") from exc

    # Containment runs on the *resolved* path, so a symlink into tahuti's own
    # state is refused by its target, and before the existence check below: a
    # path inside tahuti's own directories is a policy question, not a typo
    # worth reporting twice.
    refusal = own_state_refusal(resolved, field=field)
    if refusal:
        raise InvalidToolInput(refusal)

    if not resolved.exists():
        raise InvalidToolInput(f"{field} does not exist: {text[:120]!r}")
    if not resolved.is_file():
        raise InvalidToolInput(
            f"{field} is not a regular file: {text[:120]!r}"
        )
    if not os.access(resolved, os.R_OK):
        raise InvalidToolInput(f"{field} is not readable: {text[:120]!r}")
    return str(resolved)


def _invalid_input(exc: InvalidToolInput) -> str:
    """Return the JSON error payload for a failed argument check."""
    return json.dumps({"error": str(exc)})


def _resolve_task_id(target: str) -> str:
    """Extract the task id from a task id or a full ManageBac task URL.

    Two shapes are accepted, matching the documented contract: a bare numeric
    id, or a URL/path carrying ``/core_tasks/<id>``.  Anything else used to
    yield the *entire* input as the "id" (``"https://…".split("core_tasks/")[-1]``
    with no separator present), and ``parse_task_url``'s "last path segment"
    fallback would happily return a *class* id for a class URL.

    The rule itself now lives in :func:`client.task_id_from_target`, which the
    CLI's ``view`` command also calls — the two surfaces used to derive it
    independently and disagreed about what to accept.  This wrapper keeps the
    :class:`InvalidToolInput` contract every tool here reports through.
    """
    try:
        return task_id_from_target(target)
    except CommandError as exc:
        raise InvalidToolInput(exc.message) from exc


def _class_and_task(client, target: str, pages: int = 10) -> tuple[str, str]:
    """Resolve *target* to ``(class_id, task_id)`` through the CLI's one ladder.

    Three tools here — ``submit_file``, ``delete_submission`` and
    ``get_teacher_feedback`` — each carried their own copy of this ladder, and
    each was wrong in a different way.  Two of them still ended in
    ``get_tasks_by_view``, the "second, overlapping source of the same tasks"
    that ``list_tasks`` was moved off, so a class whose tasks only appear on its
    own core_tasks page resolved here but not in the CLI's ``list``.
    ``delete_submission`` had no final step at all, so it reported tasks
    unresolvable that the CLI submits to.  The CLI's ``_resolve_task_ids`` is
    the ladder that survived that cleanup — URL, then the local snapshot, then
    ``crawl_all``, then ``find_task_by_id`` — and it is the only one left.

    The page budget is the CLI's default rather than the ad-hoc 3, 5 and 10 the
    three copies used, so the MCP tools now see exactly the task set the CLI
    does.  ``CommandError`` is that ladder's refusal; it is restated as an
    :class:`InvalidToolInput` so the ``{"error": ...}`` payload these tools
    already return on failure is still what a caller sees, instead of an
    exception escaping into the MCP transport.
    """
    from .__main__ import _resolve_task_ids

    try:
        return _resolve_task_ids(client, target, pages)
    except CommandError as exc:
        raise InvalidToolInput(exc.message) from exc


# ── Tasks ───────────────────────────────────────────────────────────────


@mcp.tool()
def list_tasks(
    view: str = "all",
    subject: str | None = None,
    graded: bool | None = None,
    submitted: bool | None = None,
    grade: str | None = None,
    tag: str | None = None,
    completed: bool | None = None,
    details: bool = False,
    pages: int = 10,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """List ManageBac tasks (upcoming, past, overdue).

    Args:
        view: "all", "upcoming", "past", or "overdue"
        subject: Filter by subject/class name (case-insensitive substring)
        graded: Filter by graded status (True=graded only, False=not graded only)
        submitted: Filter by submission status (True=submitted only, False=not submitted only)
        grade: Filter by specific grade letter or GPA (e.g. 'B', 'B-', '4.0')
        tag: Filter by label/tag query (supports 'a,b' OR and 'a+b' AND forms)
        completed: Filter by completion status (True=completed only, False=todo only)
        details: Fetch task detail pages (slower, one request per task)
        pages: Max pages per view (default 10)
        school: School subdomain (e.g. "myschool")
        domain: Base domain ("managebac.com" or "managebac.cn")
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    # Validate before any network call: an unrecognised view used to match none
    # of the three section checks below, so the tool answered with three empty
    # lists and total_count 0 — a valid-looking "you have no homework".
    try:
        canonical_view = normalize_view(view)
    except InvalidViewError as exc:
        return json.dumps({"error": exc.message})

    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )

    # One canonical view drives which sections are *reported*, so the MCP tool
    # and filters.result_views can no longer disagree about what a view means.
    #
    # The task source is `crawl_all`, the same one the CLI's `list` command uses:
    # classes discovered from the dashboard, then each class's core_tasks page.
    # The per-view `tasks_and_deadlines` crawl this replaces is a second,
    # overlapping source of the same tasks, so the two surfaces could report
    # different sets — and a class whose tasks only appear on its own
    # core_tasks page would be missing here entirely.
    #
    # `view` is therefore a display filter over the result, not a crawl
    # selector — exactly the CLI's `--view` contract. Asking for one view still
    # crawls all three, because `crawl_all` does and the CLI's does too.
    result = client.crawl_all(max_pages=pages, fetch_details=details)
    views = result_views(result, canonical_view)

    upcoming = views["upcoming"]
    past = views["past"]
    overdue = views["overdue"]

    if subject:
        def _match(task, s) -> bool:
            cn = task.get("class_name", "")
            return s.lower() in cn.lower() if cn else False

        upcoming = [t for t in upcoming if _match(t, subject)]
        past = [t for t in past if _match(t, subject)]
        overdue = [t for t in overdue if _match(t, subject)]

    from .filters import matches_graded, matches_submitted, matches_grade_query

    if graded is not None:
        upcoming = [t for t in upcoming if matches_graded(t, graded)]
        past = [t for t in past if matches_graded(t, graded)]
        overdue = [t for t in overdue if matches_graded(t, graded)]

    if submitted is not None:
        upcoming = [t for t in upcoming if matches_submitted(t, submitted)]
        past = [t for t in past if matches_submitted(t, submitted)]
        overdue = [t for t in overdue if matches_submitted(t, submitted)]

    if grade is not None:
        upcoming = [t for t in upcoming if matches_grade_query(t, grade)]
        past = [t for t in past if matches_grade_query(t, grade)]
        overdue = [t for t in overdue if matches_grade_query(t, grade)]

    if tag is not None:
        from .filters import matches_tag
        upcoming = [t for t in upcoming if matches_tag(t, tag)]
        past = [t for t in past if matches_tag(t, tag)]
        overdue = [t for t in overdue if matches_tag(t, tag)]

    # Same `completed`/`todo` pair the CLI's `list` command exposes; MCP folds
    # both into one tri-state so callers keep the "completed only / todo only"
    # semantics rather than having to know which helper to reach for.
    if completed is not None:
        from .filters import matches_completed
        upcoming = [t for t in upcoming if matches_completed(t, completed)]
        past = [t for t in past if matches_completed(t, completed)]
        overdue = [t for t in overdue if matches_completed(t, completed)]

    result = {
        "student_name": client.student_name,
        "school": client.school,
        "base_url": client.base,
        "crawled_at": datetime.now().isoformat(),
        "upcoming": upcoming,
        "past": past,
        "overdue": overdue,
        "summary": summary_of({"upcoming": upcoming, "past": past, "overdue": overdue}),
    }

    return json.dumps(result, indent=2, ensure_ascii=False)


def _snapshot_path_for(state) -> Path:
    """Return the snapshot file belonging to *state*'s config directory.

    The snapshot is written beside the config file, so this follows a
    non-default profile's location.  ``DEFAULT_SNAPSHOT_PATH`` only names the
    default one, and the CLI's ``_snapshot_path`` is private to ``__main__``.
    """
    return state.config_path.parent / "snapshot.json"


@mcp.tool()
def view_task(
    task_id: str | None = None,
    task_url: str | None = None,
    pages: int = 10,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """View detailed information about a specific task.

    Provide either task_id or task_url. The task_url can be a full ManageBac URL.
    A bare task_id is looked up in the local snapshot and then, if needed, by
    crawling up to `pages` pages of the task lists — so passing the full URL is
    faster.

    Args:
        task_id: Numeric task ID (e.g. "1000026")
        task_url: Full task URL
        pages: Max pages to search when resolving by id
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    target = task_url or task_id
    if not target:
        return json.dumps({"error": "Provide task_id or task_url"})

    # Without this, a URL lacking "/core_tasks/" made the whole string the "id".
    try:
        resolved_id = _resolve_task_id(target)
    except InvalidToolInput as exc:
        return _invalid_input(exc)

    from .__main__ import find_task_by_id, load_snapshot

    if "/core_tasks/" in target:
        # A task URL already names the detail page, so it is fetched as given;
        # the snapshot only supplies the metadata the listing carries.
        snapshot = load_snapshot(_snapshot_path_for(state))
        task = find_task_by_id(snapshot, resolved_id) or {
            "id": resolved_id,
            "link": target,
        }
        detail = client.get_task_detail(target)
    else:
        task = find_task_by_id(load_snapshot(_snapshot_path_for(state)), resolved_id)
        if not task:
            fallback = client.find_task_by_id(resolved_id, max_pages=pages)
            if isinstance(fallback, dict):
                task = fallback
        if not task:
            # A bare id names no URL, and get_task_detail would concatenate it
            # onto the base URL rather than fail cleanly.
            return json.dumps(
                {
                    "error": (
                        f"No task found for id {resolved_id} in the local snapshot "
                        f"or the first {pages} pages of the task lists; pass the "
                        "full task URL or raise pages"
                    )
                }
            )
        detail = (
            client.get_task_detail(task["link"], from_hint=False)
            if task.get("link")
            else {}
        )

    # get_task_detail reports a fetch failure by returning a truthy
    # {"error": ...} dict instead of raising, so without this the envelope below
    # would nest that error inside an otherwise-successful payload.
    if isinstance(detail, dict) and detail.get("error"):
        return json.dumps({"error": str(detail["error"])})

    return json.dumps({"task": task, "detail": detail}, indent=2, ensure_ascii=False)


# ── File submission ─────────────────────────────────────────────────────


@mcp.tool()
def submit_file(
    task_id: str,
    file_path: str,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Submit a file to a task's dropbox.

    The task_id can be a numeric ID or a full ManageBac URL.
    PREFER passing the full task URL (e.g. from the list_tasks results) to bypass resolution.

    Args:
        task_id: Task ID or full URL (e.g. "1000026" or "https://myschool.managebac.cn/student/classes/1000001/core_tasks/1000099")
        file_path: Local path to the file to upload (tahuti's own credential
            and cache files are refused)
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    # The CLI's _safe_filename containment (__main__.py) guards *downloads*;
    # nothing checked the upload path, so a confused tool call could name a
    # directory, /dev/null, or a typo'd path and only fail inside the upload.
    try:
        safe_path = _require_readable_file(file_path)
    except InvalidToolInput as exc:
        return _invalid_input(exc)

    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )

    # One ladder, the CLI's: a full task URL resolves from itself, and a bare id
    # is looked up in the local snapshot, then `crawl_all`, then
    # `find_task_by_id`.  The `get_tasks_by_view` steps this replaces came from
    # the second, overlapping task source `list_tasks` was moved off.
    try:
        class_id, resolved_id = _class_and_task(client, task_id)
    except InvalidToolInput as exc:
        return _invalid_input(exc)

    try:
        result = client.submit_file(class_id, resolved_id, safe_path)
        # Eagerly refresh snapshot
        try:
            from .__main__ import (
                DEFAULT_SNAPSHOT_PATH,
                load_snapshot,
                find_task_by_id,
                update_snapshot_with_class_tasks,
            )
            snapshot = load_snapshot(DEFAULT_SNAPSHOT_PATH)
            existing = find_task_by_id(snapshot, resolved_id)
            class_name = existing.get("class_name") if existing else None
            fresh_tasks = client.get_class_tasks(
                class_id, class_name=class_name, bypass_cache=True
            )
            if fresh_tasks:
                update_snapshot_with_class_tasks(
                    DEFAULT_SNAPSHOT_PATH, fresh_tasks, client=client
                )
            elif existing:
                existing["status"] = "submitted"
                existing["has_submit_button"] = False
                update_snapshot_with_class_tasks(
                    DEFAULT_SNAPSHOT_PATH, [existing], client=client
                )
        except Exception:
            pass
        return json.dumps(result, indent=2)
    except Exception as e:
        return _error_payload(e)


@mcp.tool()
def delete_submission(
    task_id: str,
    asset_id: str,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Delete a submitted file from a task's dropbox on ManageBac.

    The task_id can be a numeric ID or a full ManageBac URL.
    The asset_id can be a numeric asset ID or the filename.

    Args:
        task_id: Task ID or full URL (e.g. "1000026" or "https://myschool.managebac.cn/student/classes/1000001/core_tasks/1000099")
        asset_id: The asset ID (e.g. "82189817") or filename of the submission to delete
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )

    # The same one ladder `submit_file` and the CLI use.  This tool's own copy
    # ended at `get_tasks_by_view`, with no `find_task_by_id` step, so it
    # answered "Could not resolve class_id" for tasks the CLI happily submits
    # to — the divergence this replaces.
    try:
        class_id, resolved_id = _class_and_task(client, task_id)
    except InvalidToolInput as exc:
        return _invalid_input(exc)

    try:
        result = client.delete_submission(class_id, resolved_id, asset_id)
        try:
            from .__main__ import (
                DEFAULT_SNAPSHOT_PATH,
                load_snapshot,
                find_task_by_id,
                update_snapshot_with_class_tasks,
            )

            if result.get("remaining_submissions", 0) == 0:
                old_snapshot = load_snapshot(DEFAULT_SNAPSHOT_PATH)
                existing = find_task_by_id(old_snapshot, resolved_id)
                if existing:
                    existing["status"] = "not-submitted"
                    existing["has_submit_button"] = True
                    update_snapshot_with_class_tasks(
                        DEFAULT_SNAPSHOT_PATH, [existing], client=client
                    )
        except Exception:
            pass
        return json.dumps(result, indent=2)
    except Exception as e:
        return _error_payload(e)


@mcp.tool()
def get_teacher_feedback(
    task_id: str | None = None,
    task_url: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch teacher feedback for all submitted files on a task's dropbox.

    Provide either task_id (numeric) or task_url (full ManageBac URL).
    Returns comment text, rubric scores, and any teacher-attached files for
    each submission.

    PREFER passing the full task URL (from list_tasks results) to avoid
    expensive task-list resolution.

    Args:
        task_id: Numeric task ID (e.g. "1000099")
        task_url: Full task URL (e.g. "https://myschool.managebac.cn/student/classes/1000001/core_tasks/1000099")
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    target = task_url or task_id
    if not target:
        return json.dumps({"error": "Provide task_id or task_url"})

    # This tool's copy was the only one with the right *shape* — snapshot, then
    # `crawl_all`, then `find_task_by_id` — but with its own page budgets (5 and
    # 10) and no validation of the target, so it accepted a URL the other tools
    # refuse.  The CLI's ladder is now the single copy all three share.
    try:
        class_id, resolved_id = _class_and_task(client, target)
    except InvalidToolInput as exc:
        return _invalid_input(exc)

    result = client.get_teacher_feedback(class_id, resolved_id)
    return json.dumps(result, indent=2, ensure_ascii=False)



# ── Notifications ───────────────────────────────────────────────────────


@mcp.tool()
def get_notifications(
    page: int = 1,
    per_page: int = 20,
    unread_only: bool = False,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch notifications from ManageBac.

    Args:
        page: Page number (default 1)
        per_page: Items per page (default 20)
        unread_only: Only show unread notifications
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    hub = _hub_for(client)

    stats = hub.stats()
    filter_ = "unread" if unread_only else "all"
    result = hub.list(page=page, per_page=per_page, filter_=filter_)
    return json.dumps(
        {"stats": stats, "items": result["items"], "meta": result["meta"]},
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def mark_notification(
    notification_id: int,
    action: str = "read",
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Mark a notification as read, unread, starred, or unstarred.

    Args:
        notification_id: Numeric notification ID
        action: "read", "unread", "star", or "unstar"
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    hub = _hub_for(client)

    actions = {
        "read": hub.mark_read,
        "unread": hub.mark_unread,
        "star": hub.star,
        "unstar": hub.unstar,
    }
    fn = actions.get(action)
    if not fn:
        return json.dumps(
            {"error": f"Unknown action: {action}. Use read/unread/star/unstar"}
        )

    ok = fn(notification_id)
    return json.dumps({"ok": ok, "notification_id": notification_id, "action": action})


@mcp.tool()
def mark_all_notifications_read(
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Mark all notifications as read.

    Args:
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    hub = _hub_for(client)
    ok = hub.mark_all_read()
    return json.dumps({"ok": ok, "action": "mark_all_read"})


# ── Calendar ────────────────────────────────────────────────────────────


@mcp.tool()
def get_calendar_events(
    start_date: str | None = None,
    end_date: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch calendar events for a date range.

    Dates are YYYY-MM-DD strings. Defaults to today through +6 days.

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    # These land straight in ManageBac query params, so validate the shape
    # rather than letting the server 404 on whatever the model sent.
    try:
        start = _require_iso_date(start_date, "start_date") if start_date else date.today().isoformat()
        end = _require_iso_date(end_date, "end_date") if end_date else (date.today() + timedelta(days=6)).isoformat()
    except InvalidToolInput as exc:
        return _invalid_input(exc)

    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    events = client.get_calendar_events(start, end)
    return json.dumps(
        {"start": start, "end": end, "events": events}, indent=2, ensure_ascii=False
    )


@mcp.tool()
def get_ical_feed(
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch the raw iCal feed for the calendar.

    Returns the iCal text content. Parse with an iCal library to extract events.

    Args:
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    return client.get_ical_feed()


# ── Timetable ───────────────────────────────────────────────────────────


@mcp.tool()
def get_timetable(
    date_str: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch the weekly timetable.

    Args:
        date_str: Start date of week (YYYY-MM-DD). Defaults to this week.
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    try:
        week_of = _require_iso_date(date_str, "date_str") if date_str else None
    except InvalidToolInput as exc:
        return _invalid_input(exc)

    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    result = client.get_timetable(week_of)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Grades ──────────────────────────────────────────────────────────────


@mcp.tool()
def list_classes(
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """List all classes for the current student with their IDs.

    Use this to find the class_id for get_class_grades.

    Args:
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    # The roster `crawl_all` itself uses to discover classes: the dashboard
    # scrape (`client.get_classes`). Deriving it from task links instead — which
    # is what this did, and what `tahuti grades` still does locally — silently
    # drops every class with no tasks, because an empty class contributes no
    # link to parse. It also cost a full crawl (dashboard, every class page and
    # the notification hub) to answer a question the dashboard already answers.
    classes_map = client.get_classes()
    classes = [{"id": cid, "name": cname} for cid, cname in classes_map.items()]
    return json.dumps({"classes": classes}, indent=2, ensure_ascii=False)


@mcp.tool()
def get_class_grades(
    class_id: str | None = None,
    class_name: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Get all grades for a class with expected grade calculation.

    Provide either class_id (numeric) or class_name (fuzzy substring match).

    Args:
        class_id: Numeric class ID (e.g. "1000023")
        class_name: Fuzzy match class name (e.g. "EL" matches "CAIE IGCSE G9 EL-L0")
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    # class_id is interpolated into f"/student/classes/{class_id}/core_tasks"
    # inside client.get_class_grades, so a non-numeric value would reach the
    # server as a raw path component.
    if class_id:
        try:
            class_id = _require_numeric_id(class_id, "class_id", "1000023")
        except InvalidToolInput as exc:
            return _invalid_input(exc)

    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )

    if not class_id and class_name:
        # Same roster `list_classes` reports and `crawl_all` discovers with.
        # Resolving a name from task links instead would contradict both: a
        # class `list_classes` lists would be "not found" here.
        classes_map = client.get_classes()
        for cid, cname in classes_map.items():
            if class_name.lower() in cname.lower():
                class_id = cid
                break
        if not class_id:
            return json.dumps(
                {
                    "error": f"No class matching '{class_name}'",
                    "available": list(classes_map.values()),
                }
            )

    if not class_id:
        # Default to loading grades for all classes
        classes_map = client.get_classes()

        all_grades = {}
        for cid, cname in classes_map.items():
            try:
                c_grades = client.get_class_grades(cid)
                c_grades["class_name"] = cname
                all_grades[cid] = c_grades
            except Exception as e:
                log.warning("failed to fetch grades for class %s: %s", cid, e)
        return json.dumps({"classes_grades": all_grades}, indent=2, ensure_ascii=False)

    grades = client.get_class_grades(class_id)
    grades["class_id"] = class_id
    return json.dumps(grades, indent=2, ensure_ascii=False)


# ── Grade frequency ─────────────────────────────────────────────────────


@mcp.tool()
def count_grade_frequencies(
    class_name: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Count frequency of each grade letter across all or one class.

    Args:
        class_name: Fuzzy match class name (omit for all classes)
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    result = client.count_grade_frequencies(class_filter=class_name)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Entry point ─────────────────────────────────────────────────────────


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
