"""CLI output formatting and payload construction."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unicodedata
from textwrap import indent

from .richtext import color_depth, html_to_ansi
from .task_status import (
    as_naive,
    get_task_display_grade,
    get_task_display_status,
    is_task_completed,
)

# Scripts that always want one shape can pin it here, so they never depend on
# whether stdout happens to be a terminal (cron, CI, ``tee``, a pager).
# The new spelling wins when both are set, so an already-migrated script is
# never overridden by a stale old one — the same precedence rule as
# :func:`tahuti.config.env_value`, which this mirrors rather than imports
# because it has no need of that module's other concerns.
FORMAT_ENV = "TAHUTI_FORMAT"
FORMAT_ENV_LEGACY = "MB_CLI_FORMAT"
_FORMAT_ENV_VALUES = ("json", "pretty")


def get_display_width(s: str) -> int:
    """Return the terminal display width of a string, accounting for wide characters."""
    width = 0
    for char in s:
        if unicodedata.east_asian_width(char) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def pad_string(s: str, width: int, align: str = "left") -> str:
    """Pad a string to a specific terminal display width."""
    s_width = get_display_width(s)
    pad_len = max(0, width - s_width)
    if align == "right":
        return " " * pad_len + s
    else:
        return s + " " * pad_len


_PRETTY_URL_LIMIT = 400


def display_url(url, limit: int = _PRETTY_URL_LIMIT) -> str:
    """A URL shortened for the terminal, with everything dropped declared."""
    text = str(url or "")
    if not text:
        return text
    head, sep, query = text.partition("?")
    notes: list[str] = []
    if sep:
        notes.append(f"query string omitted ({len(query)} chars)")
    if len(head) > limit:
        head = head[:limit] + "…"
        notes.append(f"{len(text)} chars total")
    if not notes:
        return text
    marker = "?…" if sep else ""
    return f"{head}{marker} [{'; '.join(notes)}; full URL in --format json]"


def resolve_format(requested_format: str | None) -> str:
    """Return the output format to use.

    Precedence, highest first:

    1. An explicit ``--format`` (``requested_format``).
    2. ``$TAHUTI_FORMAT`` (or the deprecated ``$MB_CLI_FORMAT``) set to
       ``json`` or ``pretty``.
    3. The documented default: ``pretty`` on an interactive terminal, ``json``
       when stdout is not a TTY.

    Rule 3 is what makes ``mb list | jq .`` work without every caller having to
    remember ``--format json``; see the "Output Formatting" section of the
    README.
    """
    if requested_format:
        return str(requested_format)

    override = (os.environ.get(FORMAT_ENV) or os.environ.get(FORMAT_ENV_LEGACY) or "").strip().lower()
    if override in _FORMAT_ENV_VALUES:
        return override

    try:
        is_tty = bool(sys.stdout.isatty())
    except Exception:
        # A detached/closed stdout (or a test double without isatty) must not
        # crash the command; treating it as non-interactive is the safe answer.
        is_tty = False
    return "pretty" if is_tty else "json"


def render_pretty(payload: dict) -> str:
    if not payload.get("ok"):
        # Not `error`: that is this module's payload-building helper, and a
        # local of the same name would shadow it for the rest of the function.
        err = payload.get("error", {})
        return (
            f"ERROR [{err.get('code', 'unknown')}]: "
            f"{err.get('message', 'Unknown error')}"
        )

    command = payload.get("command", "unknown")
    profile = payload.get("profile", "default")
    data = payload.get("data", {})

    if command == "login":
        return (
            "Login successful\n"
            f"  profile: {profile}\n"
            f"  school: {data.get('school')}\n"
            f"  domain: {data.get('domain')}\n"
            f"  email: {data.get('email')}\n"
            f"  base_url: {data.get('base_url')}\n"
            f"  auth_method: {data.get('auth_method')}"
        )

    if command == "logout":
        return (
            "Logout complete\n"
            f"  profile: {profile}\n"
            f"  all_profiles: {data.get('all_profiles')}"
        )

    if command == "list":
        meta = data.get("meta", {})
        summary = data.get("summary", {})
        tasks = data.get("tasks", {})
        lines = [
            "Task list",
            f"  profile: {profile}",
            f"  student: {meta.get('student_name')}",
            f"  school: {meta.get('school')}",
            f"  view: {meta.get('view')}",
            f"  subject_filter: {meta.get('subject_filter') or '-'}",
            f"  details: {meta.get('details')}",
            f"  upcoming: {summary.get('upcoming_count', 0)}",
            f"  past: {summary.get('past_count', 0)}",
            f"  overdue: {summary.get('overdue_count', 0)}",
            f"  total: {summary.get('total_count', 0)}",
        ]

        # Gather all tasks to compute maximum width of columns
        all_tasks = []
        for s in ("upcoming", "past", "overdue"):
            all_tasks.extend(tasks.get(s, []) or [])

        if not all_tasks:
            lines.append("  (no tasks)")
            return "\n".join(lines)

        from tahuti.client import parse_due_date
        from datetime import datetime

        def task_sort_key(t) -> datetime:
            # parse_due_date hands back an *aware* datetime for ISO input with
            # an offset and a *naive* one for every textual format, so the keys
            # are normalised to naive local time before sorting: mixing the two
            # in one section raises TypeError out of this renderer, which the
            # CLI does not catch.  Undated tasks sort last.
            dt = parse_due_date(t.get("due_date"))
            if dt is None:
                return datetime.max
            return as_naive(dt)

        get_grade_display = get_task_display_grade

        for section in ("upcoming", "past", "overdue"):
            section_tasks = tasks.get(section, [])
            if not section_tasks:
                continue

            lines.append(f"\n[{section}]")

            from collections import defaultdict
            class_groups = defaultdict(list)
            for t in section_tasks:
                class_groups[t.get("class_name") or "Unknown Class"].append(t)

            sorted_classes = sorted(class_groups.keys())
            class_displays = []
            is_single_class = len(sorted_classes) == 1

            for class_name in sorted_classes:
                class_tasks = sorted(class_groups[class_name], key=task_sort_key)
                id_w = max((get_display_width(str(t.get("id") or "")) for t in class_tasks), default=0)
                title_w = max((get_display_width(str(t.get("title") or "")) for t in class_tasks), default=0)
                due_w = max((get_display_width(str(t.get("due_date") or "")) for t in class_tasks), default=0)
                grade_w = max((get_display_width(get_grade_display(t)) for t in class_tasks), default=0)

                class_lines = []
                overall_suffix = ""
                class_overall = None
                for t in class_tasks:
                    if t.get("class_overall"):
                        class_overall = t.get("class_overall")
                        break
                if class_overall:
                    mark = class_overall.get("mark")
                    score = class_overall.get("score")
                    mark_part = mark if (mark and mark != "-") else ""
                    score_part = f"{score:.2f}%" if score is not None else ""
                    if mark_part and score_part:
                        overall_suffix = f" [{mark_part} ({score_part})]"
                    elif mark_part:
                        overall_suffix = f" [{mark_part}]"
                    elif score_part:
                        overall_suffix = f" [{score_part}]"

                if is_single_class:
                    class_lines.append(f"\n{class_name}{overall_suffix}")
                else:
                    class_lines.append(f"\n=== {class_name}{overall_suffix} ===")

                for task in class_tasks:
                    grade = get_grade_display(task)
                    col_id = pad_string(str(task.get("id") or ""), id_w, "right")
                    col_title = pad_string(str(task.get("title") or ""), title_w, "left")
                    col_due = pad_string(str(task.get("due_date") or ""), due_w, "left")
                    col_grade = pad_string(grade, grade_w, "left")
                    class_lines.append(
                        f"- {col_id} | {col_title} | {col_due} | {col_grade}"
                    )
                class_displays.append("\n".join(class_lines))

            separator = "\n---"
            lines.append(separator.join(class_displays))

        return "\n".join(lines)

    if command == "view":
        task = data.get("task", {}) or {}
        detail = data.get("detail", {}) or {}

        task_data = {**task}
        if detail:
            task_data["detail"] = {**(task.get("detail") or {}), **detail}

        # Format Grade & Status Display via unified domain model
        grade_display = get_task_display_grade(task_data, standalone=True)
        status_display = get_task_display_status(task_data)
        has_submit_btn = bool(
            task_data.get("has_submit_button")
            or (task_data.get("detail") or {}).get("has_submit_button")
        )

        lines = [
            "Task detail",
            f"  profile: {profile}",
            f"  id: {task.get('id')}",
            f"  title: {task.get('title')}",
            f"  class: {task.get('class_name')}",
        ]
        # The class's subject line belongs next to the class it describes, not
        # in the task's own body — they are different things and ManageBac
        # renders both on the same page.
        if detail.get("class_description"):
            lines.append(f"  class description: {detail['class_description']}")
        lines.extend([
            f"  due: {task.get('due_date')}",
            f"  grade: {grade_display}",
            f"  status: {status_display}",
            f"  submit button: {'Yes' if has_submit_btn else 'No'}",
            f"  link: {task.get('link')}",
        ])
        if detail.get("description") or detail.get("description_html"):
            lines.append("\n[description]")
            # Colour is decided here, at the presentation layer, never in the
            # client: the JSON payload must stay escape-free, and what the
            # terminal can show depends on the terminal.
            markup = detail.get("description_html")
            if markup:
                rendered = html_to_ansi(markup, depth=color_depth())
            else:
                rendered = detail["description"]
            lines.append(indent(rendered, "  "))
        if detail.get("comments"):
            lines.append("\n[comments]")
            for idx, comment in enumerate(detail["comments"], start=1):
                lines.append(f"  ({idx})")
                lines.append(indent(comment, "    "))
        submissions = [a for a in detail.get("attachments", []) if a.get("source") == "submission"]
        if submissions or detail.get("submission"):
            lines.append("\n[submissions]")
            if detail.get("submission"):
                lines.append(f"  {detail['submission']}")
            for sub in submissions:
                lines.append(f"  - {sub.get('name')} -> {display_url(sub.get('url'))}")

        other_attachments = [a for a in detail.get("attachments", []) if a.get("source") != "submission"]
        if other_attachments:
            lines.append("\n[attachments]")
            for attachment in other_attachments:
                lines.append(
                    f"  - {attachment.get('source')}: {attachment.get('name')} "
                    f"-> {display_url(attachment.get('url'))}"
                )

        feedback = data.get("feedback") or detail.get("feedback")
        if feedback and isinstance(feedback, dict):
            lines.append("\n[teacher feedback]")
            if feedback.get("error"):
                lines.append(f"  error: {feedback['error']}")
            else:
                for comment in feedback.get("general_comments", []):
                    lines.append(f"  general comment: {comment}")
                items = feedback.get("feedback_items", [])
                if not items and not feedback.get("general_comments"):
                    lines.append("  (no teacher feedback found)")
                for item in items:
                    name = item.get("submission_name") or "Submission"
                    lines.append(f"  - {name}:")
                    if item.get("comment"):
                        lines.append(indent(item["comment"], "    "))
                    for rub in item.get("rubric", []):
                        lines.append(f"    rubric: {rub.get('criterion', '')} -> {rub.get('score', '')}")
                    for att in item.get("attachments", []):
                        lines.append(f"    attachment: {att.get('name')} -> {display_url(att.get('url'))}")
        return "\n".join(lines)

    if command == "submit":
        return (
            "File submitted\n"
            f"  profile: {profile}\n"
            f"  filename: {data.get('filename')}\n"
            f"  task_url: {data.get('task_url')}"
        )

    if command == "notifications":
        stats = data.get("stats", {})
        items = data.get("items", [])
        meta = data.get("meta", {})
        lines = [
            "Notifications",
            f"  profile: {profile}",
            f"  unread: {stats.get('unread_messages', '?')}",
            f"  page: {meta.get('page', '?')}/{meta.get('total_pages', '?')}",
            f"  total: {meta.get('total', '?')}",
        ]
        for item in items:
            read_flag = " " if item.get("is_read") else "*"
            title = item.get("title", "?")
            created = (item.get("created_at") or "")[:16]
            lines.append(f"  {read_flag} [{item.get('id')}] {title}  ({created})")
        if not items:
            lines.append("  (none)")
        return "\n".join(lines)

    if command == "notifications.mutate":
        action = data.get("action", "?")
        nid = data.get("notification_id", "?")
        ok = data.get("ok", False)
        return f"Notification {action}\n  id: {nid}\n  ok: {ok}"

    if command == "calendar":
        events = data.get("events", [])
        lines = [
            "Calendar events",
            f"  profile: {profile}",
            f"  range: {data.get('start')} to {data.get('end')}",
            f"  count: {len(events)}",
        ]
        for e in events:
            start = (e.get("start") or "")[:16]
            lines.append(
                f"- [{e.get('id')}] {e.get('title')}  {start}  ({e.get('type')})"
            )
        if not events:
            lines.append("  (no events)")
        return "\n".join(lines)

    if command == "timetable":
        lessons = data.get("lessons", [])
        days = data.get("days", [])
        lines = [
            "Timetable",
            f"  profile: {profile}",
            f"  date: {data.get('start_date', 'this week')}",
            f"  days: {', '.join(d.get('header', '?') for d in days)}",
        ]
        by_day: dict[str, list[dict]] = {}
        for lesson in lessons:
            by_day.setdefault(lesson.get("day", ""), []).append(lesson)
        for day_name, day_lessons in by_day.items():
            marker = (
                " *"
                if any(d.get("is_today") and d.get("header") == day_name for d in days)
                else ""
            )
            lines.append(f"\n[{day_name}{marker}]")
            for l in day_lessons:
                p = l.get("period") or "?"
                t = l.get("time") or "?"
                s = l.get("subject") or "?"
                w = l.get("teacher") or "?"
                r = l.get("room") or ""
                lines.append(f"  {p:>12}  {t:>22}  {s:<30}  {w:<25}  {r}")
        if not lessons:
            lines.append("  (no lessons)")
        return "\n".join(lines)


    if command == "count-grade-freq":
        grades = data.get("grades", {})
        total = data.get("total", 0)
        num_classes = len(data.get("classes", []))
        lines = [
            "Grade Frequency Summary",
            f"  profile: {profile}",
            f"  classes counted: {num_classes}",
            f"  total tasks: {total}",
            "",
            f"  {'Grade':<20} {'Count':<5}",
            "  " + "-" * 26
        ]
        # Sort by count descending, then alphabetically by grade name
        sorted_grades = sorted(grades.items(), key=lambda x: (-x[1], x[0]))
        for grade, count in sorted_grades:
            lines.append(f"  {grade:<20} {count:<5}")
        return "\n".join(lines)

    if command in ("class.list", "grades.all"):
        classes = data.get("classes", [])
        lines = [
            "Classes Overview",
            f"  profile: {profile}",
            "",
        ]
        if not classes:
            lines.append("  (no classes found)")
            return "\n".join(lines)

        max_name_w = max((get_display_width(c.get("name") or c.get("class_name") or "") for c in classes), default=20)
        col_name_w = max(20, max_name_w)

        lines.append(
            f"  {'ID':<10} {pad_string('Class', col_name_w, 'left')} {'Overall Mark':<14} {'Score':<10} {'Assessed Categories'}"
        )
        lines.append("  " + "─" * (10 + 1 + col_name_w + 1 + 14 + 1 + 10 + 1 + 20))

        for c in classes:
            cid = str(c.get("id") or c.get("class_id") or "")
            name = c.get("name") or c.get("class_name") or ""
            overall = c.get("overall", {}) or {}
            mark = overall.get("mark") or "-"
            score = overall.get("score")
            score_str = f"{score:.2f}%" if score is not None else "-"

            comp = c.get("grade_composition") or []
            tot_cats = len(comp) if comp else c.get("categories_count", 0)
            assessed_cats = sum(1 for cat in comp if cat.get("score") is not None) if comp else c.get("assessed_categories_count", 0)
            cats_str = f"{assessed_cats} / {tot_cats} categories" if tot_cats else "-"

            name_col = pad_string(name, col_name_w, "left")
            lines.append(
                f"  {cid:<10} {name_col} {mark:<14} {score_str:<10} {cats_str}"
            )
        return "\n".join(lines)

    if command in ("class.view", "grades"):
        class_id = data.get("class_id") or data.get("id") or "?"
        class_name = data.get("class_name") or data.get("name") or "Unknown Class"
        overall = data.get("overall", {}) or {}
        mark = overall.get("mark") or "-"
        score = overall.get("score")
        score_str = f" ({score:.2f}%)" if score is not None else ""

        lines = [
            f"{class_name} ({class_id})",
            f"Overall Grade: {mark}{score_str}",
            "",
            "[Grade Composition]",
        ]
        composition = data.get("grade_composition") or []
        if not composition:
            lines.append("  (no category weighting information found)")
        else:
            cat_w = max((get_display_width(c.get("category") or "") for c in composition), default=12)
            cat_w = max(cat_w, len("Assessed Total"), 12)

            lines.append(
                f"  {pad_string('Category', cat_w, 'left')}  {'Weight':>8}   {'Mark':<6} {'Score':>8}    {'Contribution':>12}"
            )
            lines.append("  " + "─" * (cat_w + 2 + 8 + 3 + 6 + 1 + 8 + 4 + 12))

            total_weight = 0.0
            assessed_weight = 0.0
            weighted_points_sum = 0.0

            for cat in composition:
                cname = cat.get("category") or "Unknown"
                w = cat.get("weight", 0.0) or 0.0
                total_weight += w
                c_mark = cat.get("mark") or "-"
                c_score = cat.get("score")

                w_pct_str = f"{w * 100:.1f}".rstrip("0").rstrip(".") + "%"
                if c_score is not None:
                    assessed_weight += w
                    contribution = w * c_score
                    weighted_points_sum += contribution
                    score_cell = f"{c_score:.2f}%"
                    contrib_cell = f"{contribution:.2f}%"
                else:
                    score_cell = "-"
                    contrib_cell = "-"

                lines.append(
                    f"  {pad_string(cname, cat_w, 'left')}  {w_pct_str:>8}   {c_mark:<6} {score_cell:>8}    {contrib_cell:>12}"
                )

            lines.append("  " + "─" * (cat_w + 2 + 8 + 3 + 6 + 1 + 8 + 4 + 12))
            assessed_w_str = f"{assessed_weight * 100:.1f}".rstrip("0").rstrip(".") + "%"
            total_w_str = f"{total_weight * 100:.1f}".rstrip("0").rstrip(".") + "%"
            overall_contrib = f"{weighted_points_sum:.2f}% / {assessed_w_str}" if assessed_weight > 0 else "-"
            lines.append(
                f"  {pad_string('Assessed Total', cat_w, 'left')}  {assessed_w_str:>8}   {mark:<6} {score_str.strip(' ()') or '-':>8}    {overall_contrib:>12}"
            )

        grade_scale = data.get("grade_scale") or {}
        if grade_scale:
            lines.append("\n[Grading Scale]")
            scale_parts = []
            for k in sorted(grade_scale.keys(), key=lambda x: int(x) if x.isdigit() else x, reverse=True):
                lbl = grade_scale[k]
                scale_parts.append(f"{lbl}: Level {k}")
            lines.append("  " + "   ".join(scale_parts))

        return "\n".join(lines)

    if command == "grades.composition":
        classes = data.get("classes", [])
        lines = [
            "Grade Composition Across All Classes",
            f"  profile: {profile}",
            "",
        ]
        if not classes:
            lines.append("  (no classes found)")
            return "\n".join(lines)

        for c in classes:
            cname = c.get("name") or c.get("class_name") or "Unknown Class"
            overall = c.get("overall", {}) or {}
            mark = overall.get("mark") or "-"
            score = overall.get("score")
            score_str = f" ({score:.2f}%)" if score is not None else ""
            lines.append(f"{cname} [Overall: {mark}{score_str}]")

            comp = c.get("grade_composition") or []
            if not comp:
                lines.append("  (no category weights configured)")
            else:
                for cat in comp:
                    cat_name = cat.get("category") or "Unknown"
                    w = cat.get("weight", 0.0) or 0.0
                    w_str = f"{w * 100:.1f}".rstrip("0").rstrip(".") + "%"
                    c_mark = cat.get("mark") or "-"
                    c_score = cat.get("score")
                    if c_score is not None:
                        score_info = f"{c_mark} ({c_score:.2f}%)" if c_mark != "-" else f"{c_score:.2f}%"
                    elif c_mark != "-":
                        score_info = c_mark
                    else:
                        score_info = "-"
                    lines.append(f"  • {cat_name} ({w_str}): {score_info}")
            lines.append("")
        return "\n".join(lines)

    return json.dumps(payload, indent=2, ensure_ascii=False)


def print_payload(
    payload: dict, output_path: str | None = None, requested_format: str | None = None
) -> None:
    output_format = resolve_format(requested_format)
    rendered = (
        json.dumps(payload, indent=2, ensure_ascii=False)
        if output_format == "json"
        else render_pretty(payload)
    )
    if output_path:
        # Payloads can contain grade data or (via daemon status) a webhook
        # secret, so the file is created 0600 from birth rather than at the
        # umask default (0644).  mkstemp already opens 0600; the explicit
        # chmod pins it, and os.replace publishes the finished file in one
        # atomic step, so the destination is never briefly world-readable.
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(rendered)
                f.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, dest)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    else:
        print(rendered)


def ok(command: str, profile: str, data: dict) -> dict:
    return {
        "ok": True,
        "command": command,
        "profile": profile,
        "data": data,
    }


def error(command: str, code: str, message: str) -> dict:
    return {
        "ok": False,
        "command": command,
        "error": {
            "code": code,
            "message": message,
        },
    }
