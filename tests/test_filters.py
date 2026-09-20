"""Tests for tahuti.filters."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure worktree src is prioritized over editable installs in venv
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from tahuti.filters import (
    InvalidViewError,
    filter_result_by_status,
    filter_result_by_subject,
    find_task_by_id,
    matches_grade_query,
    matches_subject,
    normalize_view,
    result_views,
    summary_of,
)


class TestMatchesSubject:
    def test_exact_match(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "Math HL") is True

    def test_case_insensitive(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "math hl") is True

    def test_partial_match(self):
        task = {"class_name": "CAIE IGCSE G9 EL-L0"}
        assert matches_subject(task, "EL") is True

    def test_no_match(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "Physics") is False

    def test_missing_class_name(self):
        task = {}
        assert matches_subject(task, "Math") is False

    def test_none_class_name(self):
        task = {"class_name": None}
        assert matches_subject(task, "Math") is False

    def test_empty_subject(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "") is True


class TestFilterResultBySubject:
    def test_filters_all_views(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[
                {"class_name": "Math HL", "title": "T1"},
                {"class_name": "English A", "title": "T2"},
            ],
            past=[
                {"class_name": "Math HL", "title": "T3"},
                {"class_name": "Physics", "title": "T4"},
            ],
            overdue=[
                {"class_name": "Math HL", "title": "T5"},
            ],
        )
        filtered = filter_result_by_subject(result, "Math")
        assert len(filtered["upcoming"]) == 1
        assert len(filtered["past"]) == 1
        assert len(filtered["overdue"]) == 1
        assert filtered["summary"]["total_count"] == 3
        assert filtered["subject_filter"] == "Math"

    def test_no_matches(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"class_name": "English", "title": "T1"}],
            past=[],
            overdue=[],
        )
        filtered = filter_result_by_subject(result, "ZZZZZ")
        assert len(filtered["upcoming"]) == 0
        assert filtered["summary"]["total_count"] == 0


class TestNormalizeView:
    """One validated view vocabulary shared by the CLI and MCP list_tasks."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("all", "all"),
            ("ALL", "all"),
            ("  All  ", "all"),
            ("all tasks", "all"),
            ("everything", "all"),
            ("upcoming", "upcoming"),
            # Case-insensitive and natural-language spellings an LLM produces.
            ("Upcoming", "upcoming"),
            ("upcoming tasks", "upcoming"),
            ("upcoming task", "upcoming"),
            ("upcomings", "upcoming"),
            ("future", "upcoming"),
            ("past", "past"),
            ("PAST", "past"),
            ("past tasks", "past"),
            ("previous", "past"),
            ("overdue", "overdue"),
            ("overdue tasks", "overdue"),
            ("late", "overdue"),
            ("missed", "overdue"),
        ],
    )
    def test_recognised(self, raw, expected):
        assert normalize_view(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "  ", "default", "none"])
    def test_unspecified_falls_back_to_default(self, raw):
        assert normalize_view(raw) == "all"
        assert normalize_view(raw, default="upcoming") == "upcoming"

    @pytest.mark.parametrize(
        "raw",
        [
            # A misspelled "all" must not silently mean "all".
            "al",
            "alll",
            "upcomingg",
            "everythinggg",
            "todo",
            "homework",
            "1",
        ],
    )
    def test_unrecognised_raises(self, raw):
        with pytest.raises(InvalidViewError) as excinfo:
            normalize_view(raw)
        # Machine-readable code, so a CLI caller gets a payload not a traceback.
        assert excinfo.value.code == "invalid_view"
        assert "upcoming" in excinfo.value.message

    def test_error_message_names_valid_views(self):
        with pytest.raises(InvalidViewError) as excinfo:
            normalize_view("al")
        for view in ("all", "upcoming", "past", "overdue"):
            assert view in excinfo.value.message


class TestMatchesGradeQuery:
    """Grade-letter / GPA query matching, incl. score-only grade cards.

    ``matches_grade_query`` had no coverage at all, which is how the broken
    ``^([A-F][+-]?)\\b`` anchor went unnoticed: ``\\b`` can never sit between a
    ``+``/``-`` and a following space, so the modifier group always backtracked
    to empty and "A+ (95/100)" matched the query "A" instead of "A+".
    """

    # ── score-only cards (no separate grade_letter) ────────────────────
    @pytest.mark.parametrize(
        "score,query",
        [
            ("A+ (95/100)", "A+"),
            ("B- (80/100)", "B-"),
            ("A+", "A+"),
            ("B+", "B+"),
            ("A", "A"),
            ("B (80/100)", "B"),
            ("F", "F"),
        ],
    )
    def test_modifier_survives_extraction(self, score, query):
        assert matches_grade_query({"grade_score": score}, query) is True

    def test_a_plus_score_does_not_match_a_minus_query(self):
        assert matches_grade_query({"grade_score": "A+ (95/100)"}, "A-") is False

    def test_bare_a_plus_string(self):
        # The bare "A+" case the docstring's own example depends on.
        assert matches_grade_query({"grade_score": "A+"}, "A") is True
        assert matches_grade_query({"grade_score": "A+"}, "A+") is True

    def test_letter_only_query_matches_any_modifier(self):
        assert matches_grade_query({"grade_score": "A- (90/100)"}, "A") is True
        assert matches_grade_query({"grade_score": "B+ (88/100)"}, "B") is True

    # ── grade_letter cards ─────────────────────────────────────────────
    def test_exact_letter(self):
        assert matches_grade_query({"grade_letter": "A"}, "A") is True
        assert matches_grade_query({"grade_letter": "B+"}, "B+") is True

    def test_letter_preferred_over_score(self):
        task = {"grade_letter": "C", "grade_score": "A+ (95/100)"}
        assert matches_grade_query(task, "C") is True
        assert matches_grade_query(task, "A+") is False

    def test_exact_query_requires_exact_match(self):
        assert matches_grade_query({"grade_letter": "A"}, "A+") is False
        assert matches_grade_query({"grade_letter": "A-"}, "A") is True

    # ── non-letter scores ──────────────────────────────────────────────
    def test_numeric_score_is_not_a_letter(self):
        task = {"grade_score": "95/100"}
        assert matches_grade_query(task, "A") is False
        assert matches_grade_query(task, "B") is False

    def test_percentage_score_is_not_a_letter(self):
        task = {"grade_score": "87%"}
        assert matches_grade_query(task, "A") is False
        assert matches_grade_query(task, "B") is False

    def test_word_starting_with_a_grade_letter_is_not_a_grade(self):
        # "Absent"/"Formative" must not be read as an A / an F.
        assert matches_grade_query({"grade_score": "Absent"}, "A") is False
        assert matches_grade_query({"grade_score": "Formative"}, "F") is False

    def test_not_applicable(self):
        assert matches_grade_query({"grade_letter": "N/A"}, "A") is False
        assert matches_grade_query({"grade_score": "N/A"}, "A") is False

    def test_empty_task(self):
        assert matches_grade_query({}, "A") is False
        assert matches_grade_query({"grade_letter": "", "grade_score": ""}, "A") is False

    def test_mismatched_query(self):
        assert matches_grade_query({"grade_letter": "A"}, "Physics") is False
        assert matches_grade_query({"grade_letter": "A"}, "") is False

    # ── GPA mappings ───────────────────────────────────────────────────
    @pytest.mark.parametrize(
        "letter,gpa",
        [
            ("A", "4.0"),
            ("A+", "4.0"),
            ("A-", "3.7"),
            ("B+", "3.3"),
            ("B", "3.0"),
            ("B-", "2.7"),
            ("C+", "2.3"),
            ("C", "2.0"),
            ("C-", "1.7"),
            ("D+", "1.3"),
            ("D", "1.0"),
            ("F", "0.0"),
        ],
    )
    def test_gpa_mapping(self, letter, gpa):
        assert matches_grade_query({"grade_letter": letter}, gpa) is True

    def test_gpa_mapping_is_exclusive(self):
        assert matches_grade_query({"grade_letter": "A-"}, "4.0") is False
        assert matches_grade_query({"grade_letter": "A"}, "3.7") is False
        assert matches_grade_query({"grade_score": "F (20/100)"}, "4.0") is False

    # ── tolerance ──────────────────────────────────────────────────────
    def test_case_and_whitespace_insensitive(self):
        assert matches_grade_query({"grade_score": "a+ (95/100)"}, "a+") is True
        assert matches_grade_query({"grade_letter": " b- "}, " B- ") is True

    def test_filter_result_by_status_uses_the_same_matching(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[
                {"id": "1", "grade_score": "A+ (95/100)"},
                {"id": "2", "grade_score": "B- (80/100)"},
            ]
        )
        filtered = filter_result_by_status(result, grade="A+")
        assert [t["id"] for t in filtered["upcoming"]] == ["1"]


class TestResultViews:
    def test_all_view(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "all")
        assert len(views["upcoming"]) == 1
        assert len(views["past"]) == 1
        assert len(views["overdue"]) == 1

    def test_capitalised_view(self, make_crawl_result):
        result = make_crawl_result(upcoming=[{"t": 1}], past=[{"t": 2}])
        views = result_views(result, "Upcoming")
        assert len(views["upcoming"]) == 1
        assert views["past"] == []

    def test_alias_view(self, make_crawl_result):
        result = make_crawl_result(upcoming=[{"t": 1}], overdue=[{"t": 2}])
        views = result_views(result, "overdue tasks")
        assert views["upcoming"] == []
        assert len(views["overdue"]) == 1

    def test_unknown_view_raises_instead_of_returning_all(self, make_crawl_result):
        # Previously an unrecognised view fell through to "all", so a config
        # typo quietly widened the query instead of failing.
        result = make_crawl_result(upcoming=[{"t": 1}], past=[{"t": 2}])
        with pytest.raises(InvalidViewError):
            result_views(result, "al")

    def test_upcoming_only(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "upcoming")
        assert len(views["upcoming"]) == 1
        assert len(views["past"]) == 0
        assert len(views["overdue"]) == 0

    def test_past_only(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "past")
        assert len(views["upcoming"]) == 0
        assert len(views["past"]) == 1
        assert len(views["overdue"]) == 0

    def test_overdue_only(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "overdue")
        assert len(views["upcoming"]) == 0
        assert len(views["past"]) == 0
        assert len(views["overdue"]) == 1


class TestSummaryOf:
    """One producer for the summary shape, so the surfaces cannot drift.

    The CLI's `list`, the MCP `list_tasks` tool and `filter_result_by_*` all
    hand-built the same four keys, and `crawl_all` built a three-key one, so a
    filtered result and a raw crawl carried different `summary` shapes.
    """

    def test_counts_each_view_and_the_total(self):
        views = {
            "upcoming": [{"t": 1}, {"t": 2}],
            "past": [{"t": 3}],
            "overdue": [],
        }
        assert summary_of(views) == {
            "upcoming_count": 2,
            "past_count": 1,
            "overdue_count": 0,
            "total_count": 3,
        }

    def test_ignores_keys_it_does_not_count(self):
        # A full crawl result carries student_name, school, notifications and
        # the rest; only the three view lists are counted.
        result = {
            "student_name": "X",
            "upcoming": [{"t": 1}],
            "past": [],
            "overdue": [],
            "notifications": {"unread_count": 4},
        }
        assert summary_of(result) == {
            "upcoming_count": 1,
            "past_count": 0,
            "overdue_count": 0,
            "total_count": 1,
        }

    def test_all_empty(self):
        views = {"upcoming": [], "past": [], "overdue": []}
        assert summary_of(views) == {
            "upcoming_count": 0,
            "past_count": 0,
            "overdue_count": 0,
            "total_count": 0,
        }

    def test_filter_result_by_subject_keeps_the_same_shape(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[
                {"t": 1, "class_name": "Math"},
                {"t": 2, "class_name": "Physics"},
            ]
        )
        filtered = filter_result_by_subject(result, "Math")
        # A filtered result and an unfiltered one must not carry different
        # `summary` shapes — that is what the shared helper buys.
        assert set(filtered["summary"]) == set(summary_of(filtered))
        assert filtered["summary"]["total_count"] == 1


class TestFindTaskById:
    def test_finds_in_upcoming(self, make_crawl_result, sample_task):
        result = make_crawl_result(upcoming=[sample_task])
        found = find_task_by_id(result, "1000026")
        assert found is sample_task

    def test_finds_in_past(self, make_crawl_result, sample_task):
        result = make_crawl_result(past=[sample_task])
        found = find_task_by_id(result, "1000026")
        assert found is sample_task

    def test_finds_in_overdue(self, make_crawl_result, sample_task):
        result = make_crawl_result(overdue=[sample_task])
        found = find_task_by_id(result, "1000026")
        assert found is sample_task

    def test_not_found(self, make_crawl_result, sample_task):
        result = make_crawl_result(upcoming=[sample_task])
        found = find_task_by_id(result, "99999999")
        assert found is None

    def test_empty_result(self, make_crawl_result):
        result = make_crawl_result()
        found = find_task_by_id(result, "123")
        assert found is None


class TestMatchesTag:
    def test_matches_tag_exact(self):
        from tahuti.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "Exam") is True

    def test_matches_tag_case_insensitive(self):
        from tahuti.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "exam") is True

    def test_matches_tag_partial(self):
        from tahuti.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "Sum") is True

    def test_matches_tag_no_match(self):
        from tahuti.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "Quiz") is False

    def test_matches_tag_no_labels(self):
        from tahuti.filters import matches_tag
        task = {}
        assert matches_tag(task, "Exam") is False


def test_is_submitted_badge():
    from tahuti.filters import is_submitted_badge
    assert is_submitted_badge("Submitted") is True
    assert is_submitted_badge("submitted") is True
    assert is_submitted_badge("Not Submitted") is False
    assert is_submitted_badge("unsubmitted") is False
    assert is_submitted_badge("Pending") is False


def test_is_task_submitted():
    from tahuti.filters import is_task_submitted
    assert is_task_submitted({"status": "submitted"}) is True
    assert is_task_submitted({"labels": ["Submitted"]}) is True
    assert is_task_submitted({"labels": ["Not Submitted"]}) is False
    assert is_task_submitted({"detail": {"submission": {"id": 123}}}) is True
    assert is_task_submitted({}) is False


def test_is_task_unfinished_and_completed():
    from tahuti.filters import is_task_completed, is_task_unfinished

    # Incomplete task: has submit button, not submitted, no passing grade, assessed
    task_todo = {
        "has_submit_button": True,
        "status": "not-submitted",
        "grade_letter": None,
        "grade_score": None,
        "labels": [],
    }
    assert is_task_unfinished(task_todo) is True
    assert is_task_completed(task_todo) is False

    # Completed: submitted
    task_submitted = dict(task_todo, status="submitted")
    assert is_task_unfinished(task_submitted) is False
    assert is_task_completed(task_submitted) is True

    # Completed: has valid grade letter
    task_graded = dict(task_todo, grade_letter="A")
    assert is_task_unfinished(task_graded) is False
    assert is_task_completed(task_graded) is True

    # Completed: exempt or N/A
    task_exempt = dict(task_todo, labels=["Exempt"])
    assert is_task_unfinished(task_exempt) is False
    assert is_task_completed(task_exempt) is True

    task_na = dict(task_todo, grade_letter="N/A")
    assert is_task_unfinished(task_na) is False
    assert is_task_completed(task_na) is True

    # "Not Assessed Yet" does NOT make an unsubmitted task complete (it remains TODO)
    task_not_assessed = dict(task_todo, labels=["Not Assessed Yet"])
    assert is_task_unfinished(task_not_assessed) is True
    assert is_task_completed(task_not_assessed) is False


def test_classify_task_view():
    from datetime import datetime
    from tahuti.filters import classify_task_view

    now = datetime(2026, 9, 10, 12, 0, 0)

    # Future task -> upcoming
    t_future = {
        "due_date": "2026-09-15 12:00:00",
        "has_submit_button": True,
        "status": "not-submitted",
    }
    assert classify_task_view(t_future, now_ref=now) == "upcoming"

    # Past deadline, unfinished -> overdue
    t_overdue = {
        "due_date": "2026-09-05 12:00:00",
        "has_submit_button": True,
        "status": "not-submitted",
    }
    assert classify_task_view(t_overdue, now_ref=now) == "overdue"

    # Past deadline, completed -> past
    t_past = {
        "due_date": "2026-09-05 12:00:00",
        "has_submit_button": True,
        "status": "submitted",
    }
    assert classify_task_view(t_past, now_ref=now) == "past"


def test_matches_submitted_not_submitted_bugfix():
    from tahuti.filters import matches_completed, matches_submitted
    task_not_sub = {"labels": ["Not Submitted"]}
    assert matches_submitted(task_not_sub, True) is False
    assert matches_submitted(task_not_sub, False) is True

    task_sub = {"labels": ["Submitted"]}
    assert matches_submitted(task_sub, True) is True
    assert matches_submitted(task_sub, False) is False

    task_todo = {
        "has_submit_button": True,
        "status": "not-submitted",
        "grade_letter": None,
        "grade_score": None,
        "labels": [],
    }
    assert matches_completed(task_todo, False) is True
    assert matches_completed(task_todo, True) is False

