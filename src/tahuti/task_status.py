"""Unified task status, submission, grading, and presentation domain model."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from enum import Enum
import re
from typing import Any, Iterator

_UNSUBMITTED_BASELINE: ContextVar[float | None] = ContextVar(
    "tahuti_unsubmitted_baseline", default=None
)

_FRACTION_SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
_PERCENT_SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def get_unsubmitted_baseline() -> float | None:
    """Return the active percentage baseline (0-100) for treating scores as unsubmitted."""
    return _UNSUBMITTED_BASELINE.get()


@contextmanager
def unsubmitted_baseline_context(baseline: float | None) -> Iterator[None]:
    """Temporarily set the unsubmitted percentage baseline for the current context."""
    token = _UNSUBMITTED_BASELINE.set(baseline)
    try:
        yield
    finally:
        _UNSUBMITTED_BASELINE.reset(token)


def extract_score_percentage(task: dict[str, Any]) -> float | None:
    """Parse a task's grade_score into a 0-100 percentage, or None if unavailable."""
    detail = task.get("detail") or task.get("enriched_task") or {}
    raw_score = str(task.get("grade_score") or detail.get("grade_score") or "").strip()
    if not raw_score or raw_score == "-":
        return None
    m = _FRACTION_SCORE_RE.search(raw_score)
    if m:
        num = float(m.group(1))
        denom = float(m.group(2))
        if denom > 0:
            return (num / denom) * 100.0
        return None
    m = _PERCENT_SCORE_RE.search(raw_score)
    if m:
        return float(m.group(1))
    return None


def is_below_unsubmitted_baseline(
    task: dict[str, Any], baseline: float | None = None
) -> bool:
    """Return True if *task* has a numeric score at or below the configured baseline."""
    effective = baseline if baseline is not None else get_unsubmitted_baseline()
    if effective is None:
        return False
    pct = extract_score_percentage(task)
    if pct is None:
        return False
    return pct <= effective


class SubmissionStatus(str, Enum):
    SUBMITTED = "submitted"
    PENDING = "pending"
    NONE = "none"


class GradeStatus(str, Enum):
    GRADED = "graded"
    NOT_ASSESSED = "not_assessed"
    NOT_APPLICABLE = "not_applicable"


class LifecycleStatus(str, Enum):
    TODO = "todo"
    COMPLETED = "completed"


def is_submitted_badge(badge: str) -> bool:
    """Return True if badge indicates submission without negative qualifiers ('not', 'un')."""
    b = str(badge or "").strip().lower()
    return "submitted" in b and "not" not in b and "un" not in b


# The only two submission states anything downstream may compare against.  Every
# producer stores one of these and every consumer tests against them, so no
# caller is one capital letter — or one space-versus-hyphen — away from a wrong
# answer.
SUBMISSION_SUBMITTED = "submitted"
SUBMISSION_NOT_SUBMITTED = "not-submitted"

# ManageBac's wordings for "this has been handed in".
_SUBMIT_WORDS = frozenset(
    {"submit", "submits", "submitted", "submitting", "submission", "submissions"}
)

# Wordings that say the outstanding state, either on their own ("Pending",
# "Waiting") or as a qualifier on a submit word ("Not Submitted", "No
# submission", "Awaiting submission").
_OUTSTANDING_WORDS = frozenset(
    {"unsubmitted", "unsubmission", "unsubmissions", "pending", "waiting", "awaiting", "awaited", "outstanding"}
)
_NEGATION_WORDS = frozenset({"not", "no", "never", "none", "non"})

# Words that can pad a state phrase without changing it ("Not submitted yet").
_FILLER_WORDS = frozenset({"yet", "still", "is", "are", "was", "were", "been", "be"})

# A submission state is spelled *only* out of these words.  The whitelist is what
# keeps an action ("Submit Coursework", "Upload submission") from being read as a
# state — that mistake reported a pending task with an open dropbox link as
# already submitted.
_STATE_WORDS = _SUBMIT_WORDS | _OUTSTANDING_WORDS | _NEGATION_WORDS | _FILLER_WORDS


def normalize_submission_status(raw: Any) -> str | None:
    """Canonicalise ManageBac's submission wording to a canonical token.

    The same two states arrive spelled half a dozen ways: "Submitted",
    "not-submitted", "Not Submitted" (space, not hyphen — that is literally what
    the ``cell not-submitted`` span contains), "Pending", "Waiting".  Storing
    that text verbatim is what made the ``status == "not-submitted"`` test
    downstream permanently dead, so the parse layer canonicalises here and the
    classifier below independently tolerates the raw spellings: neither layer
    depends on the other.

    Returns :data:`SUBMISSION_SUBMITTED` / :data:`SUBMISSION_NOT_SUBMITTED`, or
    ``None`` when *raw* is not a submission state at all — an action label, a
    category name, "Not Assessed Yet".  ``None`` is a real answer and must never
    be coerced into a state, which is how unlabelled tasks came to be reported as
    unsubmitted (and, with no dropbox link to rescue them, as "Complete").
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    # "Not Submitted", "not-submitted", "not_submitted" and "not  submitted"
    # all collapse to the same word list.
    words = [w for w in re.split(r"[^a-z]+", text) if w]
    if not words or any(w not in _STATE_WORDS for w in words):
        return None

    if any(w in _SUBMIT_WORDS for w in words):
        # A qualifier decides: "Not Submitted", "No submission" and "Submission
        # pending" are all the outstanding state, not the submitted one.
        if any(w in _OUTSTANDING_WORDS or w in _NEGATION_WORDS for w in words):
            return SUBMISSION_NOT_SUBMITTED
        return SUBMISSION_SUBMITTED

    if any(w in _OUTSTANDING_WORDS for w in words):
        return SUBMISSION_NOT_SUBMITTED
    return None


def submission_status_from_labels(labels: Any) -> str | None:
    """Canonicalise the submission wording carried by a list of labels.

    Badge and category labels are the only signal some pages give, so "Submitted"
    among the labels means submitted.  The outstanding state is only inferred when
    a label says so outright — silence stays ``None`` rather than becoming
    "unsubmitted".
    """
    tokens = [normalize_submission_status(label) for label in (labels or [])]
    if SUBMISSION_SUBMITTED in tokens:
        return SUBMISSION_SUBMITTED
    if SUBMISSION_NOT_SUBMITTED in tokens:
        return SUBMISSION_NOT_SUBMITTED
    return None


def get_submission_status(task: dict[str, Any]) -> SubmissionStatus:
    """Evaluate canonical submission status of a task.

    **This comparison is deliberately exact-string and must stay that way.**

    ``status`` holds ManageBac's *own spelling* — the class-grades card's
    ``cell not-submitted`` span reads ``"Not Submitted"``, capital N, capital S,
    a space where the token has a hyphen.  Lowercasing it yields
    ``"not submitted"``, which does not equal ``"not-submitted"``, so on the
    class path the token arm below never fires and PENDING is reached through
    ``has_submit_btn`` alone.

    That is load-bearing, not accidental.  The owner ruled on 2026-09-19 that
    classification output is frozen: the exact behaviour here is the version
    that ran a week of live pressure testing through the daemon and webhook.
    Normalising ``status`` through :func:`normalize_submission_status` makes the
    token arm fire for every card whose teacher closed the dropbox link, moving
    those tasks from ``past`` to ``overdue`` — the §6 divergence.  Verified
    live: zero of the 45 class-grades cards are affected today, so it is latent
    rather than wrong, but latent is not the same as absent.

    The corrected parse is still available and still trustworthy: producers
    store the canonical token in ``submission_status`` alongside this field, so
    an approved rule can be built on real signals without this function moving.
    """
    status = str(task.get("status") or "").strip().lower()
    if status == "submitted":
        return SubmissionStatus.SUBMITTED

    labels = (task.get("labels") or [])
    detail = task.get("detail") or {}
    detail_labels = (detail.get("labels") or [])
    all_labels = labels + detail_labels
    if any(is_submitted_badge(l) for l in all_labels):
        return SubmissionStatus.SUBMITTED

    if str(detail.get("status") or "").strip().lower() == "submitted":
        return SubmissionStatus.SUBMITTED
    if detail.get("submission") or detail.get("submissions"):
        return SubmissionStatus.SUBMITTED

    grade_score = str(task.get("grade_score") or "").strip().lower()
    grade_letter = str(task.get("grade_letter") or "").strip().lower()
    if is_submitted_badge(grade_score) or is_submitted_badge(grade_letter):
        return SubmissionStatus.SUBMITTED

    # Check pending submission entrance
    has_submit_btn = bool(
        task.get("has_submit_button", False)
        or detail.get("has_submit_button", False)
    )
    if status == "not-submitted" or has_submit_btn:
        return SubmissionStatus.PENDING

    return SubmissionStatus.NONE


def is_task_submitted(task: dict[str, Any]) -> bool:
    """Return True if the task has been submitted."""
    return get_submission_status(task) == SubmissionStatus.SUBMITTED


def get_grade_status(task: dict[str, Any]) -> GradeStatus:
    """Evaluate canonical grading status of a task."""
    detail = task.get("detail") or task.get("enriched_task") or {}
    status = str(task.get("status") or detail.get("status") or "").strip().lower()
    if status == "graded":
        return GradeStatus.GRADED

    letter = str(task.get("grade_letter") or detail.get("grade_letter") or "").strip()
    score = str(task.get("grade_score") or detail.get("grade_score") or "").strip()

    if letter == "-":
        letter = ""
    if score == "-":
        score = ""

    # Sanitize: non-evaluative lifecycle strings leaking into score/letter must never count as grades
    non_grade_terms = ("submitted", "pending", "not-submitted", "not submitted", "not assessed yet", "not assessed", "ungraded")
    if score.lower() in non_grade_terms:
        score = ""
    if letter.lower() in non_grade_terms:
        letter = ""

    labels = (task.get("labels") or []) + (detail.get("labels") or [])
    labels_lower = [str(l).lower() for l in labels]

    # Explicit N/A or Exempt
    if letter.lower() in ("n/a", "not applicable", "exempt", "excused") or any(
        l in ("exempt", "excused") for l in labels_lower
    ):
        return GradeStatus.NOT_APPLICABLE

    # Explicit Not Assessed Yet
    if "not assessed yet" in letter.lower() or "not assessed yet" in labels_lower:
        if not (score and not re.match(r"^\s*0\s*/", score)):
            return GradeStatus.NOT_ASSESSED

    # 0 score placeholder
    if score and re.match(r"^\s*0\s*/", score) and not letter:
        return GradeStatus.NOT_ASSESSED

    # Valid evaluated grade or score
    has_score = bool(score)
    has_letter = bool(letter and letter.lower() not in ("not assessed", "not assessed yet"))
    if has_score or has_letter or any(l == "graded" for l in labels_lower):
        return GradeStatus.GRADED

    return GradeStatus.NOT_ASSESSED


def is_task_graded(task: dict[str, Any]) -> bool:
    """Return True if the task has been evaluated with a grade or marked not applicable."""
    return get_grade_status(task) in (GradeStatus.GRADED, GradeStatus.NOT_APPLICABLE)


def is_task_submitted_or_graded(task: dict[str, Any]) -> bool:
    """Return True if the task is submitted, graded, or marked exempt."""
    return is_task_submitted(task) or is_task_graded(task)


def is_task_todo(
    task: dict[str, Any], unsubmitted_baseline: float | None = None
) -> bool:
    """Return True if task is uncompleted (unsubmitted and ungraded, or below baseline)."""
    if is_below_unsubmitted_baseline(task, unsubmitted_baseline):
        return True
    if is_task_submitted(task):
        return False
    grade_status = get_grade_status(task)
    if grade_status in (GradeStatus.GRADED, GradeStatus.NOT_APPLICABLE):
        return False

    sub_status = get_submission_status(task)
    return sub_status == SubmissionStatus.PENDING


def is_task_completed(task: dict[str, Any]) -> bool:
    """Return True if a task is completed (submitted or evaluated with a grade)."""
    return not is_task_todo(task)


def align_timezones(
    due_dt: datetime, now: datetime
) -> tuple[datetime, datetime]:
    """Return *(due_dt, now)* made safe to compare with each other.

    ``client.parse_due_date`` can return either an aware datetime (ISO input
    carrying an offset, including a ``…Z`` suffix) or a naive one (every
    textual ManageBac format).  Comparing the two raises ``TypeError: can't
    compare offset-naive and offset-aware datetimes``, so every caller that
    orders or compares a parsed due date against "now" funnels through here.

    Aware values are converted to local wall-clock time and then stripped of
    their ``tzinfo``; naive values are returned untouched.  Inputs are never
    mutated.
    """
    if due_dt.tzinfo is not None:
        due_dt = due_dt.astimezone().replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    return due_dt, now


def as_naive(dt: datetime) -> datetime:
    """Return *dt* as a naive local datetime (no-op when already naive)."""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _is_task_past_due(
    task: dict[str, Any], now_ref: datetime | None = None
) -> bool:
    """Return True if *task* has a parseable due_date strictly before *now_ref*."""
    from .client import parse_due_date

    now = as_naive(now_ref or datetime.now())
    due_date = task.get("due_date")
    due_dt = parse_due_date(due_date, now_ref=now) if due_date else None
    if not due_dt:
        return False
    due_dt, now = align_timezones(due_dt, now)
    return due_dt < now


def classify_task_view(
    task: dict[str, Any],
    now_ref: datetime | None = None,
    unsubmitted_baseline: float | None = None,
) -> str:
    """Classify a task into canonical timeline views: 'upcoming', 'overdue', 'past'."""
    from .client import parse_due_date

    # Compare in naive local time throughout.  parse_due_date does its own
    # year-wrapping arithmetic against now_ref, and an aware now_ref makes that
    # subtraction raise (silently swallowed there, returning None); handing it
    # a naive reference keeps that path working whichever way the parser comes
    # back aware or naive.
    now = as_naive(now_ref or datetime.now())
    due_date = task.get("due_date")
    if not due_date:
        return "past"

    due_dt = parse_due_date(due_date, now_ref=now)
    if not due_dt:
        return "past"

    due_dt, now = align_timezones(due_dt, now)

    if due_dt >= now:
        return "upcoming"

    # Due date is in the past: if task is incomplete/todo, it is overdue
    if is_task_todo(task, unsubmitted_baseline=unsubmitted_baseline):
        return "overdue"
    return "past"


def format_grade_display(
    task: dict[str, Any],
    standalone: bool = False,
    now_ref: datetime | None = None,
    unsubmitted_baseline: float | None = None,
) -> str:
    """Format single unified grade display string.

    If standalone is True (e.g. tahuti view), returns only the grade value
    ('A', 'A (100 / 100 pts)', 'N/A', or 'None').
    """
    detail = task.get("detail") or {}
    letter = str(task.get("grade_letter") or detail.get("grade_letter") or "").strip()
    score = str(task.get("grade_score") or detail.get("grade_score") or "").strip()
    if letter == "-":
        letter = ""
    if score == "-":
        score = ""

    non_grade_terms = ("submitted", "pending", "not-submitted", "not submitted", "not assessed yet", "not assessed", "ungraded")
    if letter.lower() in non_grade_terms:
        letter = ""
    if score.lower() in non_grade_terms:
        score = ""

    grade_status = get_grade_status(task)
    if grade_status == GradeStatus.GRADED:
        if score and letter and letter.lower() not in ("not assessed yet", "not assessed", "n/a"):
            grade_str = f"{letter} ({score})" if standalone else f"{letter} ( {score} )"
        elif score:
            grade_str = score
        elif letter:
            grade_str = letter
        else:
            grade_str = ""

        if grade_str:
            if is_below_unsubmitted_baseline(task, unsubmitted_baseline):
                prefix = "⚠ Unsubmitted" if _is_task_past_due(task, now_ref) else "Unsubmitted"
                return f"{prefix} · {grade_str}"
            return grade_str

    if grade_status == GradeStatus.NOT_APPLICABLE:
        return "N/A"

    if standalone:
        return "None"

    # Not graded yet
    sub_status = get_submission_status(task)
    if sub_status != SubmissionStatus.PENDING:
        return "Ungraded"

    # Unsubmitted / pending
    return "⚠ Unsubmitted" if _is_task_past_due(task, now_ref) else "Unsubmitted"


def get_task_display_status(task: dict[str, Any]) -> str:
    """Format single unified status string (matching tahuti view status)."""
    if is_task_todo(task):
        return "Incomplete (Todo)"
    if is_task_submitted(task):
        return "Complete (Submitted)"
    if get_grade_status(task) == GradeStatus.GRADED:
        return "Complete (Graded)"
    return "Complete"


# Alias for backward compatibility
get_task_display_grade = format_grade_display
