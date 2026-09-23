# Python SDK & Library Reference (`tahuti`)

`tahuti` provides an unopinionated, strongly-typed Python SDK for programmatic interaction with ManageBac instances (`managebac.com` and `managebac.cn`).

It supports both **synchronous request-response operations** (fetching tasks, downloading resources, submitting assignments, inspecting grades) and **asynchronous event streaming** (`async for event in daemon.stream()`).

Note that "event streaming" here means *in-process* streaming: the daemon detects
changes by **polling** ManageBac on a configurable interval (30 s by default, plus
0–5 s of jitter). ManageBac exposes no push or WebSocket channel to a student
account, so `stream()` yields what the poller finds rather than a live socket. See
[events.md](events.md) for the transport details and the evidence for that.

---

## Table of Contents

1. [Installation & Requirements](#1-installation--requirements)
2. [Quickstart](#2-quickstart)
3. [Client Initialization & Authentication (`ManageBacClient`)](#3-client-initialization--authentication-managebacclient)
4. [Coursework, Tasks & Details](#4-coursework-tasks--details)
5. [Submissions & Dropbox Operations](#5-submissions--dropbox-operations)
6. [Teacher Feedback & Grades](#6-teacher-feedback--grades)
7. [Calendar, Timetables & Schedules](#7-calendar-timetables--schedules)
8. [Event Streaming (`ManageBacDaemon`)](#8-real-time-event-streaming-managebacdaemon)
9. [Event Model & Schema (`MBEvent`)](#9-event-model--schema-mbevent)
10. [MNN Hub Notifications (`MNNHubClient`)](#10-mnn-hub-push-notifications-mnnhubclient)
11. [Status Enums & Classification](#11-status-enums--classification)
12. [Caching & Performance Controls](#12-caching--performance-controls)
13. [End-to-End Integration Recipes](#13-end-to-end-integration-recipes)

---

## 1. Installation & Requirements

- **Python**: `>= 3.10`
- **Dependencies**: `requests`, `beautifulsoup4`

Install via pip or uv:

```bash
pip install tahuti
# or with uv
uv pip install tahuti
```

The MCP server (`tahuti-mcp`) needs the optional `mcp` extra — a plain install
does not pull it in, and the server fails to import without it:

```bash
pip install "tahuti[mcp]"
```

To run the test suite from a checkout, install the dev group instead:

```bash
uv sync --group dev
```

Top-level library exports:

```python
from tahuti import (
    ManageBacClient,
    ManageBacDaemon,
    MBEvent,
    MNNHubClient,
)
```

---

## 2. Quickstart

### Synchronous Task Inspection

```python
from tahuti import ManageBacClient

# Initialize client using saved credentials from the CLI
# (config.json / session.json under ~/.config/tahuti/, or wherever
# MB_CRAWLER_CONFIG / MB_CRAWLER_SESSION point)
client = ManageBacClient.from_config()

# Fetch all upcoming coursework
data = client.crawl_all(fetch_details=True)
for task in data.get("upcoming", []):
    print(f"[{task.get('due_date')}] {task.get('title')} ({task.get('class_name')})")
```

### Asynchronous Event Streaming (polled)

```python
import asyncio
from tahuti import ManageBacClient, ManageBacDaemon

async def main():
    client = ManageBacClient.from_config()
    daemon = ManageBacDaemon(client, poll_interval_seconds=60.0)

    print("Subscribing to ManageBac events...")
    async for event in daemon.stream():
        print(f"[{event.event}] {event.data['title']} (Due: {event.data['due_date']})")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 3. Client Initialization & Authentication (`ManageBacClient`)

`ManageBacClient` is the core HTTP gateway for interacting with ManageBac web endpoints.

### 3.1 Automatic Construction from Config

If you have previously authenticated using the CLI (`mb login`), load the authenticated client directly:

```python
# Uses the active profile (default: "default")
client = ManageBacClient.from_config()

# Or specify a custom profile
client = ManageBacClient.from_config(profile="demo-school")
```

### 3.2 Explicit Instantiation

```python
client = ManageBacClient(
    school="myschool",               # Subdomain, e.g. "myschool" for "myschool.managebac.cn"
    domain="managebac.cn",        # Base domain ("managebac.com" or "managebac.cn")
    verify=True,                  # TLS verification (set False for self-signed proxies)
    retry=3,                      # Max retries for retryable status codes (429, 500, 502, 503, 504)
    request_delay=1.0,            # Politeness delay between successive HTTP requests in seconds
)
```

### 3.3 Credentials & Session Methods

```python
# Authenticate with credentials (retrieves and stores session cookies)
success = client.login("student@school.edu", "secret_password", remember=True)

# Or set an existing session cookie manually
client.set_cookie("YOUR_MANAGEBAC_SESSION_COOKIE")

# Helpful properties
print(client.school)         # "myschool"
print(client.subdomain)      # "myschool" (alias)
print(client.domain)         # "managebac.cn"
print(client.base)           # "https://myschool.managebac.cn"
print(client.student_name)   # Automatically captured from DOM on first authenticated request
```

---

## 4. Coursework, Tasks & Details

### 4.1 Crawl All Tasks

```python
# Fetch upcoming, past, and overdue coursework, plus class grades and notifications
overview = client.crawl_all(
    fetch_details=True,               # Fetch deep task descriptions, rubrics & attachments
    fetch_notifications=True,         # Include MNN hub notification stats and items
    max_pages=10,                     # Max pagination depth
)

upcoming_tasks = overview.get("upcoming", [])
past_tasks = overview.get("past", [])
overdue_tasks = overview.get("overdue", [])
class_grades = overview.get("class_grades", {})
```

### 4.2 Fetch Tasks by View

```python
# Query a specific view directly ("upcoming", "past", or "overdue")
upcoming = client.get_tasks_by_view(view="upcoming", max_pages=5)
past = client.get_tasks_by_view(view="past", max_pages=5)
overdue = client.get_tasks_by_view(view="overdue", max_pages=5)
```

### 4.3 Deep Task Detail

```python
# Pass either a relative URL or full ManageBac URL
task_detail = client.get_task_detail(
    "/student/classes/1000001/core_tasks/1000009",
    bypass_cache=False,
)

if task_detail:
    print("Title:", task_detail.get("title"))
    print("Description:", task_detail.get("description"))
    print("Due Date:", task_detail.get("due_date"))
    print("Has Dropbox:", task_detail.get("has_submit_button"))
    print("Attachments:", task_detail.get("attachments"))  # List of dicts with name, url, id
    print("Rubric Criteria:", task_detail.get("criteria"))
```

### 4.4 Find Task by ID

```python
# Search for a specific task ID across pagination
task = client.find_task_by_id(task_id="1000014", max_pages=20)
```

### 4.5 Class-Specific Tasks

```python
# Fetch all tasks scoped to a specific class ID
class_tasks = client.get_class_tasks(class_id="1000024", fetch_details=False)
```

---

## 5. Submissions & Dropbox Operations

### 5.1 Upload and Submit a File

```python
result = client.submit_file(
    class_id="1000023",
    task_id="1000026",
    file_path="/path/to/my_essay.pdf",
)

if result.get("ok"):
    print("Submission successful:", result.get("filename"))
else:
    print("Submission failed:", result.get("error"))
```

### 5.2 List Existing Submissions

```python
submissions = client.get_submissions(class_id="1000023", task_id="1000026")
for sub in submissions:
    print(f"ID: {sub['id']} | Filename: {sub['filename']} | Submitted: {sub['submitted_at']}")
```

### 5.3 Delete a Submission (with Verification)

```python
# Delete an uploaded submission and verify removal
del_result = client.delete_submission(
    class_id="1000023",
    task_id="1000026",
    submission_id="4982314",
    verify=True,  # Re-queries dropbox to confirm the file is gone
)

print("Deleted:", del_result.get("ok"))
```

---

## 6. Teacher Feedback & Grades

### 6.1 Inspect Teacher Feedback

```python
feedback = client.get_teacher_feedback(class_id="1000023", task_id="1000026")
print("Comments:", feedback.get("comments"))
print("Annotations:", feedback.get("annotations"))
print("Rubric Grades:", feedback.get("rubric_evaluation"))
```

### 6.2 Class Grades & Grade Composition

```python
# Get class roster
classes = client.get_classes()  # Returns dict: { "11516148": "AP Calculus BC", ... }

# Get official overall grade, category weighting composition, and grading scale for a class
grades_data = client.get_class_grades(class_id="11516148")
print("Class:", grades_data.get("class_name"))
print("Overall Mark:", grades_data["overall"]["mark"])       # e.g. "B"
print("Overall Score:", grades_data["overall"]["score"])     # e.g. 81.67
print("Grading Scale:", grades_data.get("grade_scale"))     # e.g. {"5": "A", "4": "B", ...}

for cat in grades_data.get("grade_composition", []):
    print(f"- {cat['category']} ({cat['weight'] * 100:.0f}%): {cat['mark']} ({cat['score']}%)")

# Fetch grades across all enrolled classes
all_grades = client.get_all_grades()
for c in all_grades.get("classes", []):
    print(f"{c['class_name']}: {c['overall']['mark']} ({c['overall']['score']}%)")
```

---

## 7. Calendar, Timetables & Schedules

### 7.1 Calendar Events

```python
# Fetch events in ISO date range YYYY-MM-DD
events = client.get_calendar_events(start="2026-09-01", end="2026-09-30")
for event in events:
    print(f"[{event['start']}] {event['title']} ({event['type']})")
```

### 7.2 iCalendar (iCal) Feed

```python
# Retrieve raw iCalendar .ics string for external calendar subscriptions
ical_data = client.get_ical_feed()
```

### 7.3 Weekly Timetable

```python
# Retrieve parsed weekly timetable for current week or specific Monday
timetable = client.get_timetable(start_date="2026-09-14")
for day, periods in timetable.get("days", {}).items():
    print(f"--- {day} ---")
    for period in periods:
        print(f"{period['time']}: {period['subject']} ({period['classroom']})")
```

---

## 8. Event Streaming (`ManageBacDaemon`)

The `ManageBacDaemon` provides an in-process, non-blocking asynchronous event generator that polls ManageBac and yields typed `MBEvent` objects in real time.

### 8.1 Async Generator Usage

```python
import asyncio
from tahuti import ManageBacClient, ManageBacDaemon

async def event_listener():
    client = ManageBacClient.from_config()
    daemon = ManageBacDaemon(
        client=client,
        poll_interval_seconds=120.0,  # Polling interval in seconds
    )

    try:
        async for event in daemon.stream():
            handle_event(event)
    except asyncio.CancelledError:
        print("Listener stopped cleanly.")

def handle_event(event):
    if event.event == "task_created":
        print(f"New Assignment: {event.data['title']} in {event.data['class_name']}")
    elif event.event == "task_graded":
        print(f"Grade Posted: {event.data['title']} -> {event.data['grade_score']}")
    elif event.event == "deadline_approaching":
        print(f"Approaching Deadline: {event.data['title']} due in {event.data['due_date']}")

asyncio.run(event_listener())
```

### 8.2 Architecture & Concurrency Guarantees

- **Non-blocking Polling**: Synchronous web crawling runs inside worker threads via `asyncio.to_thread()`, keeping the caller's event loop completely unblocked for handling other I/O, WebSockets, or GUI updates.
- **Dynamic Loop Binding**: The internal event queue is bound dynamically to `asyncio.get_running_loop()` on calling `stream()`, ensuring safety across event loops.
- **Single-Consumer Guard**: Multiple concurrent iterators on the same daemon instance raise `RuntimeError("stream() is already running on this ManageBacDaemon instance.")`.
- **Graceful Termination**: Breaking from the loop or cancelling the task signals `daemon.stop()` via an internal `_STOP_SENTINEL`, cleanly joining background workers without resource leaks.

---

## 9. Event Model & Schema (`MBEvent`)

All events emitted by `ManageBacDaemon.stream()` and the webhook daemon conform to the `MBEvent` structure:

```python
class MBEvent:
    event: str          # Event name: "task_created", "task_updated", "task_graded", "deadline_approaching", etc.
    event_id: str       # Unique event UUID
    timestamp: str      # ISO-8601 UTC timestamp
    version: str        # Payload version ("1.0.0")
    data: dict          # Canonical task payload
```

### 9.1 The 12 Canonical Fields in `event.data`

| Field | Type | Description |
| :--- | :--- | :--- |
| `task_id` | `int \| str` | Numeric ManageBac task ID |
| `class_id` | `str` | Numeric class ID |
| `class_name` | `str` | Full course name (e.g. `"English Language Arts I (Hons)"`) |
| `title` | `str` | Title of the coursework assignment |
| `due_date` | `str` | Human-readable due date string (e.g. `"Sep 15, 2026 at 11:59 PM"`) |
| `due_iso` | `str \| None` | Standard ISO-8601 UTC timestamp (`"2026-09-15T23:59:00Z"`) |
| `has_submit_button` | `bool` | `True` if digital dropbox submission is required; `False` if offline/in-class |
| `category` | `str \| None` | Task category (e.g. `"Summative"`, `"Formative"`, `"Homework"`) |
| `status` | `str \| None` | Submission status (`"submitted"`, `"pending"`, `"overdue"`) |
| `grade_letter` | `str \| None` | Letter grade if posted (e.g. `"7"`, `"A*"`) |
| `grade_score` | `str \| None` | Raw score if posted (e.g. `"28/30"`) |
| `url` | `str` | Direct ManageBac link to the task |

### 9.2 Event Methods

```python
# Serialization
event_dict = event.to_dict()
json_string = event.to_json(indent=2)

# Validation
is_valid = event.validate()  # Validates envelope and canonical data fields

# Construct an event manually
event = MBEvent.from_task(
    task_dict,
    event="task_created",
)
```

---

## 10. MNN Hub Notifications (`MNNHubClient`)

ManageBac uses the ManageBac Notification Network (MNN) Hub for student activity
notices. This is a **REST** API (`/api/frontend/v2`) polled on an interval — it is
not a push or WebSocket channel.

```python
from tahuti import ManageBacClient, MNNHubClient

client = ManageBacClient.from_config()

# Retrieve active MNN token and hub endpoint
token, endpoint = client.get_notification_token()
mnn = MNNHubClient(endpoint, token)

# Check notification statistics
stats = mnn.stats()
print("Unread count:", stats.get("unread_count"))

# List notifications
response = mnn.list(page=1, per_page=20, filter_="all")
for item in response["items"]:
    print(f"[{item['created_at']}] {item['title']}: {item['body']}")

# Mark a specific notification as read
mnn.mark_read(notification_id="123456")

# Mark all notifications read
mnn.mark_all_read()
```

---

## 11. Status Enums & Classification

`tahuti.task_status` provides standard enums to classify tasks reliably:

```python
from tahuti.task_status import (
    SubmissionStatus,
    GradeStatus,
    LifecycleStatus,
    get_grade_status,
    is_task_graded,
)

# Check if a task is submitted or has an offline submission
from tahuti.filters import is_task_submitted

if is_task_submitted(task_dict):
    print("Work already submitted!")

# Grade inspection
if is_task_graded(task_dict):
    status = get_grade_status(task_dict)  # GradeStatus.SCORED or GradeStatus.COMPLETED
    print(f"Task is graded: {status}")
```

---

## 12. Caching & Performance Controls

`ManageBacClient` includes a smart in-memory and disk response cache (`ResponseCache`) to prevent rate limits and speed up operations.

```python
# Invalidate the entire client cache
client.invalidate_cache()

# Invalidate a single task cache entry when modified
client.invalidate_task_cache(class_id="1000023", task_id="1000026")

# Force bypass cache on specific reads
detail = client.get_task_detail("/student/classes/1000001/core_tasks/1000010", bypass_cache=True)
grades = client.get_class_grades(class_id="1000023", bypass_cache=True)
```

---

## 13. End-to-End Integration Recipes

### Recipe 1: Sync ManageBac Tasks to Todoist / Notion

```python
import asyncio
from tahuti import ManageBacClient, ManageBacDaemon

async def sync_loop():
    client = ManageBacClient.from_config()
    daemon = ManageBacDaemon(client, poll_interval_seconds=300.0)

    print("Listening for ManageBac assignments to sync...")
    async for event in daemon.stream():
        if event.event == "task_created":
            task = event.data
            create_external_todo(
                title=f"[{task['class_name']}] {task['title']}",
                due_date=task['due_iso'] or task['due_date'],
                description=f"ManageBac Link: {task['url']}\nRequires upload: {task['has_submit_button']}",
            )
        elif event.event == "task_graded":
            task = event.data
            print(f"Update grade in tracker: {task['title']} -> {task['grade_score']}")

def create_external_todo(title, due_date, description):
    # Call your Todoist / Notion / Google Tasks API here
    print(f"Creating Todo: {title} (Due: {due_date})")

if __name__ == "__main__":
    asyncio.run(sync_loop())
```

### Recipe 2: Automated Homework Submission Pipeline

```python
from tahuti import ManageBacClient

client = ManageBacClient.from_config()

# 1. Check for homework due today that requires a dropbox upload
upcoming = client.get_tasks_by_view("upcoming")
for task in upcoming:
    # Match assignment title
    if "Calculus Problem Set 4" in task.get("title", ""):
        class_id = task["class_id"]
        task_id = task["id"]
        
        # Verify dropbox is available
        detail = client.get_task_detail(task["url"])
        if detail.get("has_submit_button"):
            print(f"Submitting homework to {detail['title']}...")
            res = client.submit_file(class_id, task_id, "solutions.pdf")
            if res.get("ok"):
                print("Successfully submitted solutions.pdf!")
```
