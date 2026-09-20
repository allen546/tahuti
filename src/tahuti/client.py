"""ManageBac HTTP client — login, parse task tiles, crawl all views."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from .cache import ResponseCache
from .exceptions import CommandError
from .filters import classify_task_view, is_task_submitted
from .richtext import clean_redactor_html
from .task_status import (
    SUBMISSION_NOT_SUBMITTED,
    SUBMISSION_SUBMITTED,
    normalize_submission_status,
    submission_status_from_labels,
)

log = logging.getLogger(__name__)

# Retryable HTTP status codes (server errors that may resolve on retry)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Statuses whose ``Location`` header names the next hop.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# A chain longer than this is a loop, not a redirect.
_MAX_REDIRECT_HOPS = 10

# Only these domains are acceptable targets; anything else risks sending the
# session cookie (and the login password) to an unintended host.
ALLOWED_DOMAINS = frozenset({"managebac.com", "managebac.cn"})

# A school subdomain must be a plain DNS label.
_SCHOOL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?$")

# ManageBac object ids are plain integers, so anything else is not a task id
# however it is spelled.  `task_id_from_target` is the only reader.
_TASK_ID_RE = re.compile(r"^\d+$")


class SessionExpiredError(RuntimeError):
    """The server answered with the sign-in page — the session cookie is dead.

    Subclasses :class:`RuntimeError` so existing ``except RuntimeError`` callers
    keep working, but is its own type so :meth:`ManageBacClient._get` can tell
    "the session is gone" (which must propagate) from a transport blip (where
    serving stale cached content is acceptable).
    """


# Failures that a stale cache hit must never paper over.  Both describe the
# *current* session — the credentials are dead, or a security policy refused the
# request — so answering with last cycle's cached grades would convert a hard
# stop into a silently wrong result.
_NEVER_MASK_ERRORS = (CommandError, SessionExpiredError)

# How much longer a value *derived* from a page may outlive that page's cache
# entry.  The webcal token is the only such value.  It was measured
# byte-identical across samples 901 s apart — two full default TTL windows —
# while the page body rotated on every render and the session cookie rotated
# between samples, so it is stable on a longer horizon than the page is cheap to
# re-fetch.  901 s is the bound of the evidence, not of the token's lifetime, so
# `_webcal_url` recovers by re-deriving from a fresh page if a stale token is
# ever rejected.
DERIVED_TTL_MULTIPLIER = 24


def _school_display_tz(moment: Any = None) -> Any:
    """The timezone ManageBac's human-readable dates are written in.

    ManageBac renders school-local wall-clock times ("September 15, 2026 at
    23:59") with no offset.  There is no per-school timezone setting, so the
    assumption is made explicit here: **the school's clock is assumed to be this
    machine's clock.**  That assumption is what makes the daemon's reminders
    drift by the host/school offset; if a per-school offset is ever configured,
    change this function and nothing else needs to move.

    Returns an aware ``tzinfo`` rather than ``None`` so :func:`parse_due_date`
    can hand back one unambiguous type — mixing naive and aware datetimes makes
    ``sorted`` raise ``TypeError``.

    *moment* selects which offset to return.  It matters because every caller
    attaches the result with ``replace(tzinfo=...)``, which applies one fixed
    offset: returning a DST-aware zone object would still pin the offset in
    force at the moment the call happens.  So the offset is resolved for the
    date being parsed, which is what "the school's clock read this at that date"
    actually means.
    """
    # `time.daylight` is nonzero whenever a DST *rule* is defined for the zone,
    # not whenever DST is in effect, so the previous
    # `time.altzone if time.daylight else time.timezone` returned the summer
    # offset all year round: on Europe/Berlin a January due date came back as
    # UTC+2 instead of UTC+1, putting every winter task an hour ahead of where
    # `classify_task_view` put it and firing reminders early for the whole
    # standard-time season.
    #
    # `datetime.now().astimezone().tzinfo` cannot fix this — it hands back a
    # *fixed-offset* `timezone(+2, 'CEST')`, which pins the offset in force right
    # now and applies it to every date. Resolving the IANA zone by name is what
    # actually knows which offset any given moment takes.
    zone = _local_iana_zone()
    if zone is not None and moment is not None:
        probe = moment.replace(tzinfo=None) if moment.tzinfo else moment
        return probe.replace(tzinfo=zone).tzinfo
    # No moment, or no IANA zone available (e.g. a UTC-offset-only TZ): fall back
    # to the current fixed offset, which is right at least for dates near today.
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    return timezone(offset) if offset is not None else timezone.utc


def _local_iana_zone():
    """The local timezone as an IANA ``ZoneInfo``, or ``None``.

    ``datetime.now().astimezone().tzinfo`` reports a fixed offset whose *name*
    is the zone abbreviation ("CEST"), not the zone key, so the key is recovered
    from the environment instead: ``TZ`` when set, otherwise the symlink
    ``/etc/localtime`` points at.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - stdlib since 3.9
        return None

    key = os.environ.get("TZ", "").strip()
    if key:
        # POSIX allows a leading colon; glibc writes /etc/localtime's zone name
        # there plain. `TZ=UTC` (or "UTC0", "GMT") means exactly that, so it must
        # not fall through to the /etc/localtime symlink below.
        name = key[1:] if key.startswith(":") else key
        try:
            return ZoneInfo(name)
        except Exception:
            if name.upper().startswith(("UTC", "GMT")):
                return timezone.utc
    else:
        # /etc/localtime -> /usr/share/zoneinfo/Europe/Berlin
        try:
            link = os.path.realpath("/etc/localtime")
            marker = f"{os.sep}zoneinfo{os.sep}"
            if marker in link:
                return ZoneInfo(link.split(marker, 1)[1])
        except Exception:
            pass
    return None


def _absolute_event_url(base: str, url: object) -> str | None:
    """Resolve a FullCalendar ``url`` field against *base*, or ``None``.

    FullCalendar serializes a missing link as ``"url": null`` rather than
    omitting the key, so ``dict.get("url", "")`` returns ``None`` and the naive
    ``.startswith("/")`` raised ``AttributeError`` — which aborted the whole
    `tahuti calendar` listing over one link-less event, not just that event.
    """
    if not isinstance(url, str) or not url:
        return None
    return f"{base}{url}" if url.startswith("/") else url


def _validate_school_domain(school: str, domain: str) -> tuple[str, str]:
    """Normalise and validate the school subdomain and base domain.

    ``school``/``domain`` fully determine the host every authenticated request
    (password POST, session cookie, hub JWT) is sent to, so both must be
    constrained to a ManageBac host.  A value like ``evil.com/x`` would
    otherwise turn the base URL into an attacker-chosen destination.
    """
    school_clean = str(school or "").strip().lower()
    school_clean = school_clean.replace(f".{domain}", "")
    school_clean = school_clean.rstrip(".")
    if not school_clean:
        raise CommandError("invalid_school", "School subdomain must not be empty")
    if not _SCHOOL_RE.match(school_clean):
        raise CommandError(
            "invalid_school",
            f"Invalid school subdomain {school!r}: expected a plain hostname label",
        )

    domain_clean = str(domain or "").strip().lower().rstrip(".")
    if domain_clean not in ALLOWED_DOMAINS:
        raise CommandError(
            "invalid_domain",
            f"Unsupported domain {domain!r}: expected one of "
            + ", ".join(sorted(ALLOWED_DOMAINS)),
        )
    return school_clean, domain_clean

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def parse_due_date(
    due_date_str: str,
    now_ref: datetime | None = None,
    school_tz: tzinfo | None = None,
) -> datetime | None:
    """Parse a ManageBac due date into a **timezone-aware** datetime.

    Every return value carries a ``tzinfo``, so two parsed dates are always
    comparable.  See :func:`_school_display_tz` for the timezone assumption
    applied to inputs that carry no offset of their own.

    *school_tz* overrides that assumption for the naive inputs — pass the
    school's zone and "September 15, 2026 at 23:59" is read as 23:59 *there*
    rather than on the daemon host's clock.  It has to be a parameter rather
    than something the caller can fix up afterwards: once the host's zone has
    been attached the fact that the input was a bare wall-clock time is gone,
    and the school's reading cannot be recovered from the result.

    Inputs that carry their own offset keep it under every *school_tz*.

    Returns ``None`` when *due_date_str* is empty or unparseable.
    """

    def _wall_clock(naive: datetime) -> datetime:
        return naive.replace(tzinfo=school_tz or _school_display_tz(naive))

    if not due_date_str:
        return None
    try:
        cleaned = re.sub(r"^(Due:?\s*|When:?\s*)", "", str(due_date_str), flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"^[A-Za-z]+,\s*", "", cleaned).strip()
        cleaned_no_at = re.sub(r"\s+at\s+", " ", cleaned)

        # 1. Try direct ISO format if it looks like ISO
        if "-" in cleaned and ("T" in cleaned or ":" in cleaned):
            try:
                import datetime as _std_dt
                parsed_iso = _std_dt.datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
                # An ISO string with an offset keeps it; one without gets the
                # school-display timezone rather than staying naive.
                if parsed_iso.tzinfo is None:
                    parsed_iso = _wall_clock(parsed_iso)
                return parsed_iso
            except (ValueError, TypeError):
                pass

        # 2. Try formats with explicit year.
        # ManageBac renders the same date both ways — "September 15, 2026 at
        # 11:59 PM" and "September 15, 2026 at 23:59" — so both the 12-hour and
        # the 24-hour spelling need to parse.  Without the %H:%M variants the
        # 24-hour form returned None, which callers read as "no due date".
        for fmt in (
            "%B %d, %Y %I:%M %p",
            "%b %d, %Y %I:%M %p",
            "%B %d, %Y %H:%M",
            "%b %d, %Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                parsed = datetime.strptime(cleaned_no_at, fmt)
                # The offset has to be resolved for *this* date: a DST-aware zone
                # object would pin whichever offset is in force at call time.
                return _wall_clock(parsed)
            except ValueError:
                continue

        # 3. Formats without year (infer from ref year with wrapping)
        ref = now_ref or datetime.now()
        if ref.tzinfo is None:
            ref = _wall_clock(ref)
        current_year = ref.year

        dt = None
        for fmt in ("%b %d, %I:%M %p", "%B %d, %I:%M %p", "%b %d", "%B %d"):
            try:
                parsed = datetime.strptime(f"{cleaned_no_at} {current_year}", f"{fmt} %Y")
                dt = _wall_clock(parsed)
                break
            except ValueError:
                continue

        if dt:
            diff = dt - ref
            if diff.days > 180:
                dt = dt.replace(year=current_year - 1)
            elif diff.days < -180:
                dt = dt.replace(year=current_year + 1)
            return dt
    except Exception:
        pass
    return None


def parse_task_url(target: str) -> tuple[str | None, str | None]:
    """Extract (class_id, task_id) from a ManageBac URL or task identifier string."""
    if not target:
        return None, None
    m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", target)
    if m:
        return m.group(1), m.group(2)
    clean = target.rstrip("/").split("/")[-1]
    return None, clean if clean else None


def task_id_from_target(target: str) -> str:
    """Read a task id out of a task id, a full task URL, or a task path.

    One derivation of a rule that used to be written twice, with opposite
    acceptance.  ``parse_task_url``'s "last path segment" fallback will happily
    return a *class* id for a class URL, so a caller that split on
    ``"core_tasks/"`` without checking the separator was there got the entire
    input back as the "id" — and the CLI's ``view`` gate (``startswith("http")``
    OR ``"/core_tasks/" in target``) admitted every URL, so a class URL or an
    unrelated link reached that split and produced a garbage id, while the MCP
    tool refused the same input.  Both surfaces now ask here.

    A bare numeric id passes through unchanged; anything else must really carry
    ``/core_tasks/<id>``.  Refusal is a :class:`CommandError` so each caller can
    put it in its own envelope — ``client.py`` already raises these for bad
    school and task arguments, and neither the CLI nor the MCP layer should
    have to learn the other's exception type to report one bad target.
    """
    text = str(target or "").strip()
    if not text:
        raise CommandError("missing_target", "Provide task_id or task_url")
    if _TASK_ID_RE.match(text):
        return text
    _cid, tid = parse_task_url(text)
    if tid and _TASK_ID_RE.match(tid) and "/core_tasks/" in text:
        return tid
    raise CommandError(
        "invalid_target",
        "Could not read a numeric task id from "
        f"{text[:120]!r}; pass a numeric id or a full task URL containing "
        "'/core_tasks/<id>'",
    )


def _coerce_chart_points(raw: Any) -> list[float]:
    """Flatten one Highcharts series' ``data`` into a list of floats.

    ManageBac emits two shapes for the same chart:

    * flat values — ``[4]``, ``[4, 5]``
    * ``[timestamp, value]`` pairs — ``[[1700000000000, 4]]``

    Unparseable points are skipped rather than raised: one odd series must not
    take down the whole class, because ``crawl_all`` only logs a warning per
    class and the class then silently disappears from the output.
    """
    points: list[float] = []
    if not isinstance(raw, (list, tuple)):
        return points
    for point in raw:
        # A pair carries the timestamp first and the score second.
        value = point[-1] if isinstance(point, (list, tuple)) else point
        try:
            points.append(float(value))
        except (TypeError, ValueError):
            log.debug("skipping unparseable chart data point %r", point)
            continue
    return points


# ManageBac answers a rejected upload with HTTP 200 and a human-readable
# sentence, so a status-code check alone would call a failed submission a
# success.  These are the phrasings its dropbox actually returns.
_UPLOAD_FAILURE_MARKERS = (
    "file type not permitted",
    "not permitted",
    "file size exceeds",
    "exceeds the maximum",
    "maximum file size",
    "too large",
    "no file selected",
    "no file chosen",
    "no file was uploaded",
    "unsupported file",
    "could not be uploaded",
    "upload failed",
    "deadline has passed",
    "submission is closed",
    "already submitted",
)


def _detect_upload_failure(response: requests.Response) -> str | None:
    """Return a reason string when *response* shows the upload did not land.

    ``None`` means "no evidence of failure".  Deliberately conservative: an
    unrecognised body counts as success, because ManageBac's happy path is a
    bare 200 or a redirect and a false alarm would block a real submission.
    """
    if response.status_code >= 400:
        snippet = (response.text or "").strip()[:200]
        return f"HTTP {response.status_code}" + (f": {snippet}" if snippet else "")

    text = (response.text or "").strip()
    if not text:
        return None

    # A JSON envelope is unambiguous: honour ok/success/error/errors.
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for flag in ("ok", "success"):
                if flag in payload and payload[flag] is False:
                    detail = payload.get("error") or payload.get("errors")
                    return f"server reported {flag}=false" + (
                        f": {detail}" if detail else ""
                    )
            for key in ("error", "errors"):
                value = payload.get(key)
                if value:
                    return f"server reported {key}: {value}"

    # Otherwise look for ManageBac's prose failure markers.
    lowered = text.casefold()
    for marker in _UPLOAD_FAILURE_MARKERS:
        if marker in lowered:
            return f"response contained {marker!r}"
    return None


# ── Submission-state parsing ─────────────────────────────────────────────
#
# ManageBac signals submission in two places, and *neither* uses a CSS class
# containing the word "submitted" for the submitted case:
#
# * the class-grades card, where the submitted state is a green box badge —
#   ``<span class="badge color-box-green">…<span class="badge-label">Submitted</span></span>``
#   — and the unsubmitted state is ``<span class="cell not-submitted">Not
#   Submitted</span>`` (text with a space, not a hyphen);
# * the tasks-list tile suffix, whose ``f-task-score--<variant>`` modifier names
#   the state outright (``--submitted``, ``--not-assessed``, ``--assessment``,
#   ``--due``) while carrying no dropbox link at all.
#
# So the state is read from those signals and canonicalised at the boundary;
# anything else is an unlabelled task and stays unknown.

# An element whose class states the state outright.  Kept for markup that does
# carry it; the *text* is normalised regardless, because the live page writes
# "Not Submitted".
_STATE_CLASS_RE = re.compile(r"\b(submitted|not-submitted)\b")

# The badge ManageBac actually renders for a submission state.
_BADGE_CLASS_RE = re.compile(r"\b(?:badge|color-box)\b")

# The tasks-list tile suffix variant: f-task-score--submitted, --not-assessed,
# --assessment, --due.
_TILE_SCORE_VARIANT_RE = re.compile(r"f-task-score--([a-z-]+)")


def _card_submission_status(card, labels: Any = None) -> str | None:
    """Read a class-grades card's submission state as a canonical token.

    Returns :data:`~tahuti.task_status.SUBMISSION_SUBMITTED` /
    :data:`~tahuti.task_status.SUBMISSION_NOT_SUBMITTED`, or ``None`` when the
    card says nothing about submission.  ``None`` must stay ``None``: inventing a
    state here is exactly how a task the page never labelled came to be reported
    as unsubmitted (and, with no dropbox link to rescue it, as "Complete").
    """
    # 1. An element whose class states the state.  The text is normalised rather
    #    than trusted verbatim.
    for el in card.find_all(class_=_STATE_CLASS_RE):
        token = normalize_submission_status(el.get_text(strip=True))
        if token:
            return token

    # 2. The badge.  The pending badge is grey and the submitted one green, but
    #    both are `badge`, so the label text decides — never the colour.
    for el in card.find_all(class_=_BADGE_CLASS_RE):
        token = normalize_submission_status(el.get_text(strip=True))
        if token:
            return token

    # 3. Label text as a last resort.
    return submission_status_from_labels(labels)


def _card_status_text(card, labels: Any = None) -> str | None:
    """Return a class-grades card's ``status`` field exactly as the frozen
    version wrote it.

    Two vocabularies live in this one field, and the split is what makes the
    frozen classifier behave: the state-class span's text is stored **verbatim**
    (``"Not Submitted"`` — space, not hyphen), while a card with no span falls
    back to a label lookup that writes the **canonical token**.  Because
    :func:`~tahuti.task_status.get_submission_status` compares exactly, the
    verbatim spelling never matches and PENDING comes from ``has_submit_btn``,
    while the label-derived token does match.

    Collapsing both onto one spelling — canonicalising the span, as this branch
    briefly did — makes the token arm fire for span-bearing cards too, which is
    the §6 divergence.  This function exists to keep them apart.
    """
    status_el = card.find("span", class_=re.compile(r"\b(submitted|not-submitted)\b"))
    status = status_el.get_text(strip=True) if status_el else None
    if not status:
        labels_lower = [l.lower() for l in (labels or [])]
        if "submitted" in labels_lower:
            status = "submitted"
        elif "pending" in labels_lower or "not submitted" in labels_lower:
            status = SUBMISSION_NOT_SUBMITTED
    return status


# Wording ManageBac puts on a control that hands work in.
_SUBMIT_CONTROL_KEYWORDS = ("submit coursework", "upload submission", "submit")

# Headings, whose anchors navigate to the task rather than acting on it.
_HEADING_NAMES = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _is_submit_control(el) -> bool:
    """Return True if *el* is a control that hands work in.

    This replaces a scan for "any ``<a>`` or ``<button>`` whose text contains
    the word *submit*", which over-matched in three ways, each of which made a
    task look actionable when it was not:

    * Nothing was excluded, so a card's own **title link** counted.  Its text is
      the task title, so a task really named "Submitted reading log" was offered
      an upload it does not have — and since the class path reaches PENDING
      through ``has_submit_btn`` *alone*, that alone moved it from ``past`` to
      ``overdue``.  Heading anchors navigate to a task; they never act on one.
    * It read the element's whole subtree, so a wrapping anchor inherited the
      wording of everything nested inside it.  A control labels *itself*, so an
      element wrapping a heading or another control is a container, not a
      control, and its wording belongs to what it wraps.
    * On the detail page it scanned the entire document, so site chrome and nav
      could supply the match.  That call site passes ``main_content`` instead.

    The separate ``href`` test for a ``/dropbox`` link is unaffected and still
    covers the whole document.
    """
    if el.name not in ("a", "button"):
        return False
    if el.find_parent(_HEADING_NAMES):
        return False
    # A wrapper around other content is not itself a control.
    if el.find(_HEADING_NAMES + ["a", "button"]):
        return False
    # A control whose label lives in a nested element (<a><span>Submit</span></a>)
    # still counts; its whole subtree *is* the label at this point.
    own = "".join(el.find_all(string=True, recursive=False)).strip().lower()
    text = own or el.get_text(" ", strip=True).lower()
    return any(kw in text for kw in _SUBMIT_CONTROL_KEYWORDS)


def _tile_score_variant(score_div) -> str | None:
    """Return a tasks-list tile's ``f-task-score--<variant>`` modifier, if any.

    A bare ``f-task-score`` (older markup, no modifier) returns ``None`` — an
    absent variant says nothing and must not be guessed at.
    """
    if score_div is None:
        return None
    classes = " ".join(score_div.get("class", []) or [])
    match = _TILE_SCORE_VARIANT_RE.search(classes)
    return match.group(1) if match else None


class ManageBacClient:
    """HTTP client for ManageBac with session-based auth.

    Parameters
    ----------
    school : str
        School subdomain, e.g. ``"myschool"`` for ``myschool.managebac.cn``.
    domain : str
        Base domain.  ``"managebac.com"`` (default) or ``"managebac.cn"``
        for mainland-China instances.
    """

    def __init__(
        self,
        school: str,
        domain: str = "managebac.com",
        cache: ResponseCache | None = None,
        verify: bool | str = True,
        retry: int = 3,
        request_delay: float = 1.0,
    ):
        self.school, self.domain = _validate_school_domain(school, domain)
        self.base = f"https://{self.school}.{self.domain}"
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.verify = verify
        self.student_name: str | None = None
        self.cache = cache or ResponseCache()
        # A value derived from a page gets its own, longer TTL, because the
        # page's TTL is the wrong unit for it.  The webcal token is the case in
        # point: it is 36 characters scraped out of 171 KB of HTML that cannot
        # be revalidated, so sharing one TTL would expire the two together and
        # re-fetch all 171 KB to re-read a value that has not changed.  Same
        # directory, so `logout`'s clear() still sweeps it.  See _webcal_url.
        self._derived_cache = ResponseCache(
            cache_dir=self.cache.cache_dir,
            ttl=self.cache.ttl * DERIVED_TTL_MULTIPLIER,
            enabled=self.cache.enabled,
        )
        # Clamped: `--retry` is a plain int with no floor, and `range(retry + 1)`
        # on a negative value never runs the loop body, so the retry wrapper fell
        # through to `raise last_exc` with `last_exc` still None —
        # `TypeError: exceptions must derive from BaseException` instead of a
        # usable error about the request that failed. The help text documents
        # "0=off", so anything below 0 means the same thing.
        self.retry = max(0, retry)
        self.request_delay = request_delay
        self._last_request_time: float = 0.0
        self._last_url: str | None = None
        self._url_locks: dict[str, threading.Lock] = {}
        self._url_locks_mutex = threading.Lock()

    @property
    def subdomain(self) -> str:
        """Alias for school subdomain."""
        return self.school

    @classmethod
    def from_config(cls, profile: str | None = None) -> ManageBacClient:
        """Construct an authenticated ManageBacClient from local config/credentials."""
        from .auth import build_client
        _state, client, _email = build_client(profile=profile)
        return client

    # ── Auth ────────────────────────────────────────────────────────────

    def login(self, email: str, password: str, remember: bool | None = True) -> bool:
        """Authenticate with email + password.  Returns *True* on success.

        *remember* is tri-state. ``None`` omits ``remember_me`` from the POST
        body entirely, leaving the cookie lifetime to whatever the server does
        by default — which is what ``tahuti login --no-remember-me`` asks for,
        and is a server-side decision that touches nothing on disk. ``True`` and
        ``False`` keep sending ``"1"`` / ``"0"`` as before.
        """
        r = self._request_with_retry("GET", f"{self.base}/login")
        soup = BeautifulSoup(r.text, "html.parser")
        token_el = soup.find("input", {"name": "authenticity_token"})
        if not token_el:
            log.warning("could not find authenticity_token on login page")
            return False

        form = {
            "authenticity_token": token_el["value"],
            "login": email,
            "password": password,
            "commit": "Sign in",
        }
        if remember is not None:
            form["remember_me"] = "1" if remember else "0"
        r = self._request_with_retry(
            "POST",
            f"{self.base}/sessions",
            data=form,
            allow_redirects=True,
        )

        log.debug("login POST final url=%s status=%d", r.url, r.status_code)
        # A successful login always redirects away from /sessions.
        # If we land back on /sessions (200 with no redirect) the credentials
        # were rejected by the server (wrong password, MFA, etc.).
        if "/sessions" in r.url and not r.history:
            log.warning("login failed — server returned 200 on /sessions (bad credentials?)")
            return False
        # Decide on the final path only, and require that we are still on our
        # own host — a cross-host landing page must not count as success.
        final = urlparse(r.url)
        if final.netloc.lower() != urlparse(self.base).netloc.lower():
            log.warning("login failed — redirected off-host to %s", final.netloc)
            return False
        last_segment = final.path.rstrip("/").split("/")[-1]
        if last_segment == "login":
            log.warning("login failed — redirected back to login page")
            return False
        return True

    def set_cookie(self, cookie_value: str) -> None:
        """Inject a ``_managebac_session`` cookie directly."""
        self.session.cookies.set(
            "_managebac_session",
            cookie_value,
            domain=f"{self.school}.{self.domain}",
        )

    def invalidate_cache(self) -> None:
        """Clear the entire response cache."""
        self.cache.invalidate()

    def invalidate_task_cache(self, class_id: str, task_id: str) -> None:
        """Invalidate cached HTTP responses for a specific task and its class."""
        urls = [
            f"{self.base}/student/classes/{class_id}/core_tasks/{task_id}",
            f"{self.base}/student/classes/{class_id}/core_tasks/{task_id}/dropbox",
            f"{self.base}/student/classes/{class_id}/events/{task_id}/hint",
            f"{self.base}/student/classes/{class_id}/core_tasks",
        ]
        for url in urls:
            self.cache.invalidate(url)

    # ── Retry logic ─────────────────────────────────────────────────────

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return exc.response.status_code in _RETRYABLE_STATUS_CODES
        return False

    def _assert_same_host(self, url: str) -> None:
        """Refuse to **send** an authenticated request to a host outside ManageBac.

        Call this *before* issuing the request, never on a response you already
        have.  requests follows redirects by merging the whole cookie jar into
        the new target and, on 307/308, replays the request body.  Since the
        login POST body contains the plaintext password and the jar holds
        ``_managebac_session``, a redirect off the ManageBac estate would
        exfiltrate both before any post-hoc check could run.

        Hosts within an allowed domain (e.g. the school subdomain and the
        shared calendar host ``managebac.com``) are permitted, since ManageBac
        legitimately redirects between them.
        """
        expected = urlparse(self.base).netloc.lower()
        actual = urlparse(url).netloc.lower()
        if not actual or actual == expected:
            return
        if any(
            actual == d or actual.endswith("." + d) for d in ALLOWED_DOMAINS
        ):
            return
        raise CommandError(
            "cross_host_redirect_blocked",
            f"Refusing to send authenticated request to {actual!r} "
            f"(expected {expected!r})",
        )

    def _assert_allowed_transport(self, url: str) -> None:
        """Refuse a redirect that drops TLS, even one that stays on our own name.

        ``Location: http://myschool.managebac.cn/...`` keeps the host but ships
        ``_managebac_session`` in cleartext to anything on the path.
        """
        scheme = urlparse(url).scheme.lower()
        if scheme and scheme != "https":
            raise CommandError(
                "insecure_redirect_blocked",
                f"Refusing to follow redirect to non-HTTPS URL {url!r}",
            )

    @staticmethod
    def _strip_body(kwargs: dict) -> dict:
        """Drop the request body for a hop that turns a POST into a GET."""
        for key in ("data", "files", "json"):
            kwargs.pop(key, None)
        return kwargs

    def _respect_rate_limit(self) -> None:
        now = time.time()
        elapsed = now - getattr(self, "_last_request_time", 0.0)
        min_delay = getattr(self, "request_delay", 1.0)
        if elapsed < min_delay:
            sleep_time = (min_delay - elapsed) * random.uniform(0.75, 1.25)
            time.sleep(max(0.0, sleep_time))
        self._last_request_time = time.time()

    def _follow_redirects_safely(
        self,
        method: str,
        url: str,
        response: requests.Response,
        kwargs: dict,
        headers: dict,
    ) -> requests.Response:
        """Follow redirects by hand, checking every hop **before** it is sent.

        Automatic redirect-following is disabled precisely so this runs first:
        see :meth:`_assert_same_host` for what a foreign ``Location`` would
        otherwise leak.  Same-host redirects are followed normally, including
        the 307/308 body replay that ManageBac relies on for form resubmission.
        """
        for _hop in range(_MAX_REDIRECT_HOPS):
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = (response.headers.get("Location") or "").strip()
            if not location:
                # A 3xx with no Location is malformed; hand it back and let
                # raise_for_status() decide what it is.
                return response
            next_url = urljoin(response.url, location)
            # Both gates run while the request still does not exist.
            self._assert_allowed_transport(next_url)
            self._assert_same_host(next_url)

            next_method = method
            next_kwargs = dict(kwargs)
            next_kwargs["allow_redirects"] = False
            if response.status_code == 303:
                # 303 See Other always becomes a bodyless GET.
                next_method = "GET"
                self._strip_body(next_kwargs)
            elif response.status_code in (301, 302) and method not in ("GET", "HEAD"):
                # Historical clients downgrade a 301/302 after a POST to GET.
                next_method = "GET"
                self._strip_body(next_kwargs)
            # 307/308 deliberately keep method *and* body — that is the whole
            # point of "temporary/permanent redirect" versus "see other" — and
            # it is safe here precisely because the host was just re-validated.

            hop_headers = dict(headers)
            hop_headers["Referer"] = response.url
            status = response.status_code
            previous_url = response.url
            response.close()
            self._respect_rate_limit()
            log.debug("following %d %s -> %s", status, previous_url, next_url)
            response = self.session.request(
                next_method, next_url, headers=hop_headers, **next_kwargs
            )

        raise CommandError(
            "redirect_loop",
            f"Too many redirects (> {_MAX_REDIRECT_HOPS}) from {url!r}",
        )

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        # Redirects are followed by _follow_redirects_safely so every hop is
        # host-checked before the request exists.  Disabling requests' own
        # following here is the fix, not an optimisation: with it enabled the
        # cookie jar and (on 307/308) the body have already gone by the time any
        # check of ours can run.
        if kwargs.pop("allow_redirects", False):
            log.debug(
                "%s %s: allow_redirects ignored — redirects are validated hop by hop",
                method,
                url,
            )
        kwargs["allow_redirects"] = False

        self._respect_rate_limit()

        headers = kwargs.pop("headers", {}) or {}
        if self._last_url and "Referer" not in headers:
            headers["Referer"] = self._last_url
        last_exc: Exception | None = None
        for attempt in range(self.retry + 1):
            try:
                r = self.session.request(method, url, headers=headers, **kwargs)
                r = self._follow_redirects_safely(method, url, r, kwargs, headers)
                # Belt and braces: every hop was validated on the way out.
                # Re-checking the final URL means a future edit here cannot
                # silently downgrade the guard to post-hoc detection again.
                self._assert_allowed_transport(r.url)
                self._assert_same_host(r.url)
                r.raise_for_status()
                self._last_url = url
                return r
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.retry:
                    delay = 2**attempt
                    log.warning(
                        "%s %s failed (attempt %d/%d), retrying in %ds...",
                        method,
                        url,
                        attempt + 1,
                        self.retry + 1,
                        delay,
                    )
                    time.sleep(delay)
            except requests.HTTPError as exc:
                if self._is_retryable(exc) and attempt < self.retry:
                    last_exc = exc
                    delay = 2**attempt
                    log.warning(
                        "%s %s returned %d (attempt %d/%d), retrying in %ds...",
                        method,
                        url,
                        exc.response.status_code,
                        attempt + 1,
                        self.retry + 1,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    # ── Internal helpers ────────────────────────────────────────────────

    def _get_url_lock(self, url: str) -> threading.Lock:
        with self._url_locks_mutex:
            if url not in self._url_locks:
                self._url_locks[url] = threading.Lock()
            return self._url_locks[url]

    def _get(self, path: str, bypass_cache: bool = False) -> BeautifulSoup:
        url = f"{self.base}{path}"
        if not bypass_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return self._soup_from_cached(url, cached[0])

        lock = self._get_url_lock(url)
        with lock:
            if not bypass_cache:
                cached = self.cache.get(url)
                if cached is not None:
                    return self._soup_from_cached(url, cached[0])

            try:
                r = self._request_with_retry("GET", url)
                soup = BeautifulSoup(r.text, "html.parser")
                # Reject before caching, so a login page is never written under
                # a real /student/... URL to be served for a whole TTL.
                self._reject_login_page(r.url, soup)
                self.cache.put(url, r.text, r.status_code)
                self._capture_student_name(soup)
                return soup
            except Exception as e:
                # A stale entry may paper over a *transient* transport failure.
                # It must never paper over a dead session or a refused security
                # policy: both are facts about the current credentials, so
                # answering with last cycle's grades (or with cached content
                # behind a blocked redirect) turns a hard stop into a silently
                # wrong result.
                if isinstance(e, _NEVER_MASK_ERRORS):
                    raise
                cached = self.cache.get(url, allow_stale=True)
                if cached is not None:
                    body, status = cached
                    log.warning(
                        "Request to %s failed (%s) — SERVING STALE CACHED CONTENT "
                        "for %s (cached status %s); this data may be out of date",
                        url,
                        e,
                        path,
                        status,
                    )
                    soup = BeautifulSoup(body, "html.parser")
                    self._capture_student_name(soup)
                    return soup
                raise

    def _soup_from_cached(self, url: str, body: str) -> BeautifulSoup:
        """Turn a cache hit into soup, rejecting anything that is a login page."""
        soup = BeautifulSoup(body, "html.parser")
        self._reject_login_page(url, soup)
        self._capture_student_name(soup)
        return soup

    # A genuine ManageBac sign-in form posts to /sessions.
    _LOGIN_FORM_ACTION_RE = re.compile(r"/sessions?(?:[?#/]|$)")
    _LOGIN_ID_FIELD_RE = re.compile(r"^(login|email|user_?name|user)$", re.IGNORECASE)

    def _looks_like_login_page(self, soup: BeautifulSoup) -> bool:
        """True when *soup* is ManageBac's sign-in page rather than real content.

        The decision has to come from the body.  The previous check looked for
        ``/login`` in the *requested* URL, which no real student path contains —
        dead logic — so a login page served at 200 was parsed as an empty task
        list and reported as a successful, empty crawl.
        """
        if soup.find("form", action=self._LOGIN_FORM_ACTION_RE):
            return True
        password = soup.find("input", attrs={"type": "password"})
        if password is None:
            return False
        # A password field alone is not proof: /student/profile carries a
        # change-password form.  Require the login/email identifier that only a
        # sign-in form pairs with it.
        if soup.find("input", attrs={"name": self._LOGIN_ID_FIELD_RE}):
            return True
        form = password.find_parent("form")
        if form is not None and re.search(
            r"sign[_ -]?in|log[_ -]?in",
            " ".join(form.get("class", [])) + " " + str(form.get("id", "")),
            re.IGNORECASE,
        ):
            return True
        return False

    def _reject_login_page(self, url: str, soup: BeautifulSoup) -> None:
        """Raise :class:`SessionExpiredError` if *soup* is a login page."""
        if "/login" in url:
            raise SessionExpiredError(
                "Session expired or invalid — redirected to login"
            )
        if self._looks_like_login_page(soup):
            raise SessionExpiredError(
                f"Session expired or invalid — {url} returned the sign-in page"
            )

    def _capture_student_name(self, soup: BeautifulSoup) -> None:
        if self.student_name:
            return
        for a in soup.find_all("a", href="/student/profile"):
            text = a.get_text(strip=True)
            if text and "Manage" in text:
                self.student_name = (
                    text.split("Manage")[0].strip().rstrip("\u2014\u2015- ")
                )
                return

    # ── Tile parsing (Faria "f-tile" UI) ────────────────────────────────

    def _parse_tile(self, tile) -> dict | None:
        a = tile.find("a", class_=re.compile(r"f-tile__title-link"))
        if not a:
            return None
        title = a.get_text(strip=True)
        link = a.get("href", "")

        # Task ID & Class ID
        task_id = None
        if "core_tasks/" in link:
            task_id = link.split("core_tasks/")[-1].rstrip("/")
        elif link:
            task_id = link.rstrip("/").split("/")[-1]

        class_id = None
        if link:
            m_cls = re.search(r"/student/classes/(\d+)/", link)
            if m_cls:
                class_id = m_cls.group(1)

        # Due date & class name
        desc = tile.find("div", class_="f-tile__description")
        due_date = class_name = None
        if desc:
            for s in desc.find_all("span", recursive=True):
                t = s.get_text(strip=True)
                cls = s.get("class", [])
                if not t or "badge" in str(cls) or "fi" in str(cls):
                    continue
                if not due_date and re.search(r"[A-Z][a-z]{2}\s+\d", t):
                    due_date = t
                    break
            class_link = desc.find("a", href=re.compile(r"/student/classes/"))
            if class_link:
                class_name = class_link.get_text(strip=True)
                if not class_id and class_link.get("href"):
                    m_cls2 = re.search(r"/student/classes/(\d+)/", class_link.get("href", ""))
                    if m_cls2:
                        class_id = m_cls2.group(1)

        # Labels / badges
        labels = []
        for badge in tile.find_all("span", class_=re.compile(r"^badge$|color-box")):
            badge_text = badge.get_text(strip=True)
            if badge_text and badge_text not in labels:
                labels.append(badge_text)

        # Grade & Suffix Parsing
        grade_letter = None
        grade_score = None
        has_submit_button = False
        # The submission state the tile's suffix declares, if any.  Read from the
        # suffix *variant* class, which is the only signal the current UI gives:
        # none of the 47 tiles on a live tasks page carries a dropbox link or a
        # "Submit" control, so the submit-button heuristic below can never fire
        # and every tile looked identically unsubmitted-but-actionable.
        declared_status = None

        suffix = tile.find("div", class_=re.compile(r"f-tile__suffix"))
        score_div = suffix.find("div", class_=re.compile(r"f-task-score")) if suffix else None
        variant = _tile_score_variant(score_div)
        if score_div:
            if variant == "submitted":
                declared_status = SUBMISSION_SUBMITTED
            elif variant == "due":
                # Nothing handed in: the suffix falls back to the due date.
                declared_status = SUBMISSION_NOT_SUBMITTED
            elif variant == "not-assessed":
                # "Not Assessed Yet" — parsed as the grade the page shows, and
                # deliberately *not* as a submission state: the tile does not say
                # the work was handed in, and guessing it would invent a state.
                body = score_div.find(class_=re.compile(r"f-task-score__body")) or score_div
                text = body.get_text(" ", strip=True)
                if text:
                    grade_letter = text
            else:
                # `--assessment` (a real grade) or a bare `f-task-score` in older
                # markup: read the grade box.
                h4 = score_div.find("h4")
                p = score_div.find("p")
                grade_letter = h4.get_text(strip=True) if h4 else None
                raw_score = p.get_text(" ", strip=True) if p else None
                # Grade score must be a genuine score/percentage/points, never a lifecycle badge
                if raw_score and not any(k in raw_score.lower() for k in ["submitted", "pending", "task", "due", "not"]):
                    grade_score = raw_score
        else:
            # Suffix does not contain a grade box; extract status badges and action links
            for el in (suffix.find_all(["span", "div", "a"]) if suffix else []):
                txt = el.get_text(strip=True)
                if txt and txt not in labels:
                    labels.append(txt)
                href = str(el.get("href", "")).lower()
                if "dropbox" in href or any(kw in txt.lower() for kw in ("submit", "upload")):
                    has_submit_button = True

        from .task_status import SubmissionStatus, get_submission_status

        if declared_status is None:
            declared_status = submission_status_from_labels(labels)

        parsed = {
            "title": title,
            "link": f"{self.base}{link}" if link.startswith("/") else link,
            "id": task_id,
            "task_id": task_id,
            "class_id": class_id,
            "due_date": due_date,
            "class_name": class_name,
            "labels": labels or None,
            "grade_letter": grade_letter,
            "grade_score": grade_score,
            "has_submit_button": has_submit_button,
        }
        sub_status = get_submission_status(parsed)
        parsed["submission_status"] = sub_status.value
        # The page's own declared state, kept for a future approved rule.  It
        # deliberately does *not* feed classification — see below.
        parsed["tile_declared_status"] = declared_status
        # `status` is asserted, not proven: any tile that cannot be shown
        # submitted is written "not-submitted", which the frozen classifier reads
        # as PENDING, so every unsubmitted tile is actionable and a past-due one
        # lands in `overdue`.  That is the frozen version's behaviour and the one
        # that ran a week of live daemon + webhook traffic.  Narrowing this to
        # only the tiles that say so outright — the change this branch had made —
        # moved 5 tasks from `overdue` to `past` and is reverted here.
        parsed["status"] = "submitted" if sub_status == SubmissionStatus.SUBMITTED else "not-submitted"
        if sub_status == SubmissionStatus.PENDING:
            parsed["has_submit_button"] = True
        return parsed

    def _parse_tasks_page(self, soup: BeautifulSoup) -> list[dict]:
        self._capture_student_name(soup)
        tiles = soup.find_all("div", class_=re.compile(r"f-task-tile"))
        return [t for tile in tiles if (t := self._parse_tile(tile))]

    # Pagination controls are recognised structurally, not by a bare "next"
    # substring: a lesson's "Next lesson" link is not a page control.
    _PAGE_LINK_RE_CACHE: dict[int, re.Pattern] = {}
    _PAGINATION_WORD_RE = re.compile(
        r"\b(pagination|pager|page-item|f-pagination|prev|previous|next|older)\b",
        re.IGNORECASE,
    )

    def _links_to_page(self, soup: BeautifulSoup, page: int) -> bool:
        """True when some anchor's query string carries ``page=<page>`` exactly.

        The match must be anchored on the value: an unanchored ``page=2`` also
        matches ``page=20``, so on page 1 a widget linking pages 20-24 looked
        like a next page.  The crawl then requested an out-of-range page and,
        if that 404'd, ``_get`` had no stale entry and the whole crawl aborted.
        """
        pattern = self._PAGE_LINK_RE_CACHE.get(page)
        if pattern is None:
            pattern = re.compile(rf"(?:[?&]|^)page={page}(?![0-9])")
            self._PAGE_LINK_RE_CACHE[page] = pattern
        for a in soup.find_all("a", href=pattern):
            return True
        return False

    @classmethod
    def _is_next_control(cls, el) -> bool:
        """True when *el* is a genuine next-page control.

        Requires both a "next"-ish token and evidence that the control is about
        *pages* — either a pagination-flavoured class, or an accessible name /
        label that mentions a page.  ``aria-label="Next lesson"`` fails the
        second test, so it no longer drives an extra fetch.
        """
        classes = " ".join(el.get("class", []) or [])
        aria = el.get("aria-label", "") or ""
        if not re.search(r"\b(next|older)\b", f"{classes} {aria}", re.IGNORECASE):
            return False
        if el.get("disabled") is not None or "disabled" in classes.lower():
            return False
        label = el.get_text(" ", strip=True)
        if re.search(r"\bpage", f"{aria} {label}", re.IGNORECASE):
            return True
        return bool(cls._PAGINATION_WORD_RE.search(classes))

    def _has_next_page(self, soup: BeautifulSoup, page: int, view: str) -> bool:
        next_page = page + 1
        # 1. A link whose query string names the next page number exactly.
        if self._links_to_page(soup, next_page):
            return True
        # 2. An explicit rel="next" — the canonical pagination hint.
        if soup.find("a", rel="next") or soup.find("button", rel="next"):
            return True
        # 3. A next-page control that is not disabled.
        for el in soup.find_all(["a", "button"]):
            if self._is_next_control(el):
                return True
        return False

    def _text_from_block(self, node, limit: int | None = None) -> str | None:
        if not node:
            return None
        text = node.get_text("\n", strip=True)
        if not text:
            return None
        return text[:limit] if limit else text

    def _is_downloadable_attachment_url(self, resolved_url: str, raw_href: str) -> bool:
        """True when an attachment URL is safe to hand to the download path.

        ``_extract_attachments`` passes any absolute href straight through, so a
        task page carrying ``https://cdn.evil.test/payload.pdf`` (or a plaintext
        ``http://myschool.managebac.com/...``) put that URL in the download list,
        where ``cmd_download`` fetches it with the authenticated session and
        writes the body into the output directory.  Filtering here keeps the
        decision in the client, next to the other host checks.

        Same rules as every other request: HTTPS only, and a host inside the
        ManageBac estate.
        """
        parsed = urlparse(resolved_url)
        if parsed.scheme.lower() != "https":
            return False
        try:
            self._assert_same_host(resolved_url)
        except CommandError:
            return False
        return True

    def _extract_attachments(self, soup: BeautifulSoup) -> list[dict]:
        attachments: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for link in soup.find_all("a", href=True):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            lower_href = href.casefold()
            if lower_href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue

            link_classes = " ".join(link.get("class", []))
            parent_classes = (
                " ".join(link.parent.get("class", [])) if link.parent else ""
            )
            context = f"{link_classes} {parent_classes}".casefold()
            text = link.get_text(" ", strip=True)
            filename_hint = href.split("?")[0].rstrip("/").split("/")[-1]
            looks_like_file = bool(
                re.search(r"\.[a-z0-9]{2,8}$", filename_hint, re.IGNORECASE)
            )
            attachmentish = (
                "fr-file" in context
                or "/attachments/" in lower_href
                or "/uploads/" in lower_href
                or any(
                    marker in context
                    for marker in (
                        "attachment",
                        "resource",
                        "upload",
                        "download",
                        "file",
                    )
                )
            )
            if not (looks_like_file or attachmentish):
                continue

            url = urljoin(f"{self.base}/", href)
            if not self._is_downloadable_attachment_url(url, href):
                log.warning(
                    "Skipping attachment link %r (shown as %r): not an HTTPS URL on "
                    "the ManageBac estate — downloading it would send the session "
                    "cookie to a third party",
                    href,
                    link.get_text(" ", strip=True),
                )
                continue
            source = "description"
            if link.find_parent(class_=re.compile(r"discussion", re.IGNORECASE)):
                source = "discussion"
            elif (
                link.find_parent(class_=re.compile(r"dropbox|submission|coursework", re.IGNORECASE))
                or link.find_parent("tr", class_=re.compile(r"file", re.IGNORECASE))
            ):
                source = "submission"

            name = (
                text or link.get("data-name") or filename_hint or url.rsplit("/", 1)[-1]
            )
            if not name:
                continue
            name = unquote(name)
            if source == "description":
                data_name = link.get("data-name")
                if data_name:
                    name = unquote(data_name)
                elif (
                    text
                    and "\n" not in text
                    and re.search(r"\.[a-z0-9]{2,8}(?:\s|$)", text, re.IGNORECASE)
                ):
                    name = unquote(text.splitlines()[0].strip())
            elif source == "submission" and text.casefold() in {
                "view teacher feedback",
                "view feedback",
            }:
                continue

            # The query string is part of the file's identity: ManageBac serves
            # revisioned attachments as essay.pdf?v=1 / essay.pdf?v=2.  Dropping
            # it collapsed distinct files into one — the only dedup key in the
            # codebase that omitted the query.
            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                {
                    "name": name,
                    "url": url,
                    "source": source,
                }
            )

        return attachments

    # ── CSRF helper ─────────────────────────────────────────────────────

    def _get_csrf(self, soup: BeautifulSoup) -> str | None:
        meta = soup.find("meta", {"name": "csrf-token"})
        return meta["content"] if meta else None

    # ── Notifications ───────────────────────────────────────────────────

    def get_notification_token(self, bypass_cache: bool = False) -> tuple[str, str]:
        """Extract MNN hub endpoint and JWT from the notifications page.

        Returns ``(hub_endpoint, jwt_token)``.  ``hub_endpoint`` is whatever the
        page said — pass it through :meth:`_validated_hub_endpoint` before use,
        since it is attacker-influenced scraped HTML.
        """
        soup = self._get("/student/notifications", bypass_cache=bypass_cache)
        trigger = soup.find("a", class_="js-messages-and-notifications-trigger")
        if not trigger:
            raise RuntimeError("Could not find notification trigger on page")
        return (
            trigger.get("data-mnn-hub-endpoint", ""),
            trigger.get("data-token", ""),
        )

    def _validated_hub_endpoint(self, scraped: str) -> str:
        """Return the only hub origin the JWT may be sent to.

        ``data-mnn-hub-endpoint`` comes verbatim out of scraped ManageBac HTML,
        so a compromised page, a poisoned edge, or a TLS-stripping MITM chooses
        it.  Handing it to ``MNNHubClient`` unexamined put the ``Authorization:
        Bearer <jwt>`` header on whatever host was named — and an ``http://``
        endpoint shipped the token in cleartext.

        The scraped value is used only when it is https **and** its host is one
        of the Faria-operated hubs in ``notifications.HUB_ENDPOINTS`` (which
        contains the expected host for this domain).  Anything else — a foreign
        host, a cleartext scheme, a ``wss://`` scheme, or a userinfo spoof like
        ``https://mnn-hub.prod.faria.cn@evil.test`` — falls back to
        ``hub_for_domain(self.domain)``.
        """
        from .notifications import HUB_ENDPOINTS, hub_for_domain

        fallback = hub_for_domain(self.domain)
        candidate = (scraped or "").strip()
        if not candidate:
            return fallback

        parsed = urlparse(candidate)
        host = parsed.netloc.lower()
        if parsed.scheme.lower() != "https" or not host:
            log.warning(
                "Ignoring scraped MNN hub endpoint %r (not https); using %s",
                candidate,
                fallback,
            )
            return fallback
        # Reject "user@host" and "host:port" spellings outright: urlparse folds
        # both into netloc, so a naive startswith check would be fooled.
        if "@" in host or ":" in host:
            log.warning(
                "Ignoring scraped MNN hub endpoint %r (unexpected host form %r); using %s",
                candidate,
                host,
                fallback,
            )
            return fallback

        known_hosts = {urlparse(e).netloc.lower() for e in HUB_ENDPOINTS.values()}
        if host not in known_hosts:
            log.warning(
                "Ignoring scraped MNN hub endpoint %r — host %r is not a known "
                "Faria hub; using %s",
                candidate,
                host,
                fallback,
            )
            return fallback

        return f"https://{host}"

    def _fetch_notifications(self) -> dict:
        """Pull unread count + items from the MNN hub.

        Both the light ``crawl_index`` and the full ``crawl_all`` need this, and
        the two copies had drifted into the same unvalidated-endpoint shape, so
        the logic lives here once.
        """
        hub_endpoint, token = self.get_notification_token()
        if not hub_endpoint:
            return {"unread_count": 0, "items": []}

        from .notifications import MNNHubClient

        hub = MNNHubClient(self._validated_hub_endpoint(hub_endpoint), token)
        stats = hub.stats()
        result = hub.list(page=1, per_page=10, filter_="unread")
        return {
            "unread_count": stats.get("unread_count", 0),
            "items": result.get("items", []),
        }

    # ── File submission ─────────────────────────────────────────────────

    def submit_file(self, class_id: str, task_id: str, file_path: str) -> dict:
        """Upload a file to a task's dropbox.

        Returns ``{"ok": True, "filename": ..., "task_url": ...}``.

        Raises
        ------
        RuntimeError
            If the upload did not actually land.  ManageBac signals rejection
            with a 200 and an explanatory sentence, so the response is inspected
            rather than assumed — reporting ``ok: True`` for a rejected upload
            tells the user coursework was submitted when it was not.
        """
        from pathlib import Path

        dropbox_path = f"/student/classes/{class_id}/core_tasks/{task_id}/dropbox"
        soup = self._get(dropbox_path, bypass_cache=True)
        csrf = self._get_csrf(soup)
        if not csrf:
            raise RuntimeError("Could not find CSRF token on dropbox page")

        form = soup.find("form", id=lambda x: x and x.startswith("edit_dropbox"))
        if not form:
            raise RuntimeError("Could not find upload form on dropbox page")

        upload_url = f"{self.base}/student/classes/{class_id}/core_tasks/{task_id}/dropbox/upload"
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        with p.open("rb") as fh:
            files = {
                "dropbox[assets_attributes][0][file]": (p.name, fh),
            }
            data = {
                "_method": "patch",
                "authenticity_token": csrf,
                "commit": "Upload Files",
            }
            headers = {
                "X-CSRF-Token": csrf,
                "X-Requested-With": "XMLHttpRequest",
            }
            r = self._request_with_retry(
                "POST", upload_url, data=data, files=files, headers=headers
            )

        # Verify before claiming success — this is the whole point of the check.
        failure = _detect_upload_failure(r)
        self.invalidate_task_cache(class_id, task_id)
        if failure:
            raise RuntimeError(
                f"Upload of {p.name!r} to task {task_id} did not succeed: {failure}"
            )

        task_url = f"{self.base}/student/classes/{class_id}/core_tasks/{task_id}"
        return {
            "ok": True,
            "filename": p.name,
            "task_url": task_url,
            "upload_status": r.status_code,
        }

    def get_submissions(
        self, class_id: str, task_id: str, bypass_cache: bool = False
    ) -> list[dict]:
        """List current submissions on a task page (or dropbox).

        Each entry includes ``asset_id``, ``name``, ``url``, ``uploaded_at``,
        ``can_delete``, ``delete_url``, and optional ``feedback_url`` and/or
        ``preview_modal_url``.
        """
        task_path = f"/student/classes/{class_id}/core_tasks/{task_id}"
        soup = None
        try:
            soup = self._get(task_path, bypass_cache=bypass_cache)
        except Exception:
            pass

        rows = []
        if soup:
            rows = soup.find_all("tr", class_=re.compile(r"file", re.IGNORECASE))
            if not rows:
                rows = soup.find_all("tr", id=re.compile(r"^asset_\d+", re.IGNORECASE))

        # Fallback to dropbox page if no submission rows found on task page
        if not rows:
            dropbox_path = f"{task_path}/dropbox"
            try:
                soup_drop = self._get(dropbox_path, bypass_cache=bypass_cache)
                drop_rows = soup_drop.find_all("tr", class_=re.compile(r"file", re.IGNORECASE))
                if not drop_rows:
                    drop_rows = soup_drop.find_all("tr")
                if drop_rows:
                    rows = drop_rows
                    soup = soup_drop
            except Exception as e:
                if not soup:
                    return [{"error": str(e)}]

        if not rows and soup:
            rows = soup.find_all("tr")

        # Extract page-level dropbox_id if form is present
        page_dropbox_id = None
        if soup:
            form = soup.find("form", id=lambda x: x and x.startswith("edit_dropbox_"))
            if form and form.get("id"):
                m = re.search(r"edit_dropbox_(\d+)", form["id"])
                if m:
                    page_dropbox_id = m.group(1)

        submissions: list[dict] = []
        for row in rows:
            anchors = row.find_all("a", href=True)
            if not anchors:
                continue

            file_link = None
            feedback_href: str | None = None
            preview_modal_url: str | None = None
            delete_href: str | None = None
            can_delete = False

            for a in anchors:
                href = a.get("href", "")
                txt = a.get_text(strip=True).lower()
                title_attr = (a.get("title") or "").lower()
                classes = a.get("class", [])
                method = a.get("data-method", "").lower()

                if "btn-remove" in classes or method == "delete" or "destroy_asset" in href:
                    can_delete = True
                    delete_href = href
                    continue

                is_feedback = (
                    "teacher feedback" in txt
                    or "teacher feedback" in title_attr
                    or "view feedback" in txt
                    or "view feedback" in title_attr
                    or a.get("data-pdf-preview-url-value") is not None
                    or "pdf-preview" in classes
                )

                if is_feedback:
                    feedback_href = href
                    if a.get("data-pdf-preview-url-value"):
                        preview_modal_url = a.get("data-pdf-preview-url-value")
                else:
                    looks_like_file = (
                        "/attachments/" in href
                        or "/uploads/" in href
                        or bool(re.search(r"\.[a-z0-9]{2,8}(?:\?|$)", href, re.IGNORECASE))
                        or "text-break" in classes
                    )
                    if looks_like_file and file_link is None:
                        file_link = a

            if not file_link:
                continue

            href = file_link.get("href", "")
            name = file_link.get_text(strip=True) or href.split("?")[0].rstrip("/").split("/")[-1]
            if not name:
                continue

            # Extract asset_id
            asset_id = None
            row_id = row.get("id") or ""
            if row_id.startswith("asset_"):
                asset_id = row_id.split("asset_")[-1]
            elif delete_href:
                m_del = re.search(r"file_id=(\d+)", delete_href)
                if m_del:
                    asset_id = m_del.group(1)
            if not asset_id:
                m_s3 = re.search(r"/uploads/asset/file/(\d+)/", href)
                if m_s3:
                    asset_id = m_s3.group(1)

            # Extract dropbox_id
            dropbox_id = page_dropbox_id
            if delete_href:
                m_drop = re.search(r"/dropboxes/(\d+)/", delete_href)
                if m_drop:
                    dropbox_id = m_drop.group(1)

            # Extract uploaded timestamp
            uploaded_at = None
            label_el = row.find("label")
            if label_el:
                label_text = label_el.get_text(separator=" ", strip=True)
                m_time = re.search(r"Uploaded\s+(.+)$", label_text, re.IGNORECASE)
                if m_time:
                    uploaded_at = m_time.group(1).strip()
                elif label_text:
                    uploaded_at = label_text

            entry: dict = {
                "name": name,
                "url": f"{self.base}{href}" if href.startswith("/") else href,
                "asset_id": asset_id,
                "uploaded_at": uploaded_at,
                "can_delete": can_delete,
                "delete_url": delete_href,
                "dropbox_id": dropbox_id,
            }
            if feedback_href:
                entry["feedback_url"] = (
                    f"{self.base}{feedback_href}"
                    if feedback_href.startswith("/")
                    else feedback_href
                )
            if preview_modal_url:
                entry["preview_modal_url"] = (
                    f"{self.base}{preview_modal_url}"
                    if preview_modal_url.startswith("/")
                    else preview_modal_url
                )
            submissions.append(entry)

        return submissions

    def delete_submission(
        self, class_id: str, task_id: str, asset_identifier: str
    ) -> dict:
        """Delete a submitted file from a task's dropbox.

        Parameters
        ----------
        class_id : str
            Class ID.
        task_id : str
            Task ID.
        asset_identifier : str
            Asset ID (e.g. '82189817' or 'asset_82189817') or file name.

        Returns
        -------
        dict
            ``{"ok": True, "asset_id": ..., "filename": ..., "status": "deleted", ...}``.
        """
        task_path = f"/student/classes/{class_id}/core_tasks/{task_id}"
        soup = self._get(task_path, bypass_cache=True)
        csrf = self._get_csrf(soup)
        if not csrf:
            raise RuntimeError("Could not find CSRF token on task page")

        # Extract page dropbox_id if present
        page_dropbox_id = None
        form = soup.find("form", id=lambda x: x and x.startswith("edit_dropbox_"))
        if form and form.get("id"):
            m = re.search(r"edit_dropbox_(\d+)", form["id"])
            if m:
                page_dropbox_id = m.group(1)

        if not page_dropbox_id:
            try:
                soup_drop = self._get(f"{task_path}/dropbox", bypass_cache=True)
                form_drop = soup_drop.find(
                    "form", id=lambda x: x and x.startswith("edit_dropbox_")
                )
                if form_drop and form_drop.get("id"):
                    m = re.search(r"edit_dropbox_(\d+)", form_drop["id"])
                    if m:
                        page_dropbox_id = m.group(1)
            except Exception:
                pass

        submissions = self.get_submissions(class_id, task_id, bypass_cache=True)
        if not submissions:
            raise ValueError(f"No submissions found for task {task_id}")

        clean_ident = str(asset_identifier).strip()
        clean_ident_id = (
            clean_ident[6:] if clean_ident.lower().startswith("asset_") else clean_ident
        )

        target = None
        for s in submissions:
            aid = str(s.get("asset_id") or "")
            sname = str(s.get("name") or "")
            if aid and aid == clean_ident_id:
                target = s
                break
            if sname.lower() == clean_ident.lower():
                target = s
                break

        if not target:
            for s in submissions:
                sname = str(s.get("name") or "").lower()
                if clean_ident.lower() in sname:
                    target = s
                    break

        if not target:
            raise ValueError(
                f"Submission not found matching '{asset_identifier}' on task {task_id}"
            )

        asset_id = target.get("asset_id")
        filename = target.get("name")
        dropbox_id = target.get("dropbox_id") or page_dropbox_id
        if not dropbox_id:
            raise RuntimeError(f"Could not determine dropbox ID for task {task_id}")

        delete_url = (
            target.get("delete_url")
            or f"/student/dropboxes/{dropbox_id}/destroy_asset?file_id={asset_id}"
        )
        full_delete_url = (
            f"{self.base}{delete_url}" if delete_url.startswith("/") else delete_url
        )

        headers = {
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript, */*; q=0.01",
            "Referer": f"{self.base}{task_path}",
        }

        # Route through the shared wrapper, not session.request directly: the
        # URL comes from scraped HTML and the CSRF token rides in a header, so
        # it needs the same pre-send host check, rate limit and retry as every
        # other authenticated request.
        r = self._request_with_retry("DELETE", full_delete_url, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Delete request failed with HTTP {r.status_code}: {r.text[:200]}"
            )

        # Post-delete verification: fetch task page freshly and ensure file is gone
        self.invalidate_task_cache(class_id, task_id)
        subs_after = self.get_submissions(class_id, task_id, bypass_cache=True)
        if any(str(s.get("asset_id")) == str(asset_id) for s in subs_after):
            raise RuntimeError(
                f"File '{filename}' was not deleted: task deadline has passed or ManageBac server locked the submission."
            )

        task_url = f"{self.base}{task_path}"
        return {
            "ok": True,
            "asset_id": asset_id,
            "filename": filename,
            "task_url": task_url,
            "remaining_submissions": len(subs_after),
        }

    def _parse_feedback_page(
        self, url: str | None, preview_modal_url: str | None = None
    ) -> dict:
        """Fetch and parse teacher feedback details from a feedback page or preview modal.

        Extracts comment, rubric criteria, and attached/annotated files.
        """
        comment = None
        rubric: list[dict] = []
        attachments: list[dict] = []
        annotated_url: str | None = None
        err = None

        # 1. Check preview modal if available (modern ManageBac PSPDFKit annotation viewer)
        if preview_modal_url:
            modal_req_url = (
                preview_modal_url
                if preview_modal_url.startswith("http")
                else f"{self.base}{preview_modal_url}"
            )
            try:
                # Through the validated wrapper: preview_modal_url is scraped
                # HTML, and this request carries the session cookie.
                r = self._request_with_retry(
                    "GET",
                    modal_req_url,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                if r.status_code == 200:
                    ann_m = re.search(
                        r'data-download-annotated-url=\\?[\'"]([^\'"\\]+)', r.text
                    )
                    if ann_m:
                        annotated_url = ann_m.group(1).replace("&amp;", "&")
                        attachments.append(
                            {
                                "name": "annotated_feedback.pdf",
                                "url": annotated_url,
                                "type": "annotated_pdf",
                            }
                        )
            except Exception as exc:
                log.warning("Failed to fetch preview modal %s: %s", modal_req_url, exc)

        # 2. Check HTML feedback page if URL is a page path (not an S3 direct asset URL)
        if url and not re.search(r"\.(pdf|docx?|xlsx?|png|jpe?g)(\?|$)", url, re.IGNORECASE):
            path = url.replace(self.base, "") if url.startswith(self.base) else url
            try:
                soup = self._get(path, bypass_cache=True)
                comment_el = soup.find(
                    "div", class_=re.compile(r"fr-view|fix-body-margins", re.IGNORECASE)
                )
                comment = self._text_from_block(comment_el, limit=4000) if comment_el else None

                seen_criteria: set[str] = set()
                for row in soup.find_all("tr"):
                    cells = row.find_all(["td", "th"])
                    if len(cells) < 2:
                        continue
                    criterion = cells[0].get_text(strip=True)
                    score_text = cells[1].get_text(strip=True)
                    max_text = cells[2].get_text(strip=True) if len(cells) >= 3 else None
                    if not criterion or criterion.lower() in ("criterion", "criteria", "description", ""):
                        continue
                    if criterion in seen_criteria:
                        continue
                    seen_criteria.add(criterion)
                    rubric.append({"criterion": criterion, "score": score_text, "max": max_text})

                seen_attach = {a["url"] for a in attachments}
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/attachments/" not in href and "/uploads/" not in href:
                        continue
                    full_url = f"{self.base}{href}" if href.startswith("/") else href
                    if full_url in seen_attach:
                        continue
                    seen_attach.add(full_url)
                    name = a.get_text(strip=True) or href.rsplit("/", 1)[-1]
                    attachments.append({"name": name, "url": full_url, "type": "attachment"})
            except Exception as exc:
                err = str(exc)
        elif url:
            if not any(a["url"] == url for a in attachments):
                attachments.append({"name": "feedback_file", "url": url, "type": "file"})

        return {
            "comment": comment,
            "rubric": rubric,
            "attachments": attachments,
            "annotated_download_url": annotated_url,
            "error": err,
        }

    def get_teacher_feedback(self, class_id: str, task_id: str) -> dict:
        """Fetch teacher feedback for all submissions on a task.

        Returns structured feedback including comments, rubric scores,
        and teacher-annotated attachments.
        """
        task_path = f"/student/classes/{class_id}/core_tasks/{task_id}"
        grade_letter = None
        grade_score = None
        general_comments: list[str] = []

        try:
            soup = self._get(task_path)
            card = soup.find(class_="fusion-card-item")
            if card:
                score_cell = card.find(class_=re.compile(r"assessment-cell|task-score")) or card
                grade_el = score_cell.find(class_=re.compile(r"\bgrade\b"))
                if grade_el:
                    grade_letter = grade_el.get_text(strip=True)
                points_el = card.find("div", class_="points")
                if points_el:
                    raw_pt = points_el.get_text(strip=True)
                    if raw_pt and not any(k in raw_pt.lower() for k in ["submitted", "pending", "task", "due", "not"]):
                        grade_score = raw_pt

            main_content = soup.find("main") or soup
            assessment_div = main_content.find(class_="assessment-comments")
            if assessment_div:
                for b in assessment_div.find_all(
                    "div", class_=re.compile(r"fr-view|fix-body-margins", re.IGNORECASE)
                ):
                    txt = self._text_from_block(b, limit=2000)
                    if txt and txt not in general_comments:
                        general_comments.append(txt)
        except Exception as exc:
            log.warning("Could not fetch task detail for feedback metadata: %s", exc)

        submissions = self.get_submissions(class_id, task_id)
        items: list[dict] = []
        for sub in submissions:
            if "error" in sub:
                items.append({
                    "submission_name": None,
                    "feedback_url": None,
                    "comment": None,
                    "rubric": [],
                    "attachments": [],
                    "annotated_download_url": None,
                    "error": sub["error"],
                })
                continue

            feedback_url = sub.get("feedback_url")
            preview_modal = sub.get("preview_modal_url")
            entry: dict = {
                "submission_name": sub.get("name"),
                "feedback_url": feedback_url,
                "comment": None,
                "rubric": [],
                "attachments": [],
                "annotated_download_url": None,
                "error": None,
            }
            if feedback_url or preview_modal:
                parsed = self._parse_feedback_page(feedback_url, preview_modal_url=preview_modal)
                entry.update(parsed)
            items.append(entry)

        return {
            "task_id": task_id,
            "class_id": class_id,
            "task_url": f"{self.base}/student/classes/{class_id}/core_tasks/{task_id}",
            "grade": {
                "grade_letter": grade_letter,
                "grade_score": grade_score,
            },
            "general_comments": general_comments,
            "feedback_items": items,
        }

    # ── Calendar ────────────────────────────────────────────────────────

    def _get_revalidating(self, url: str) -> str:
        """GET *url*, answering a ``304`` from the stored body.

        Worth doing only where the server actually honours ``If-None-Match``.
        Measured on this origin: ``/student/events.json`` (200, 5,186 B -> 304,
        0 B) and the iCal feed (200, 14,399 B -> 304, 0 B), each confirmed by a
        bogus ETag returning 200 with the full body.  The Rails HTML pages never
        304 — their ETag is a digest of a body that rotates on every render — so
        this is deliberately *not* wired into :meth:`_get`.  See
        ``docs/http-revalidation-findings.md``.

        The stored ETag is what makes a ``304`` worth asking for and the stored
        body is what makes it answerable, so both come from the same entry —
        which by now is normally past its TTL, hence
        :meth:`ResponseCache.get_entry` rather than ``get()``.
        """
        fresh = self.cache.get(url)
        if fresh is not None:
            return fresh[0]

        lock = self._get_url_lock(url)
        with lock:
            fresh = self.cache.get(url)
            if fresh is not None:
                return fresh[0]

            stale = self.cache.get_entry(url)
            headers = {}
            if stale and stale.get("etag"):
                headers["If-None-Match"] = stale["etag"]

            r = self._request_with_retry("GET", url, headers=headers)
            if r.status_code == 304 and stale is not None:
                # Unchanged.  Replay what is already held rather than treating
                # the empty 304 body as the response.
                return stale["body"]
            # Same dead-logic trap as _get: check the body, not the URL.
            self._reject_login_page(r.url, BeautifulSoup(r.text, "html.parser"))
            self.cache.put(url, r.text, r.status_code, r.headers.get("ETag"))
            return r.text

    def _webcal_url(self, refresh: bool = False) -> str:
        """The iCal feed URL, reusing the cached token when one is still fresh.

        The page that yields the token is 171 KB of HTML that cannot be
        revalidated, and its only product is a 36-character token.  Measured:
        the token was byte-identical across samples 901 s apart — two full TTL
        windows — while the page body rotated on every render and the session
        cookie rotated between samples, so its stability is not an artifact of
        an unchanging session.  Caching it turns ~16 MB/day of page fetches into
        one, at the user's own TTL.

        The cache key names the page the token came from, so the entry lives in
        the same directory as every other cached credential and is cleared by
        ``logout``.  Its TTL is deliberately longer than the page's — sharing
        one would expire them together and re-fetch all 171 KB to re-read a
        36-character value.
        """
        key = f"{self.base}/student/calendar?__derived__=webcal_url"
        if not refresh:
            hit = self._derived_cache.get(key)
            if hit is not None:
                return hit[0]

        soup = self._get("/student/calendar", bypass_cache=refresh)
        link = soup.find("a", href=re.compile(r"webcal://"))
        if not link:
            raise RuntimeError("Could not find webcal link on calendar page")
        ical_url = link["href"].replace("webcal://", "https://")
        self._derived_cache.put(key, ical_url, 200)
        return ical_url

    def get_calendar_events(self, start: str, end: str) -> list[dict]:
        """Fetch calendar events for a date range via the JSON API.

        *start* and *end* are ``YYYY-MM-DD`` strings.
        """
        url = f"{self.base}/student/events.json?start={start}&end={end}"
        events = json.loads(self._get_revalidating(url))
        return [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "start": e.get("start"),
                "end": e.get("end"),
                "all_day": e.get("allDay", False),
                "description": BeautifulSoup(
                    e.get("description", ""), "html.parser"
                ).get_text("\n", strip=True)[:500]
                if e.get("description")
                else None,
                "type": e.get("type"),
                "category": e.get("category"),
                "url": _absolute_event_url(self.base, e.get("url")),
                "color": e.get("backgroundColor"),
            }
            for e in events
        ]

    def get_ical_feed(self) -> str:
        """Fetch the raw iCal feed content.

        Scrapes the calendar page to find the webcal token, then fetches the
        iCal file via HTTP.  Both the token and the feed are revalidated: the
        token because the page carrying it cannot be, and the feed because the
        server answers ``If-None-Match`` with a ``304``.
        """
        try:
            return self._get_revalidating(self._webcal_url())
        except Exception:
            # The cached token is the one value here that can silently go
            # stale: the probe established stability across 901 s, which cannot
            # rule out a longer cadence.  Re-derive it from a fresh page and try
            # once more, so a rotated token costs one wasted request instead of
            # a broken `calendar --ical`.
            return self._get_revalidating(self._webcal_url(refresh=True))

    # ── Timetable ───────────────────────────────────────────────────────

    def get_timetable(self, start_date: str | None = None) -> dict:
        """Scrape the weekly timetable.

        *start_date* is a ``YYYY-MM-DD`` string.  Defaults to today.
        Returns ``{"days": [...], "lessons": [...]}``.
        """
        params = ""
        if start_date:
            params = f"?start_date={start_date}"
        soup = self._get(f"/student/timetables/weekly{params}")

        table = soup.find("table", class_="f-timetable")
        if not table:
            raise RuntimeError("Could not find timetable table on page")

        # Parse column headers (day names)
        thead = table.find("thead")
        headers: list[dict] = []
        if thead:
            for th in thead.find_all("th")[1:]:  # skip "Period" column
                text = th.get_text(strip=True)
                headers.append(
                    {
                        "header": text,
                        "is_today": "table-active-th" in (th.get("class") or []),
                    }
                )

        # Parse rows
        lessons: list[dict] = []
        tbody = table.find("tbody") or table
        for row in tbody.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            period_label = cells[0].get_text(strip=True)
            for col_idx, td in enumerate(cells[1:]):
                link = td.find("a", class_=re.compile(r"f-timetable-item"))
                if not link:
                    continue

                body = link.find(class_="f-box-item__body")
                if not body:
                    continue

                # Time
                time_el = body.find("small", class_="color-secondary")
                time_slot = time_el.get_text(strip=True) if time_el else None

                # Subject / class name
                subject_el = body.find("p", class_="fw-semibold")
                subject = subject_el.get_text(strip=True) if subject_el else None

                # Skip homeroom / attendance rows with no subject
                if not subject:
                    continue

                # Year group
                year_els = body.find_all("p", class_="text-truncate")
                year = year_els[0].get_text(strip=True) if year_els else None

                # Teacher
                teacher = None
                teacher_els = [
                    p
                    for p in body.find_all("p")
                    if "text-truncate" in " ".join(p.get("class", []))
                ]
                if len(teacher_els) >= 2:
                    teacher = teacher_els[-1].get_text(strip=True)

                # Room — bare <p> with no classes (the last element)
                room = None
                all_ps = body.find_all("p")
                if all_ps:
                    last_p = all_ps[-1]
                    if not last_p.get("class"):
                        room = last_p.get_text(strip=True) or None

                # Class ID from popover URL
                content_url = link.get("data-bs-content-url", "")
                class_id = None
                m = re.search(r"ib_class_id=(\d+)", content_url)
                if m:
                    class_id = m.group(1)

                day_header = headers[col_idx] if col_idx < len(headers) else {}
                lessons.append(
                    {
                        "period": period_label,
                        "day": day_header.get("header", ""),
                        "is_today": day_header.get("is_today", False),
                        "time": time_slot,
                        "subject": subject,
                        "year": year,
                        "teacher": teacher,
                        "room": room,
                        "class_id": class_id,
                    }
                )

        return {"days": headers, "lessons": lessons}

    # ── Class grades ────────────────────────────────────────────────────

    def get_classes(self) -> dict[str, str]:
        """Fetch dashboard and extract all class IDs and class names."""
        soup = self._get("/student/dashboard")
        seen: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            match = re.search(r"^/student/classes/(\d+)/?$", href.split("?")[0].rstrip("/"))
            if match:
                class_id = match.group(1)
                name = a.get_text(" ", strip=True)
                if name and not any(kw in name.lower() for kw in ("all classes", "browse", "view")):
                    seen[class_id] = name
        return seen

    def get_class_grades(self, class_id: str, bypass_cache: bool = False) -> dict:
        """Fetch all grades for a class and compute expected grade.

        Returns ``{"tasks": [...], "categories": [...], "grade_scale": {...}, "expected_grade": ...}``.
        """
        soup = self._get(f"/student/classes/{class_id}/core_tasks", bypass_cache=bypass_cache)

        # Grade scale
        chart = soup.find("div", class_="assignments-progress-chart")
        grade_scale: dict = {}
        if chart:
            raw_labels = chart.get("data-grade-labels", "{}")
            try:
                grade_scale = {int(k): v for k, v in json.loads(raw_labels).items()}
            except Exception:
                pass

        # Category weights
        categories: list[dict] = []
        cat_table = soup.find("div", id="categories-table")
        if cat_table:
            for item in cat_table.find_all("div", class_="list-item"):
                cells = item.find_all("div", class_="cell")
                if len(cells) >= 2:
                    cat_name = cells[0].get_text(strip=True)
                    weight_str = cells[1].get_text(strip=True).rstrip("%")
                    if cat_name.lower() in ("category", ""):
                        continue  # skip header row
                    try:
                        weight = float(weight_str) / 100.0
                    except ValueError:
                        weight = 0.0
                    categories.append({"name": cat_name, "weight": weight})

        # Tasks with grades
        tasks: list[dict] = []
        for card in soup.find_all("div", class_="fusion-card-item"):
            title_el = card.find(class_="title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            link_el = title_el.find("a")
            href = link_el.get("href", "") if link_el else ""
            task_id_match = re.search(r"/core_tasks/(\d+)", href)

            # Grade
            grade_letter = None
            assessment_cell = card.find(class_=re.compile(r"assessment-cell|task-score")) or card
            grade_el = assessment_cell.find(class_=re.compile(r"\bgrade\b"))
            if grade_el:
                grade_letter = grade_el.get_text(strip=True)
            
            if not grade_letter:
                not_assessed_els = assessment_cell.find_all(class_=re.compile(r"not-assessed"))
                for el in not_assessed_els:
                    txt = el.get_text(strip=True)
                    if txt:
                        grade_letter = txt
                        break

            if not grade_letter:
                not_applicable_el = assessment_cell.find(class_=re.compile(r"not-applicable"))
                if not_applicable_el:
                    grade_letter = not_applicable_el.get_text(strip=True)
                else:
                    na_el = assessment_cell.find(lambda tag: tag.name in {"div", "span"} and tag.get_text(strip=True) == "N/A")
                    if na_el:
                        grade_letter = "N/A"

            if not grade_letter:
                status_el = assessment_cell.find(class_=re.compile(r"\b(submitted|not-submitted)\b"))
                if status_el:
                    txt = status_el.get_text(strip=True)
                    if txt and txt.lower() in ("complete", "incomplete"):
                        grade_letter = txt

            # Points
            points_el = card.find("div", class_="points")
            points_text = points_el.get_text(strip=True) if points_el else None

            # Category and badge labels
            labels: list[str] = []
            labels_set = card.find("div", class_="labels-set")
            if labels_set:
                for lbl in labels_set.find_all("div", class_="label"):
                    t = lbl.get_text(strip=True)
                    if t:
                        labels.append(t)
                for badge in labels_set.find_all("span", class_="badge-label"):
                    t = badge.get_text(strip=True)
                    if t:
                        labels.append(t)

            # Two fields, deliberately.  `status` keeps the frozen version's own
            # spelling (see `_card_status_text`), so the frozen classifier reads
            # it exactly as it always did.  `submission_status` carries the
            # canonical token read off the real signals — the green `Submitted`
            # badge, the `cell not-submitted` span — so an approved rule has a
            # trustworthy state to build on without moving a classification.
            # Six tasks have no dropbox link and no badge at all; those stay
            # None rather than being guessed into a state.
            status = _card_status_text(card, labels)
            submission_status = _card_submission_status(card, labels)

            # Parse submit button
            dropbox_link = card.find("a", href=re.compile(r"/core_tasks/\d+/dropbox"))
            has_submit_btn = bool(
                dropbox_link or card.find(lambda el: _is_submit_control(el))
            )

            # Parse due date
            due_date = None
            date_badge = card.find(class_="date-badge")
            if date_badge:
                m_el = date_badge.find(class_="month")
                d_el = date_badge.find(class_="day")
                if m_el and d_el:
                    month = m_el.get_text(strip=True)
                    day = d_el.get_text(strip=True)
                    due_date_base = f"{month} {day}"

                    due_el = card.find(class_="due-date")
                    time_str = ""
                    if due_el:
                        due_text = due_el.get_text(" ", strip=True)
                        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))", due_text)
                        if time_match:
                            time_str = time_match.group(1)

                    if time_str:
                        due_date = f"{due_date_base}, {time_str}"
                    else:
                        due_date = due_date_base

            tasks.append(
                {
                    "title": title,
                    "task_id": task_id_match.group(1) if task_id_match else None,
                    "url": f"{self.base}{href}" if href.startswith("/") else href,
                    "due_date": due_date,
                    "grade_letter": grade_letter,
                    "points": points_text,
                    "status": status,
                    "submission_status": submission_status,
                    "category": labels[0] if labels else None,
                    "labels": labels or None,
                    "has_submit_button": has_submit_btn,
                }
            )

        # Compute expected grade from chart data
        expected = self._compute_expected_grade(chart, grade_scale, categories)

        return {
            "tasks": tasks,
            "categories": categories,
            "grade_scale": grade_scale,
            "expected_grade": expected,
        }

    def _compute_expected_grade(
        self,
        chart,
        grade_scale: dict,
        categories: list[dict],
    ) -> dict | None:
        """Compute weighted expected grade from the Highcharts data-series."""
        if not chart or not grade_scale:
            return None

        raw_series = chart.get("data-series", "[]")
        try:
            series = json.loads(raw_series)
        except Exception:
            return None

        if not series:
            return None

        # The chart uses a 0-11 numeric scale mapped to letter grades
        # We need to figure out which category each task belongs to
        # from the task list — but the chart only has names.
        # Compute a simple unweighted average from the chart data.
        scores: list[float] = []
        if isinstance(series, list):
            for item in series:
                if not isinstance(item, dict):
                    continue
                points = _coerce_chart_points(item.get("data") or [])
                if points:
                    scores.append(points[0])

        if not scores:
            return None

        avg_score = sum(scores) / len(scores)
        # Clamp to scale range
        idx = max(0, min(round(avg_score), max(grade_scale.keys())))
        letter = grade_scale.get(idx, str(idx))

        return {
            "average_score": round(avg_score, 2),
            "letter_grade": letter,
            "num_graded": len(scores),
            "note": "Unweighted average from chart data",
        }

    # ── Grade frequency ─────────────────────────────────────────────────

    def count_grade_frequencies(self, class_filter: str | None = None) -> dict:
        """Count frequency of each grade letter across all or one class.

        Returns ``{"grades": {"A": 5, "B": 3, ...}, "total": N, "classes": [...]}``.
        """
        # The roster `crawl_all` itself discovers classes with: the dashboard
        # scrape. Deriving it from task links instead — which this did, and
        # which the MCP `list_classes` tool no longer does — silently drops
        # every class with no tasks, because an empty class contributes no link
        # to parse, so two MCP tools answered "which classes exist"
        # differently. It also cost a full crawl (dashboard, every class page
        # and the notification hub) to answer a question the dashboard already
        # answers.
        classes_map = self.get_classes()

        target_classes: list[tuple[str, str]]
        if class_filter:
            target_classes = [
                (cid, cn)
                for cid, cn in classes_map.items()
                if class_filter.lower() in cn.lower()
            ]
            if not target_classes:
                return {
                    "error": f"No class matching '{class_filter}'",
                    "available": list(classes_map.values()),
                }
        else:
            target_classes = list(classes_map.items())

        freq: dict[str, int] = {}
        classes_used: list[dict] = []
        for cid, cname in target_classes:
            grades = self.get_class_grades(cid)
            classes_used.append({"id": cid, "name": cname})
            for task in grades.get("tasks", []):
                letter = task.get("grade_letter")
                if letter:
                    freq[letter] = freq.get(letter, 0) + 1

        return {
            "grades": dict(sorted(freq.items())),
            "total": sum(freq.values()),
            "classes": classes_used,
        }

    # ── Public crawl methods ────────────────────────────────────────────

    def get_tasks_by_view(self, view: str, max_pages: int = 10) -> list[dict]:
        """Crawl one view (``upcoming`` / ``past`` / ``overdue``).

        Tasks are de-duplicated by id across pages: a server that echoes page 1
        for an out-of-range page would otherwise return every task twice, and
        nothing downstream removes the duplicates.
        """
        all_tasks: list[dict] = []
        seen_ids: set[str] = set()
        for page in range(1, max_pages + 1):
            soup = self._get(f"/student/tasks_and_deadlines?view={view}&page={page}")
            tasks = self._parse_tasks_page(soup)
            if not tasks:
                break
            new_count = 0
            for t in tasks:
                task_id = t.get("id") or t.get("task_id")
                if task_id:
                    if task_id in seen_ids:
                        continue
                    seen_ids.add(task_id)
                t["view"] = view
                all_tasks.append(t)
                new_count += 1
            log.info("%s page %d: %d items (%d new)", view, page, len(tasks), new_count)
            if not self._has_next_page(soup, page, view):
                break
        return all_tasks

    def get_task_detail(self, task_path: str, from_hint: bool = False, bypass_cache: bool = False) -> dict | None:
        """Fetch one task's detail page for task body, attachments, and submission info.
        
        If from_hint is True, hits the event popover hint page instead of the full detail page.
        """
        if task_path.startswith("http"):
            task_path = task_path.replace(self.base, "")

        task_match = re.search(r"(/student/classes/\d+/core_tasks/\d+)", task_path)
        if task_match:
            task_path = task_match.group(1)

        if from_hint:
            m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", task_path)
            if m:
                class_id, task_id = m.group(1), m.group(2)
                task_path = f"/student/classes/{class_id}/events/{task_id}/hint"

        try:
            soup = self._get(task_path, bypass_cache=bypass_cache)
        except Exception as exc:
            # A single unreadable task must not abort a whole crawl, but the
            # failure must not masquerade as a successful fetch either: return
            # None so every caller's `if not detail` guard fires.  A truthy
            # {"error": ...} dict slipped past those guards and was then merged
            # into task metadata as if it were a parsed detail page.
            log.warning("task detail fetch failed for %s: %s", task_path, exc)
            return None

        detail: dict = {}
        main_content = soup.find("main") or soup

        # Parse card details from the detail page if present
        card = soup.find(class_="fusion-card-item")
        if card:
            # Parse grade_letter
            grade_letter = None
            assessment_cell = card.find(class_=re.compile(r"assessment-cell|task-score")) or card
            grade_el = assessment_cell.find(class_=re.compile(r"\bgrade\b"))
            if grade_el:
                grade_letter = grade_el.get_text(strip=True)
            
            if not grade_letter:
                not_assessed_els = assessment_cell.find_all(class_=re.compile(r"not-assessed"))
                for el in not_assessed_els:
                    txt = el.get_text(strip=True)
                    if txt:
                        grade_letter = txt
                        break

            if not grade_letter:
                not_applicable_el = assessment_cell.find(class_=re.compile(r"not-applicable"))
                if not_applicable_el:
                    grade_letter = not_applicable_el.get_text(strip=True)
                else:
                    na_el = assessment_cell.find(lambda tag: tag.name in {"div", "span"} and tag.get_text(strip=True) == "N/A")
                    if na_el:
                        grade_letter = "N/A"

            if not grade_letter:
                status_el = assessment_cell.find(class_=re.compile(r"\b(submitted|not-submitted)\b"))
                if status_el:
                    txt = status_el.get_text(strip=True)
                    if txt and txt.lower() in ("complete", "incomplete"):
                        grade_letter = txt
            detail["grade_letter"] = grade_letter

            # Parse points
            points_el = card.find("div", class_="points")
            if points_el:
                raw_pt = points_el.get_text(strip=True)
                if raw_pt and not any(k in raw_pt.lower() for k in ["submitted", "pending", "task", "due", "not"]):
                    detail["grade_score"] = raw_pt

            # Parse submit button.  Scoped to the page body: the document-wide
            # scan this replaces let site chrome and nav supply the match.
            dropbox_link = soup.find("a", href=re.compile(r"/core_tasks/\d+/dropbox"))
            has_submit_btn = bool(
                dropbox_link or main_content.find(lambda el: _is_submit_control(el))
            )
            detail["has_submit_button"] = has_submit_btn

            # Parse status.  Same two signals as the class-grades card, same
            # canonicalisation: the detail page renders the submitted state as a
            # badge with no `submitted` class, and the unsubmitted one as
            # `cell not-submitted` reading "Not Submitted".
            labels = []
            labels_set = card.find("div", class_="labels-set")
            if labels_set:
                for lbl in labels_set.find_all("div", class_="label"):
                    t = lbl.get_text(strip=True)
                    if t and t not in labels:
                        labels.append(t)
                for badge in labels_set.find_all("span", class_="badge-label"):
                    t = badge.get_text(strip=True)
                    if t and t not in labels:
                        labels.append(t)
            # Same split as the class-grades card: `status` keeps the frozen
            # spelling, `submission_status` carries the canonical token.
            detail["status"] = _card_status_text(card, labels)
            detail["submission_status"] = _card_submission_status(card, labels)
            detail["labels"] = labels

        if not from_hint:
            dropbox = main_content.find(class_=re.compile(r"dropbox|submission|coursework"))
            submission_text = self._text_from_block(dropbox)
            if submission_text:
                detail["submission"] = submission_text

            comments = []
            seen_comment_texts: set[str] = set()

            # Parse official teacher/assessment evaluation comments
            assessment_comments_div = main_content.find(class_="assessment-comments")
            if assessment_comments_div:
                for body in assessment_comments_div.find_all(
                    "div", class_=re.compile(r"fr-view|fix-body-margins", re.IGNORECASE)
                ):
                    text = self._text_from_block(body, limit=2000)
                    if text and text not in seen_comment_texts:
                        seen_comment_texts.add(text)
                        comments.append(text)

            for discussion in main_content.find_all(
                "div", class_=re.compile(r"\bdiscussion\b", re.IGNORECASE)
            )[:5]:
                body = discussion.find(
                    "div", class_=re.compile(r"fr-view|fix-body-margins", re.IGNORECASE)
                )
                if not body:
                    continue
                text = self._text_from_block(body, limit=2000)
                if text and text not in seen_comment_texts:
                    seen_comment_texts.add(text)
                    comments.append(text)
            if comments:
                detail["comments"] = comments

        # Description parsing.
        #
        # ManageBac renders the label as <div class="h4">Description</div> on
        # the task detail page: a *div* whose class is a heading name.  A
        # tag.name test never sees it, so the heading branch always missed and
        # the fallback below matched the class hero subtitle
        # (<div class="f-title__description f-hero__description">) instead —
        # `view` then reported the class's subject line as the task body, which
        # reads as plausible and so went unnoticed.
        def _is_description_label(tag) -> bool:
            if tag.get_text(" ", strip=True) != "Description":
                return False
            if tag.name in {"h3", "h4", "h5", "th"}:
                return True
            classes = tag.get("class") or []
            return tag.name == "div" and any(c in {"h3", "h4", "h5", "h6"} for c in classes)

        # bs4 invokes a `class_` callable once per *individual* class name, not
        # with the whole list, so these take a single name.
        def _is_class_hero_class(name) -> bool:
            return bool(name) and bool(
                re.search(r"f-hero__description|f-title__description", str(name), re.IGNORECASE)
            )

        desc = None
        if from_hint:
            desc = main_content.find(class_="fr-view") or main_content.find(class_="fix-body-margins")
        else:
            desc_heading = main_content.find(_is_description_label)
            if desc_heading:
                desc = desc_heading.find_next(
                    "div",
                    class_=re.compile(r"fr-view|fix-body-margins|show-more", re.IGNORECASE),
                )
            if not desc:
                # Fall back to a description-ish block, but never the class
                # hero: naming the class's subject is not describing the task.
                # Fail closed (no description) rather than return the wrong
                # text, because a wrong-but-plausible body is worse than none.
                desc = main_content.find(
                    class_=lambda c: bool(c)
                    and bool(re.search(r"description|task-body", str(c), re.IGNORECASE))
                    and not _is_class_hero_class(c)
                )

        # The class's own subject line, parsed independently so it can be
        # reported in its own place instead of standing in for the task body.
        class_description = self._text_from_block(
            main_content.find(class_=_is_class_hero_class), limit=300
        )
        if class_description:
            detail["class_description"] = class_description

        description_text = self._text_from_block(desc)
        if description_text:
            detail["description"] = description_text

        # Keep the markup too.  Flattening here would force every consumer to
        # re-derive structure that is right there in the page, so the raw HTML
        # goes alongside the plain text and the importer cleans it into MBEvent
        # itself.  ANSI is a terminal concern and is applied by the formatter.
        description_html = clean_redactor_html(desc)
        if description_html:
            detail["description_html"] = description_html

        attachments = self._extract_attachments(main_content)
        if attachments:
            detail["attachments"] = attachments
        return detail if detail else None

    def find_task_by_id(self, task_id: str, max_pages: int = 50) -> dict | None:
        """Search page-by-page across all views for a specific task ID, stopping as soon as found."""
        for view in ("overdue", "upcoming", "past"):
            for page in range(1, max_pages + 1):
                soup = self._get(f"/student/tasks_and_deadlines?view={view}&page={page}")
                tasks = self._parse_tasks_page(soup)
                if not tasks:
                    break
                for t in tasks:
                    t_id_match = re.search(r"(\d+)$", t.get("link", "").split("?")[0].rstrip("/"))
                    t_id = t_id_match.group(1) if t_id_match else None
                    if t_id == task_id or t.get("id") == task_id:
                        t["view"] = view
                        return t
                if not self._has_next_page(soup, page, view):
                    break
        return None

    def crawl_index(self) -> dict:
        """Lightweight check: upcoming page 1 + notifications hub.

        Returns a minimal dict suitable for daemon diffing.  Only two HTTP
        requests regardless of how many tasks exist.
        """
        upcoming = self.get_tasks_by_view("upcoming", 1)

        notifications: dict = {"unread_count": 0, "items": []}
        try:
            notifications = self._fetch_notifications()
        except Exception as exc:
            log.warning("notifications fetch failed: %s", exc)

        return {
            "student_name": self.student_name,
            "school": self.school,
            "base_url": self.base,
            "crawled_at": datetime.now().isoformat(),
            "upcoming": upcoming,
            "notifications": notifications,
        }

    def get_class_tasks(
        self,
        class_id: str,
        class_name: str | None = None,
        bypass_cache: bool = False,
    ) -> list[dict]:
        """Fetch and reconstruct all tasks for a specific class."""
        class_data = self.get_class_grades(class_id, bypass_cache=bypass_cache)
        tasks: list[dict] = []
        for t in class_data.get("tasks", []):
            task_id = t.get("task_id")
            if not task_id:
                continue

            labels = t.get("labels") or []
            grade_letter = t.get("grade_letter")
            due_date = t.get("due_date")
            has_submit_btn = bool(t.get("has_submit_button", False))
            is_submitted = is_task_submitted(t)
            # Exact string test on the frozen `status` spelling — this is the §6
            # gate, and it is deliberately the frozen version's test.  The grades
            # page writes "Not Submitted" (space, not hyphen), so this arm fires
            # only for the label-derived canonical token, never for the span text.
            # Canonising it (as this branch briefly did) makes it fire for every
            # card whose teacher closed the dropbox link, moving those from `past`
            # to `overdue`.  The trustworthy reading of that state now lives in
            # `submission_status`, which nothing here classifies on.

            if is_submitted:
                task_status = "submitted"
            elif has_submit_btn or t.get("status") == "not-submitted":
                task_status = "not-submitted"
            else:
                task_status = t.get("status")

            reconstructed_task = {
                "id": task_id,
                "title": t.get("title"),
                "class_name": class_name or "",
                "due_date": due_date,
                "link": t.get("url"),
                "grade_letter": grade_letter,
                "grade_score": t.get("points"),
                "labels": labels or None,
                "status": task_status,
                "has_submit_button": has_submit_btn,
            }

            view = classify_task_view(reconstructed_task)
            reconstructed_task["view"] = view
            tasks.append(reconstructed_task)
        return tasks

    def crawl_all(
        self,
        max_pages: int = 10,
        fetch_details: bool = False,
    ) -> dict:
        """Crawl all tasks by compiling from active classes core_tasks pages."""
        log.info("Discovering classes from dashboard...")
        classes = {}
        try:
            classes = self.get_classes()
        except Exception as e:
            log.warning("Failed to discover classes from dashboard: %s", e)

        upcoming = []
        past = []
        overdue = []

        if not classes:
            log.warning("No classes found. Falling back to paginated dashboard crawling...")
            upcoming = self.get_tasks_by_view("upcoming", max_pages)
            past = self.get_tasks_by_view("past", max_pages)
            overdue = self.get_tasks_by_view("overdue", max_pages)
        else:
            log.info("Fetching tasks from %d classes...", len(classes))
            for class_id, class_name in classes.items():
                try:
                    tasks = self.get_class_tasks(class_id, class_name=class_name, bypass_cache=False)
                    for reconstructed_task in tasks:
                        view = reconstructed_task.get("view")
                        if view == "upcoming":
                            upcoming.append(reconstructed_task)
                        elif view == "overdue":
                            overdue.append(reconstructed_task)
                        else:
                            past.append(reconstructed_task)
                except Exception as e:
                    log.warning("Failed to crawl tasks for class %s: %s", class_name, e)

        # Retrieve notifications
        notifications: dict = {"unread_count": 0, "items": []}
        try:
            notifications = self._fetch_notifications()
        except Exception as exc:
            log.warning("notifications fetch failed: %s", exc)

        if fetch_details:
            items = [t for t in upcoming + past + overdue if t.get("link")]
            log.info("Fetching details for %d tasks via /hint...", len(items))
            for i, task in enumerate(items):
                detail = self.get_task_detail(task["link"], from_hint=True)
                if detail:
                    task["detail"] = detail
                if (i + 1) % 5 == 0:
                    log.info("  detail %d/%d", i + 1, len(items))

        return {
            "student_name": self.student_name,
            "school": self.school,
            "base_url": self.base,
            "crawled_at": datetime.now().isoformat(),
            "upcoming": upcoming,
            "past": past,
            "overdue": overdue,
            "notifications": notifications,
            "summary": {
                "upcoming_count": len(upcoming),
                "past_count": len(past),
                "overdue_count": len(overdue),
            },
        }
