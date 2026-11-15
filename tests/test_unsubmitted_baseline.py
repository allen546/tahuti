"""Tests for configurable unsubmitted percentage baseline."""

from datetime import datetime, timedelta
from pathlib import Path

from tahuti.config import AppState, ProfileConfig, SessionConfig, load_state, save_profile
from tahuti.task_status import (
    classify_task_view,
    extract_score_percentage,
    format_grade_display,
    get_task_display_status,
    is_task_todo,
    unsubmitted_baseline_context,
)


def test_extract_score_percentage():
    assert extract_score_percentage({"grade_score": "50 / 100 pts"}) == 50.0
    assert extract_score_percentage({"grade_score": "10 / 20 pts"}) == 50.0
    assert extract_score_percentage({"grade_score": "F (40/100)"}) == 40.0
    assert extract_score_percentage({"grade_score": "50%"}) == 50.0
    assert extract_score_percentage({"grade_score": "0 / 0 pts"}) is None
    assert extract_score_percentage({"grade_score": "-"}) is None
    assert extract_score_percentage({"grade_score": "Submitted"}) is None
    assert extract_score_percentage({}) is None


def test_is_task_todo_with_baseline_regardless_of_submission_state():
    past_due = (datetime.now() - timedelta(days=2)).strftime("%b %d, %I:%M %p")
    for status, btn in [("not-submitted", True), ("submitted", False), (None, False)]:
        task = {
            "status": status,
            "has_submit_button": btn,
            "grade_letter": "F",
            "grade_score": "50 / 100 pts",
            "due_date": past_due,
        }
        assert is_task_todo(task) is False
        with unsubmitted_baseline_context(50.0):
            assert is_task_todo(task) is True
            assert classify_task_view(task) == "overdue"
            assert get_task_display_status(task) == "Incomplete (Todo)"

    # Above baseline (55%) stays graded/completed
    above = {
        "status": "not-submitted",
        "has_submit_button": True,
        "grade_letter": "F",
        "grade_score": "55 / 100 pts",
        "due_date": past_due,
    }
    with unsubmitted_baseline_context(50.0):
        assert is_task_todo(above) is False
        assert classify_task_view(above) == "past"


def test_format_grade_display_shows_both_unsubmitted_and_f():
    now = datetime(2026, 11, 15, 12, 0)
    past_task = {
        "status": "submitted",
        "grade_letter": "F",
        "grade_score": "50 / 100 pts",
        "due_date": "Nov 10, 11:55 PM",
    }
    future_task = {
        "status": "not-submitted",
        "grade_letter": "F",
        "grade_score": "40 / 100 pts",
        "due_date": "Nov 20, 11:55 PM",
    }
    with unsubmitted_baseline_context(50.0):
        assert (
            format_grade_display(past_task, now_ref=now)
            == "⚠ Unsubmitted · F ( 50 / 100 pts )"
        )
        assert (
            format_grade_display(past_task, standalone=True, now_ref=now)
            == "⚠ Unsubmitted · F (50 / 100 pts)"
        )
        assert (
            format_grade_display(future_task, now_ref=now)
            == "Unsubmitted · F ( 40 / 100 pts )"
        )


def test_config_roundtrip_unsubmitted_baseline(tmp_path: Path):
    cfg = tmp_path / "config.json"
    sess = tmp_path / "session.json"
    state = AppState(
        config_path=cfg,
        session_path=sess,
        active_profile="default",
        profile=ProfileConfig(name="default", unsubmitted_baseline=50.0),
        session=SessionConfig(name="default"),
    )
    save_profile(state)
    loaded = load_state(config_path=cfg, session_path=sess)
    assert loaded.profile.unsubmitted_baseline == 50.0

    # Invalid values coerce to None
    cfg.write_text('{"version": 1, "defaults": {"unsubmitted_baseline": true}}')
    assert load_state(config_path=cfg, session_path=sess).profile.unsubmitted_baseline is None
    cfg.write_text('{"version": 1, "defaults": {"unsubmitted_baseline": 150}}')
    assert load_state(config_path=cfg, session_path=sess).profile.unsubmitted_baseline is None
