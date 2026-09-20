import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tahuti.cache import ResponseCache
from tahuti.client import ManageBacClient
from tahuti.config import ProfileConfig
from tahuti.task_status import is_task_submitted
from tahuti.__main__ import (
    _resolve_task_ids,
    _set_submission_state,
    update_snapshot_with_class_tasks,
    load_snapshot,
    save_snapshot,
    cmd_submit,
    cmd_list,
    build_parser,
)

TASK_URL = "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000021"


def test_invalidate_task_cache(tmp_path: Path):
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True, ttl=1800)
    client = ManageBacClient("myschool", domain="managebac.cn", cache=cache)

    base = client.base
    task_url = f"{base}/student/classes/101/core_tasks/202"
    dropbox_url = f"{base}/student/classes/101/core_tasks/202/dropbox"
    hint_url = f"{base}/student/classes/101/events/202/hint"
    core_tasks_url = f"{base}/student/classes/101/core_tasks"
    unrelated_url = f"{base}/student/classes/999/core_tasks/888"

    cache.put(task_url, "task detail", 200)
    cache.put(dropbox_url, "dropbox page", 200)
    cache.put(hint_url, "hint page", 200)
    cache.put(core_tasks_url, "class core tasks", 200)
    cache.put(unrelated_url, "unrelated page", 200)

    # Invalidate task 202 in class 101
    client.invalidate_task_cache("101", "202")

    assert cache.get(task_url) is None
    assert cache.get(dropbox_url) is None
    assert cache.get(hint_url) is None
    assert cache.get(core_tasks_url) is None
    assert cache.get(unrelated_url) is not None


def test_get_class_tasks(tmp_path: Path):
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True)
    client = ManageBacClient("myschool", domain="managebac.cn", cache=cache)

    mock_class_data = {
        "tasks": [
            {
                "task_id": "111",
                "title": "HW 1",
                "url": "https://myschool.managebac.cn/student/classes/101/core_tasks/111",
                "points": "10",
                "grade_letter": "A",
                "due_date": "Sep 20, 5:00 PM",
                "status": "submitted",
                "has_submit_button": False,
                "labels": ["Homework", "Submitted"],
            },
            {
                "task_id": "222",
                "title": "HW 2",
                "url": "https://myschool.managebac.cn/student/classes/101/core_tasks/222",
                "points": None,
                "grade_letter": None,
                "due_date": "Oct 1, 5:00 PM",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Homework", "Pending"],
            },
        ]
    }

    with patch.object(client, "get_class_grades", return_value=mock_class_data):
        tasks = client.get_class_tasks("101", class_name="Physics", bypass_cache=True)

    assert len(tasks) == 2
    assert tasks[0]["id"] == "111"
    assert tasks[0]["class_name"] == "Physics"
    assert tasks[0]["status"] == "submitted"
    assert tasks[1]["id"] == "222"
    assert tasks[1]["status"] == "not-submitted"
    assert tasks[1]["has_submit_button"] is True


def test_resolve_task_ids_snapshot_fast_path(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_data = {
        "crawled_at": "2026-09-13T10:00:00",
        "upcoming": [
            {
                "id": "1000021",
                "title": "kinematics",
                "link": "https://demo-school.managebac.cn/student/classes/1000011/core_tasks/1000021",
            }
        ],
        "past": [],
        "overdue": [],
    }
    save_snapshot(snapshot_path, snapshot_data)

    client = MagicMock()
    # crawl_all should NOT be called because the task is in the snapshot!
    client.crawl_all.side_effect = RuntimeError("Should not crawl!")

    cid, tid = _resolve_task_ids(client, "1000021", snapshot_path=snapshot_path)
    assert cid == "1000011"
    assert tid == "1000021"
    client.crawl_all.assert_not_called()


def test_resolve_task_ids_fallback_to_crawl(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(snapshot_path, {"upcoming": [], "past": [], "overdue": []})

    client = MagicMock()
    client.crawl_all.return_value = {
        "upcoming": [
            {
                "id": "1000099",
                "link": "https://demo-school.managebac.cn/student/classes/12345/core_tasks/1000099",
            }
        ],
        "past": [],
        "overdue": [],
    }

    cid, tid = _resolve_task_ids(client, "1000099", snapshot_path=snapshot_path)
    assert cid == "12345"
    assert tid == "1000099"
    client.crawl_all.assert_called_once()


def test_update_snapshot_with_class_tasks(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot.json"
    initial_snapshot = {
        "crawled_at": "2026-09-13T12:00:00",
        "upcoming": [
            {
                "id": "1",
                "title": "Task 1",
                "class_name": "Math",
                "due_date": "Dec 1, 10:00 AM",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Pending"],
            },
            {
                "id": "2",
                "title": "Task 2",
                "class_name": "Physics",
                "due_date": "Dec 2, 10:00 AM",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Pending"],
            },
        ],
        "past": [],
        "overdue": [],
    }
    save_snapshot(snapshot_path, initial_snapshot)

    # Updated Physics tasks (Task 2 submitted, Task 3 added)
    updated_physics_tasks = [
        {
            "id": "2",
            "title": "Task 2",
            "class_name": "Physics",
            "due_date": "Dec 2, 10:00 AM",
            "status": "submitted",
            "has_submit_button": False,
            "labels": ["Submitted"],
        },
        {
            "id": "3",
            "title": "Task 3",
            "class_name": "Physics",
            "due_date": "Dec 5, 10:00 AM",
            "status": "not-submitted",
            "has_submit_button": True,
            "labels": ["Pending"],
        },
    ]

    client = MagicMock()
    updated = update_snapshot_with_class_tasks(
        snapshot_path, updated_physics_tasks, client=client
    )

    loaded = load_snapshot(snapshot_path)
    # Task 1 (Math) should still be present and unmodified
    t1 = next(t for t in loaded["upcoming"] if t["id"] == "1")
    assert t1["status"] == "not-submitted"

    # Task 2 should now be submitted!
    t2 = next(t for t in loaded["upcoming"] if t["id"] == "2")
    assert t2["status"] == "submitted"
    assert t2["has_submit_button"] is False

    # Task 3 should be added
    t3 = next(t for t in loaded["upcoming"] if t["id"] == "3")
    assert t3["title"] == "Task 3"


def _row(snapshot: dict, task_id: str) -> dict:
    """Return a task's row from whichever section it ended up in.

    ``update_snapshot_with_class_tasks`` reclassifies every row it touches, so
    a row's section is an output of the merge rather than a fixed place to
    look.  Reaching for ``snapshot["upcoming"][0]`` also makes a test fail for
    a reason that has nothing to do with what it is about, the first time the
    fixture's due date falls into the past.
    """
    for section in ("upcoming", "past", "overdue"):
        for task in snapshot.get(section, []):
            if task.get("id") == task_id:
                return task
    raise AssertionError(f"task {task_id} is in no section of the snapshot")


def test_cmd_submit_eager_refresh_end_to_end(tmp_path: Path, capsys):
    parser = build_parser()
    submit_args = parser.parse_args(["submit", "1000021", str(tmp_path / "work.pdf")])
    (tmp_path / "work.pdf").write_bytes(b"%PDF-test")

    # Prepare snapshot where task 1000021 is unsubmitted.  The row's link names
    # the same task as its id: a row whose link points at a different task is
    # not a snapshot any crawl could have produced, and it silently routed this
    # test through the no-row branch below instead of the one it is named for.
    snapshot_path = tmp_path / "snapshot.json"
    initial_snapshot = {
        "crawled_at": "2026-09-13T12:00:00",
        "student_name": "Test Student",
        "school": "demo-school",
        "base_url": "https://demo-school.managebac.cn",
        "upcoming": [
            {
                "id": "1000021",
                "title": "kinematics classwork1",
                "class_name": "AP Physics 1",
                "due_date": "Dec 13, 5:55 PM",
                "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000021",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Formative", "Pending"],
            }
        ],
        "past": [],
        "overdue": [],
    }
    save_snapshot(snapshot_path, initial_snapshot)

    mock_state = MagicMock()
    mock_state.config_path = tmp_path / "config.json"
    mock_state.active_profile = "default"
    # cmd_list reads the profile's defaults when the flags are absent.  Use the
    # real dataclass so `default_view`/`default_subject` are concrete: with a
    # bare MagicMock the view was an unspecified object and the subject a
    # MagicMock, which only rendered because the pretty formatter stringified
    # them.  The default output for a non-TTY stdout is JSON.
    mock_state.profile = ProfileConfig(name="default")

    mock_client = MagicMock()
    mock_client.base = "https://demo-school.managebac.cn"
    mock_client.domain = "managebac.cn"
    mock_client.school = "demo-school"
    mock_client.student_name = "Test Student"
    mock_client.submit_file.return_value = {
        "ok": True,
        "filename": "work.pdf",
        "task_url": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000021",
    }
    # What the class grade page would say about the task afterwards.  The row is
    # already in the snapshot, so `submit` writes the submission state locally
    # and never asks for this — see
    # test_cmd_submit_with_snapshot_row_writes_state_locally.
    mock_client.get_class_tasks.return_value = [
        {
            "id": "1000021",
            "title": "kinematics classwork1",
            "class_name": "AP Physics 1",
            "due_date": "Dec 13, 5:55 PM",
            "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000021",
            "status": "submitted",
            "has_submit_button": False,
            "labels": ["Formative", "Submitted"],
        }
    ]

    with (
        patch("tahuti.__main__._build_client", return_value=(mock_state, mock_client, "user@test.com")),
        patch("tahuti.__main__._authenticate_client"),
    ):
        code = cmd_submit(submit_args)
        assert code == 0

    # Verify snapshot was eagerly refreshed to submitted!
    snap = load_snapshot(snapshot_path)
    updated_t = _row(snap, "1000021")
    assert updated_t["status"] == "submitted"
    assert updated_t["has_submit_button"] is False
    # Labels are a field of the class grade page, and the local write does not
    # refetch that page — `["Formative", "Pending"]` above is what the row keeps.
    # What the fetched page would have carried is asserted on the no-row branch,
    # which is the only arm that still fetches.

    # Now verify that cmd_list with --todo excludes this submitted task!
    list_args = parser.parse_args(["list", "--todo"])
    # cmd_list re-crawls (the snapshot's crawled_at is old), so give the crawl a
    # realistic payload: with a MagicMock return value the merged result holds
    # MagicMocks, which only rendered because the pretty formatter stringified
    # them.  The default output for a non-TTY stdout is JSON, which cannot.
    mock_client.crawl_all.return_value = {
        "student_name": "Test Student",
        "school": "demo-school",
        "base_url": "https://demo-school.managebac.cn",
        "crawled_at": "2026-09-13T12:00:00",
        "upcoming": [
            {
                "id": "1000021",
                "title": "kinematics classwork1",
                "class_name": "AP Physics 1",
                "due_date": "Dec 13, 5:55 PM",
                "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000021",
                "status": "submitted",
                "has_submit_button": False,
                "labels": ["Formative", "Submitted"],
            }
        ],
        "past": [],
        "overdue": [],
    }
    with (
        patch("tahuti.__main__._build_client", return_value=(mock_state, mock_client, "user@test.com")),
        patch("tahuti.__main__._authenticate_client"),
    ):
        code = cmd_list(list_args)
        assert code == 0

    captured = capsys.readouterr()
    # The submitted task should not appear in the todo list
    assert "kinematics classwork1" not in captured.out


def _submit_state(
    tmp_path: Path,
    *,
    task_id: str = "1000021",
    sibling: dict | None = None,
) -> tuple[MagicMock, MagicMock, Path]:
    """Build the pieces `cmd_submit` needs for a task already in the snapshot.

    The snapshot row and its link agree on the task id, which is what puts the
    command on the local-write arm: the class id comes from the link, so a row
    whose link names a different task resolves to a task id no row carries.
    """
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(
        snapshot_path,
        {
            "crawled_at": "2026-09-13T12:00:00",
            "student_name": "Test Student",
            "school": "demo-school",
            "base_url": "https://demo-school.managebac.cn",
            "upcoming": [
                {
                    "id": task_id,
                    "title": "kinematics classwork1",
                    "class_name": "AP Physics 1",
                    "due_date": "Dec 13, 5:55 PM",
                    "link": f"https://demo-school.managebac.cn/student/classes/1000001/core_tasks/{task_id}",
                    "status": "not-submitted",
                    "has_submit_button": True,
                    "labels": ["Formative", "Pending"],
                },
                *([sibling] if sibling else []),
            ],
            "past": [],
            "overdue": [],
        },
    )

    mock_state = MagicMock()
    mock_state.config_path = tmp_path / "config.json"
    mock_state.active_profile = "default"
    # The real dataclass, for the same reason the end-to-end test below uses it:
    # `cmd_list` reads `default_view`/`default_subject` off the profile.
    mock_state.profile = ProfileConfig(name="default")

    mock_client = MagicMock()
    mock_client.base = "https://demo-school.managebac.cn"
    mock_client.domain = "managebac.cn"
    mock_client.school = "demo-school"
    mock_client.student_name = "Test Student"
    mock_client.submit_file.return_value = {
        "ok": True,
        "filename": "work.pdf",
        "task_url": f"https://demo-school.managebac.cn/student/classes/1000001/core_tasks/{task_id}",
    }
    return mock_state, mock_client, snapshot_path


def _run_submit(mock_state, mock_client, args) -> int:
    with (
        patch(
            "tahuti.__main__._build_client",
            return_value=(mock_state, mock_client, "user@test.com"),
        ),
        patch("tahuti.__main__._authenticate_client"),
    ):
        return cmd_submit(args)


def test_cmd_submit_with_snapshot_row_writes_state_locally(tmp_path: Path):
    """A row that is already there is updated without re-fetching the class.

    The eager refresh used to re-read the whole class grade page — every task in
    the class, with `bypass_cache=True` so the response cache could not spare
    it — to flip one task's row.  Measured over the upload's own two requests,
    three became two, cold and warm.
    """
    mock_state, mock_client, snapshot_path = _submit_state(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["submit", "1000021", str(tmp_path / "work.pdf")])
    (tmp_path / "work.pdf").write_bytes(b"%PDF-test")

    assert _run_submit(mock_state, mock_client, args) == 0

    # The whole point: no class grade page is read for a task we already hold.
    mock_client.get_class_tasks.assert_not_called()
    mock_client.get_class_grades.assert_not_called()

    row = _row(load_snapshot(snapshot_path), "1000021")
    assert row["status"] == "submitted"
    assert row["has_submit_button"] is False
    assert is_task_submitted(row) is True
    # A local write still invalidates the task's cached detail pages, which is
    # what a crawl-detected change would have done.
    mock_client.invalidate_task_cache.assert_called_once_with("1000001", "1000021")


def test_cmd_submit_with_snapshot_row_leaves_other_rows_alone(tmp_path: Path):
    """Only the submitted task's row is touched.

    The fetch it replaces rewrote every row of the class from the page, so this
    pins the other side of the trade: siblings keep whatever the snapshot holds.
    """
    sibling = {
        "id": "1000022",
        "title": "other classwork",
        "class_name": "AP Physics 1",
        "due_date": "Dec 14, 5:55 PM",
        "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000022",
        "status": "not-submitted",
        "has_submit_button": True,
        "labels": ["Formative"],
        # A field only a detail-page crawl writes.  The class grade page has
        # nothing to say about it, so the fetch used to drop it from the row.
        "detail": {"grade_letter": None},
    }
    mock_state, mock_client, snapshot_path = _submit_state(tmp_path, sibling=sibling)

    parser = build_parser()
    args = parser.parse_args(["submit", "1000021", str(tmp_path / "work.pdf")])
    (tmp_path / "work.pdf").write_bytes(b"%PDF-test")

    assert _run_submit(mock_state, mock_client, args) == 0

    snapshot = load_snapshot(snapshot_path)
    untouched = _row(snapshot, "1000022")
    # `_reclassify_tasks` stamps a `view` on every row it walks past, siblings
    # included; every other field the sibling had survives the submit, `detail`
    # included.
    assert {k: v for k, v in untouched.items() if k != "view"} == sibling
    assert untouched["detail"] == {"grade_letter": None}
    # The submitted row itself is still written.
    assert _row(snapshot, "1000021")["status"] == "submitted"


def test_cmd_submit_without_snapshot_row_keeps_full_refetch(tmp_path: Path):
    """A task the snapshot has never listed still gets the full refresh.

    This arm is the only thing that puts such a task in the snapshot at all:
    there is no row to update locally, so skipping the fetch would lose a
    submitted task entirely.  That is a correctness regression, not an
    optimisation, which is why it stays.
    """
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(
        snapshot_path,
        {
            "crawled_at": "2026-09-13T12:00:00",
            "student_name": "Test Student",
            "school": "demo-school",
            "base_url": "https://demo-school.managebac.cn",
            "upcoming": [
                {
                    "id": "1000022",
                    "title": "other classwork",
                    "class_name": "AP Physics 1",
                    "due_date": "Dec 14, 5:55 PM",
                    "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000022",
                    "status": "not-submitted",
                    "has_submit_button": True,
                    "labels": ["Formative"],
                }
            ],
            "past": [],
            "overdue": [],
        },
    )

    mock_state = MagicMock()
    mock_state.config_path = tmp_path / "config.json"
    mock_state.active_profile = "default"
    mock_state.profile = ProfileConfig(name="default")

    mock_client = MagicMock()
    mock_client.base = "https://demo-school.managebac.cn"
    mock_client.domain = "managebac.cn"
    mock_client.school = "demo-school"
    mock_client.student_name = "Test Student"
    mock_client.submit_file.return_value = {
        "ok": True,
        "filename": "work.pdf",
        "task_url": TASK_URL,
    }
    # The page as it reads once the upload has landed: submitted, no dropbox
    # link, and a Submitted badge the local write would never have learned.
    mock_client.get_class_tasks.return_value = [
        {
            "id": "1000021",
            "title": "kinematics classwork1",
            "class_name": "AP Physics 1",
            "due_date": "Dec 13, 5:55 PM",
            "link": TASK_URL,
            "grade_letter": "A",
            "grade_score": "95/100",
            "status": "submitted",
            "has_submit_button": False,
            "labels": ["Formative", "Submitted"],
        }
    ]

    parser = build_parser()
    # Submitted by URL, which is how a task the snapshot never listed arrives.
    args = parser.parse_args(["submit", TASK_URL, str(tmp_path / "work.pdf")])
    (tmp_path / "work.pdf").write_bytes(b"%PDF-test")

    assert _run_submit(mock_state, mock_client, args) == 0

    mock_client.get_class_tasks.assert_called_once_with(
        "1000001", class_name=None, bypass_cache=True
    )
    snapshot = load_snapshot(snapshot_path)
    row = _row(snapshot, "1000021")
    assert row["status"] == "submitted"
    assert row["has_submit_button"] is False
    # Only the fetched page carries these.
    assert row["grade_letter"] == "A"
    assert "Submitted" in row["labels"]
    # The sibling survives the refresh untouched.
    assert _row(snapshot, "1000022")["status"] == "not-submitted"


def test_local_write_and_full_refetch_agree_on_submitted(tmp_path: Path):
    """The two arms must mean the same thing by "submitted".

    `_set_submission_state` writes `status` and `has_submit_button` together,
    on the grounds that they are two views of one fact; the fetched arm writes
    whatever the class grade page says.  For a task that has just been uploaded
    to those have to land on the same row, or which arm a submit happened to
    take would change what every later command believes about it.

    They do not agree on everything, and the difference is stated below rather
    than smoothed over: the fetched arm also brings back fields only the grade
    page carries, which is exactly what the local write declines to pay for.
    """
    starting_row = {
        "id": "1000021",
        "title": "kinematics classwork1",
        "class_name": "AP Physics 1",
        "due_date": "Dec 13, 5:55 PM",
        "link": TASK_URL,
        "status": "not-submitted",
        "has_submit_button": True,
        "labels": ["Formative"],
    }
    # What `get_class_tasks` reconstructs from a card whose dropbox has just
    # taken a file: the submitted span, no dropbox link, no badge in the labels.
    fetched_row = {
        "id": "1000021",
        "title": "kinematics classwork1",
        "class_name": "AP Physics 1",
        "due_date": "Dec 13, 5:55 PM",
        "link": TASK_URL,
        "grade_letter": "A",
        "grade_score": "95/100",
        "status": "submitted",
        "has_submit_button": False,
        "labels": ["Formative"],
    }

    local_path = tmp_path / "local.json"
    fetched_path = tmp_path / "fetched.json"
    for path in (local_path, fetched_path):
        save_snapshot(
            path,
            {
                "crawled_at": "2026-09-13T12:00:00",
                "student_name": "Test Student",
                "school": "demo-school",
                "base_url": "https://demo-school.managebac.cn",
                "upcoming": [dict(starting_row)],
                "past": [],
                "overdue": [],
            },
        )

    local_client = MagicMock()
    _set_submission_state(local_path, "1000021", True, client=local_client)

    fetched_client = MagicMock()
    update_snapshot_with_class_tasks(fetched_path, [dict(fetched_row)], client=fetched_client)

    local_row = _row(load_snapshot(local_path), "1000021")
    fetched = _row(load_snapshot(fetched_path), "1000021")

    for field in ("status", "has_submit_button", "view"):
        assert local_row[field] == fetched[field], field
    assert local_row["status"] == "submitted"
    assert local_row["has_submit_button"] is False
    assert is_task_submitted(local_row) is True
    assert is_task_submitted(fetched) is True

    # Both arms invalidate the cached detail pages for the row they changed.
    local_client.invalidate_task_cache.assert_called_once_with("1000001", "1000021")
    fetched_client.invalidate_task_cache.assert_called_once_with("1000001", "1000021")

    # Where they part company: the grade is a field of the page that was not
    # fetched, so the local write leaves the row without one and the grade shows
    # up at the next crawl instead of at submit time.
    assert "grade_letter" not in local_row
    assert fetched["grade_letter"] == "A"
