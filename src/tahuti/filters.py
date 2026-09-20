"""Task filtering and view helpers."""

from __future__ import annotations

import re

from .exceptions import CommandError


# ── Timeline views ─────────────────────────────────────────────────────
# One canonical vocabulary shared by the CLI (`--view`) and the MCP
# `list_tasks` tool.  Anything that is not an explicit alias is an error:
# silently falling back to "all" makes a mistyped view look like real data.
CANONICAL_VIEWS: tuple[str, ...] = ("all", "upcoming", "past", "overdue")

VIEW_ALIASES: dict[str, str] = {
    "all": "all",
    "all tasks": "all",
    "any": "all",
    "everything": "all",
    "*": "all",
    "upcoming": "upcoming",
    "upcoming task": "upcoming",
    "upcoming tasks": "upcoming",
    "future": "upcoming",
    "next": "upcoming",
    "past": "past",
    "past task": "past",
    "past tasks": "past",
    "previous": "past",
    "overdue": "overdue",
    "overdue task": "overdue",
    "overdue tasks": "overdue",
    "late": "overdue",
    "missed": "overdue",
}

# Empty/unspecified means "no preference", which resolves to *default*.
_VIEW_DEFAULT_TOKENS = frozenset({"", "default", "none"})


class InvalidViewError(CommandError):
    """Raised when a requested timeline view is not recognised.

    Subclasses :class:`CommandError` so an uncaught instance still surfaces as
    a structured CLI error payload (``ERROR [invalid_view]: ...``) instead of a
    traceback, and so MCP tools can map it straight to a JSON error.
    """

    def __init__(self, requested_view: object):
        shown = str(requested_view)[:80]
        super().__init__(
            "invalid_view",
            f"Unknown view: {shown!r}. "
            f"Use one of {', '.join(CANONICAL_VIEWS)}.",
        )


def normalize_view(requested_view: object, default: str = "all") -> str:
    """Return the canonical name for *requested_view*.

    Matching is case-insensitive and tolerates surrounding whitespace and the
    natural singular/plural forms listed in :data:`VIEW_ALIASES`.  A missing or
    empty value resolves to *default*.

    Raises:
        InvalidViewError: if the value is not a recognised view or alias.
    """
    token = re.sub(r"\s+", " ", str(requested_view or "").strip()).casefold()
    if token in _VIEW_DEFAULT_TOKENS:
        return default
    canonical = VIEW_ALIASES.get(token)
    if canonical:
        return canonical
    # Tolerate a plural/singular swap on any listed alias ("upcomings").
    if token.endswith("s") and token[:-1] in VIEW_ALIASES:
        return VIEW_ALIASES[token[:-1]]
    raise InvalidViewError(requested_view)


def matches_subject(task: dict, subject: str) -> bool:
    """Return *True* if *task*'s class name contains *subject* (case-insensitive)."""
    class_name = task.get("class_name")
    if not class_name:
        return False
    return subject.casefold() in class_name.casefold()


def filter_result_by_subject(result: dict, subject: str) -> dict:
    """Filter a crawl result dict in-place by subject and update summary counts."""
    result["upcoming"] = [t for t in result["upcoming"] if matches_subject(t, subject)]
    result["past"] = [t for t in result["past"] if matches_subject(t, subject)]
    result["overdue"] = [t for t in result["overdue"] if matches_subject(t, subject)]
    _update_summary_counts(result)
    result["subject_filter"] = subject
    return result


from .task_status import (
    GradeStatus,
    classify_task_view,
    get_grade_status,
    is_submitted_badge,
    is_task_completed,
    is_task_submitted,
    is_task_todo,
)

# Alias for backward compatibility
is_task_unfinished = is_task_todo


def matches_graded(task: dict, graded: bool) -> bool:
    """Return *True* if the task's graded state matches the *graded* query."""
    is_graded = get_grade_status(task) == GradeStatus.GRADED
    return is_graded == graded


def matches_submitted(task: dict, submitted: bool) -> bool:
    """Return *True* if the task's submission state matches the *submitted* query."""
    return is_task_submitted(task) == submitted


# A grade code is a letter A-F with an optional +/- modifier.  The trailing
# lookahead keeps the match anchored to the whole token, so "A+ (95/100)"
# yields "A+" while an ordinary word ("Absent", "Formative") does not yield
# "A"/"F".  Note ``\\b`` cannot be used here: there is no word boundary between
# "+"/"-" and a following space, which made the modifier group backtrack to
# empty and silently degraded every "A+"/"B-" to "A"/"B".
_GRADE_CODE_RE = re.compile(r"^([A-F][+-]?)(?![A-Za-z])")


def matches_grade_query(task: dict, query: str) -> bool:
    """Return *True* if the task's grade matches the *query*.

    Supports:
      - Letters (e.g. "B" matches "B", "B+", "B-", whereas "B-" matches only "B-")
      - GPA to letter mappings (e.g. "4.0" -> "A", "A+", "3.7" -> "A-", etc.)
    """
    gl = task.get("grade_letter") or ""
    gs = task.get("grade_score") or ""

    # Try to find a grade code from letter or score (e.g. "A+", "B-", "A")
    grade_val = str(gl).strip().upper()
    if not grade_val:
        # Some lists carry only a score string like "A+ (95/100)".
        match = _GRADE_CODE_RE.match(str(gs).strip().upper())
        if match:
            grade_val = match.group(1)

    if not grade_val:
        return False

    q = query.strip().upper()

    # Mappings from GPA to letter grades
    gpa_mapping = {
        "4.0": ["A", "A+"],
        "3.7": ["A-"],
        "3.3": ["B+"],
        "3.0": ["B"],
        "2.7": ["B-"],
        "2.3": ["C+"],
        "2.0": ["C"],
        "1.7": ["C-"],
        "1.3": ["D+"],
        "1.0": ["D"],
        "0.0": ["F"],
    }
    if q in gpa_mapping:
        return grade_val in gpa_mapping[q]

    # Letter matching logic:
    # If query is a single letter (A, B, C, D, F), match any modifier (+, -)
    if len(q) == 1 and q.isalpha():
        return grade_val.startswith(q)

    # Otherwise exact match (e.g. "B-" matches only "B-")
    return grade_val == q


def summary_of(views: dict) -> dict:
    """Build the per-view task count summary from a view mapping.

    ``views`` needs ``"upcoming"``, ``"past"`` and ``"overdue"`` lists; anything
    else in it is ignored, so a full crawl result works as well as the three
    slices ``result_views`` returns.
    """
    return {
        "upcoming_count": len(views["upcoming"]),
        "past_count": len(views["past"]),
        "overdue_count": len(views["overdue"]),
        "total_count": len(views["upcoming"])
        + len(views["past"])
        + len(views["overdue"]),
    }


def _update_summary_counts(result: dict) -> None:
    """Recalculate summary counts in-place for a result dict."""
    result["summary"] = summary_of(result)


def matches_tag(task: dict, tag_query: str) -> bool:
    """Return *True* if the task's labels match the logical tag query (case-insensitive).

    Supports:
      - OR operators: 'tagA,tagB', 'tagA|tagB', 'tagA or tagB'
      - AND operators: 'tagA+tagB', 'tagA&tagB', 'tagA and tagB'
    """
    labels = task.get("labels") or []
    if not labels:
        return False
    
    label_set = {lbl.casefold() for lbl in labels}
    query = tag_query.casefold()

    # Check for OR operators first
    or_splitters = [",", "|", " or "]
    for splitter in or_splitters:
        if splitter in query:
            parts = [p.strip() for p in query.split(splitter) if p.strip()]
            return any(
                any(part in lbl for lbl in label_set)
                for part in parts
            )

    # Check for AND operators
    and_splitters = ["+", "&", " and "]
    for splitter in and_splitters:
        if splitter in query:
            parts = [p.strip() for p in query.split(splitter) if p.strip()]
            return all(
                any(part in lbl for lbl in label_set)
                for part in parts
            )

    # Single tag fallback
    return any(query in lbl for lbl in label_set)


def matches_completed(task: dict, completed: bool) -> bool:
    """Return *True* if the task's completion state matches the *completed* query."""
    return is_task_completed(task) == completed


def filter_result_by_status(
    result: dict,
    graded: bool | None = None,
    submitted: bool | None = None,
    grade: str | None = None,
    tag: str | None = None,
    completed: bool | None = None,
) -> dict:
    """Filter a crawl result dict in-place by status/grade/tag/completed attributes and update counts."""
    for section in ("upcoming", "past", "overdue"):
        tasks = result.get(section, [])
        if graded is not None:
            tasks = [t for t in tasks if matches_graded(t, graded)]
        if submitted is not None:
            tasks = [t for t in tasks if matches_submitted(t, submitted)]
        if grade is not None:
            tasks = [t for t in tasks if matches_grade_query(t, grade)]
        if tag is not None:
            tasks = [t for t in tasks if matches_tag(t, tag)]
        if completed is not None:
            tasks = [t for t in tasks if matches_completed(t, completed)]
        result[section] = tasks

    _update_summary_counts(result)
    return result



def result_views(result: dict, requested_view: str) -> dict:
    """Return only the requested view section from a crawl result.

    Uses the same validated vocabulary as the MCP ``list_tasks`` tool: an
    unrecognised *requested_view* raises :class:`InvalidViewError` rather than
    quietly degrading to "all".  A name that is already canonical is taken as
    given — the MCP tool normalises it up front so it can fail before it crawls,
    and re-running the alias table on the answer is repetition, not validation.

    Raises:
        InvalidViewError: if *requested_view* is not a recognised view.
    """
    view = (
        requested_view
        if requested_view in CANONICAL_VIEWS
        else normalize_view(requested_view)
    )
    return {
        "upcoming": result["upcoming"] if view in ("all", "upcoming") else [],
        "past": result["past"] if view in ("all", "past") else [],
        "overdue": result["overdue"] if view in ("all", "overdue") else [],
    }


def find_task_by_id(result: dict, task_id: str) -> dict | None:
    """Find a task by its ID across all views."""
    for task in result["upcoming"] + result["past"] + result["overdue"]:
        if task.get("id") == task_id:
            return task
    return None
