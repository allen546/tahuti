import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tahuti.cache import ResponseCache
from tahuti.client import ManageBacClient
from tahuti.config import ProfileConfig
from tahuti.__main__ import (
    _resolve_task_ids,
    update_snapshot_with_class_tasks,
    load_snapshot,
    save_snapshot,
    cmd_submit,
    cmd_list,
    build_parser,
)


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


from bs4 import BeautifulSoup


def test_get_class_tasks(tmp_path: Path):
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True)
    client = ManageBacClient("myschool", domain="managebac.cn", cache=cache)

    html = """
    <div class="fusion-card-item">
        <div class="title"><a href="/student/classes/101/core_tasks/111">HW 1</a></div>
        <div class="points">10</div>
        <div class="grade">A</div>
        <div class="date">Sep 20, 5:00 PM</div>
        <div class="status submitted">Submitted</div>
        <div class="labels-set"><div class="label">Homework</div><div class="label">Submitted</div></div>
    </div>
    <div class="fusion-card-item">
        <div class="title"><a href="/student/classes/101/core_tasks/222">HW 2</a></div>
        <div class="date">Oct 1, 5:00 PM</div>
        <div class="status not-submitted">Pending</div>
        <a href="/student/classes/101/core_tasks/222/dropbox" class="btn">Submit Work</a>
        <div class="labels-set"><div class="label">Homework</div><div class="label">Pending</div></div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    with patch.object(client, "_get", return_value=soup):
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


def test_cmd_submit_eager_refresh_end_to_end(tmp_path: Path, capsys):
    parser = build_parser()
    submit_args = parser.parse_args(["submit", "1000021", str(tmp_path / "work.pdf")])
    (tmp_path / "work.pdf").write_bytes(b"%PDF-test")

    # Prepare snapshot where task 1000021 is unsubmitted
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
                "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000099",
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
        "task_url": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000099",
    }
    # Return fresh class tasks where task 1000021 is submitted
    mock_client.get_class_tasks.return_value = [
        {
            "id": "1000021",
            "title": "kinematics classwork1",
            "class_name": "AP Physics 1",
            "due_date": "Dec 13, 5:55 PM",
            "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000099",
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
    updated_t = snap["upcoming"][0]
    assert updated_t["status"] == "submitted"
    assert updated_t["has_submit_button"] is False
    assert "Submitted" in updated_t["labels"]

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
                "link": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000099",
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
