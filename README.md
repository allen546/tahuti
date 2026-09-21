# tahuti: ManageBac CLI, Python SDK & Event Engine

An unopinionated, robust toolkit for **ManageBac**: typed Python SDK, command-line interface, Model Context Protocol (MCP) server for AI assistants, and a near-real-time event streaming and webhook engine.

Supports both international (`managebac.com`) and China (`managebac.cn`) instances.

---

## Core Capabilities

1. **Unopinionated Python SDK** (`ManageBacClient`, `ManageBacDaemon`):
   - Authenticate seamlessly via credentials or saved session cookies (`ManageBacClient.from_config()`).
   - Programmatic access to tasks, submissions, grades, calendar feeds, weekly timetables, and MNN notifications.
   - Clean separation of concerns: produces pure, typed data with zero vendor-specific assumptions or hardcoded push rules.
2. **Interactive CLI** (`tahuti login`, `tahuti list`, `tahuti view`, `tahuti grades`, `tahuti submit`, `tahuti daemon`):
   - Fast terminal workflows for everyday student tasks: listing assignments, viewing details, uploading files, inspecting grades, checking schedules, and managing background daemons.
   - Smart output formatting: human-friendly colored tables on interactive TTYs, structured JSON when piped to files or other tools (`jq`).
3. **MCP Server for AI Coding Assistants**:
   - Built-in Model Context Protocol server (`tahuti-mcp`) with 14 tools for AI assistants like Claude Desktop, Gemini, and Cursor to inspect deadlines, grades, and coursework.
4. **Event Streaming & Webhook Engine**:
   - In-process async event streaming (`async for event in daemon.stream()`) for Python bots and background tasks.
   - Background daemon service (`tahuti daemon run --webhook-url ...`) dispatching typed `MBEvent` payloads to HTTP webhooks with HMAC-SHA256 signatures, exponential backoff retries, stealth jitter, and active-hours scheduling.
   - For the full event contract and JSON schema, see [Event Stream Specification](docs/events.md). For operational push notification setups (such as Bark for iOS), see [Downstream Notifier Guide](docs/downstream-notifier-guide.md).

> **Delivery is polling-based, not push.** No WebSocket or server-sent-event
> transport exists in `tahuti`, and the MNN hub endpoint ManageBac publishes is an
> HTTPS origin rather than a socket URL — so there is nothing to subscribe to.
> The daemon polls the ManageBac Notification Network (MNN) Hub REST API on a
> configurable interval — `poll_interval_seconds` (default 30s) plus a random
> `poll_jitter_seconds` (default 0-5s) — and emits events from the delta between
> successive polls. Expect latency of roughly one poll interval after an event
> appears on ManageBac. The hub endpoint attribute is scraped from the
> notifications page and used **only** to obtain the hub JWT and to derive the REST
> base URL; it is not a socket URL. See the
> [notification transport findings](docs/events.md#11-notification-transport-polling-not-push)
> for the full investigation.

---

## Disclaimer

**Use at your own risk.** This tool is an unofficial, community-maintained client and scraper. It is not affiliated with or endorsed by Faria Education Group or ManageBac. By using this tool, you acknowledge and accept the following:

Faria/ManageBac's legal documents restrict automated access:
- **robots.txt** (managebac.com): Disallows `/login`, `/admin`, `/api` for all user agents.
- **Terms of Use §1.2.6**: "Accounts registered by 'bots' or screen scrapers and/or other automated means are not permitted and access will be terminated without notice."
- **Terms of Service §5.5**: "Misuse of the Service, including but not limited to reverse engineering... may result in permanent and/or temporary suspension or termination of the School's account."
- **Terms of Service §1.4**: Violations may result in account termination without notice.
- **Terms of Service §9.4**: Schools exceeding 200 GB/month bandwidth may face caps or additional invoices.

**The authors bear no responsibility for any consequences resulting from its use, including account suspension or school-level penalties.** You are solely responsible for ensuring your use complies with your school's policies and ManageBac's Terms of Service.

---

## Installation

```bash
pip install .
```

The console commands are `tahuti` and `tahuti-mcp`. The pre-rename `mb` and
`mb-mcp` are kept as aliases, so existing scripts, shell aliases and MCP client
configs keep working — this document uses the new names throughout.

The `tahuti-mcp` MCP server needs one extra dependency. `mcp` is *not* a runtime
dependency of the `tahuti` CLI, so a plain `pip install .` leaves `tahuti-mcp` failing
with `ModuleNotFoundError: No module named 'mcp'`:

```bash
pip install "tahuti[mcp]"
```

Or install in editable mode for local development:
```bash
pip install -e .
pip install -e ".[mcp]"
```

This is also a [`uv`](https://docs.astral.sh/uv/) project, with the test
dependencies kept out of the published runtime environment in a `dev`
dependency group:

```bash
uv sync --group dev     # pytest + requests-mock + the mcp extra
uv run pytest
```

---

## Python SDK Quickstarts

> 📖 **Full Library Reference**: For complete method signatures, parameter types, status enums, MNN Hub integration, and production recipes, see [docs/library.md](docs/library.md).

### 1. Basic Client Usage (`ManageBacClient`)

Use `ManageBacClient` for synchronous fetching and actions:

```python
from tahuti import ManageBacClient

# Option A: Authenticate automatically from saved local CLI credentials
client = ManageBacClient.from_config()

# Option B: Explicit authentication
# client = ManageBacClient(school="your-school", domain="managebac.com")
# client.login("student@example.com", "your-password")

# 1. Fetch upcoming tasks and coursework
tasks_data = client.crawl_all(fetch_details=True)
for task in tasks_data.get("upcoming", []):
    print(f"[{task.get('due_date')}] {task.get('title')} ({task.get('class_name')})")

# 2. View one task in detail
task_detail = client.get_task_detail("/student/classes/1000024/core_tasks/1000025")
if task_detail:
    print(task_detail.get("description"))

# 3. Check class grades and computed expected scores
grades = client.get_class_grades(class_id="1000023")
print(f"Expected Grade: {grades.get('expected_grade')}")

# 4. View calendar events
events = client.get_calendar_events(start="2026-09-01", end="2026-09-07")

# 5. Fetch weekly timetable
timetable = client.get_timetable()

# 6. Upload homework file to assignment dropbox
client.submit_file(
    class_id="1000023",
    task_id="1000026",
    file_path="homework.pdf",
)
```

### 2. Async Event Streaming (`ManageBacDaemon`)

Use `ManageBacDaemon.stream()` to consume ManageBac events asynchronously in your Python application. Events are produced by polling on the interval you configure, so treat the first event after a change as arriving within one poll cycle rather than instantly:

```python
import asyncio
from tahuti import ManageBacClient, ManageBacDaemon

async def main():
    # Load authenticated client
    client = ManageBacClient.from_config()

    # Create daemon instance (crawling runs in worker threads, non-blocking)
    daemon = ManageBacDaemon(client, poll_interval_seconds=60)

    print("Subscribed to ManageBac event stream (Ctrl+C to stop)...")
    async for event in daemon.stream():
        print(f"\n[Event: {event.event} @ {event.timestamp}]")
        
        if event.event == "task_created":
            print(f"  📝 New Task: {event.data.get('title')}")
            print(f"     Class: {event.data.get('class_name')}")
            print(f"     Due: {event.data.get('due_date')}")
            print(f"     URL: {event.data.get('url')}")
            
        elif event.event == "deadline_approaching":
            print(f"  ⏰ Deadline Warning: {event.data.get('title')}")
            print(f"     Threshold: {event.data.get('reminder_threshold')}")
            print(f"     Remaining: {event.data.get('time_remaining_minutes')} mins")
            
        elif event.event == "task_graded":
            print(f"  📊 Grade Released: {event.data.get('title')}")
            print(f"     Score: {event.data.get('grade_letter')} {event.data.get('grade_score')}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected from event stream.")
```

---

## Event Webhook Engine

`tahuti` includes a background daemon that dispatches polled events to HTTP webhook receivers (e.g. local scripts, microservices, or custom bots):

```bash
# Run daemon in foreground with webhook dispatching
# Pass the secret via the environment so it does not appear in `ps` output
# or your shell history.
export MB_WEBHOOK_SECRET="your-hmac-secret"
tahuti daemon run --webhook-url http://127.0.0.1:8000/webhook --secret "$MB_WEBHOOK_SECRET"

# Or configure webhook URL persistently and run daemon.
# `start` only detaches when you pass -b/--background; without it the loop runs
# in the foreground and dies with your terminal.
tahuti daemon configure-webhook http://127.0.0.1:8000/webhook
tahuti daemon start -b --interval 1800 --active-hours-start 7 --active-hours-end 23

# Test the webhook connection with a mock ping
tahuti daemon test-webhook http://127.0.0.1:8000/webhook
```

> **Verify the signature.** The daemon signs every payload with HMAC-SHA256,
> but a receiver that ignores `X-MB-Signature` accepts forged events from
> anything that can reach its port. The bundled receiver in
> `extras/mb-notifier/` requires `--secret` (or `MB_WEBHOOK_SECRET`) and refuses
> unsigned, replayed, or stale pushes. Write your receiver the same way.

### Webhook HTTP Contract
- **Method**: `POST`
- **Headers**:
  - `Content-Type: application/json; charset=utf-8`
  - `User-Agent: tahuti-daemon/1.0`
  - `X-MB-Event: <event_type>` (e.g. `task_created`, `task_graded`)
- **Signing**: `X-MB-Signature: sha256=<hex_hmac>` (when `--secret` is configured).

  **BREAKING PROTOCOL CHANGE.** The HMAC covers the timestamp *and* the body:

  ```python
  signed_material = f"{X-MB-Timestamp}.".encode("utf-8") + request_body
  expected = "sha256=" + hmac.new(secret, signed_material, hashlib.sha256).hexdigest()
  ```

  Previously it covered the body alone, which left `X-MB-Timestamp`
  unauthenticated: anyone who captured a single POST could replay it
  indefinitely by rewriting that header, because the original digest still
  validated and the receiver's freshness check passed. Receivers built against
  the body-only construction reject **every** payload until they add the
  timestamp to the signed material. The bundled receiver in
  `extras/mb-notifier/` is updated in the same commit; check any receiver of
  your own against the construction above. The `.` delimiter keeps `ts=17` +
  `body="89ab"` from colliding with `ts=1789` + `body="ab"`.
- **Retry Mechanism**: Exponential backoff (`1s`, `2s`, `4s`) on network or
  server errors (5xx, 408, 429). Other 4xx are permanent and are not retried.
- **Specification**: See [docs/events.md](docs/events.md) for full payload schemas and documentation.

> **Latency and polling interval.** Events are detected by polling, so a receiver
> sees a new task or grade after the daemon's next cycle, not at the moment
> ManageBac publishes it. Inside an active window the cycle sleeps
> `poll_interval_seconds + random(0, poll_jitter_seconds)` (defaults 30s and 5s, so
> 30-35s); the jitter keeps request timing irregular rather than a fixed cadence.
> `tahuti daemon run` sets this with `--poll-interval`, `tahuti daemon start` with
> `--interval`. Lower it if you want tighter detection, but each cycle issues
> authenticated requests to ManageBac — an aggressive interval raises the risk of
> rate limiting or account flagging. There is no push channel to subscribe to
> instead; see the notification transport findings in
> [docs/events.md](docs/events.md#11-notification-transport-polling-not-push).
>
> **Active hours gate the polling, they do not throttle it.** With no
> `active_windows` configured the daemon polls around the clock. Set
> `--active-hours-start` / `--active-hours-end` (or an `active_windows` entry in
> the daemon JSON) and the loop sleeps outside the window instead of polling —
> useful for keeping a school-hours-only notifier from hammering ManageBac
> overnight. `--once` ignores the window: one cycle always runs.

---

## Interactive CLI Reference

```bash
# Authentication & Session
tahuti login --school your-school --domain managebac.com -e student@example.com
tahuti login --keep-credentials            # also save the password for silent renewal
tahuti login --keep-credentials --keychain # keep it in the OS keychain, not a file
tahuti login --no-remember-me              # omit remember_me from the login POST
tahuti --version                       # print the installed version and exit
tahuti logout                             # forget this profile's session + password
tahuti logout --all                       # every profile's, plus the legacy file
tahuti logout --keep-credentials          # keep the saved password
tahuti logout --purge                     # ...and forget the profile's school/domain/email

# Tasks & Coursework
tahuti list                             # list upcoming tasks
tahuti list --view past                 # past tasks
tahuti list --subject "Math"            # filter by class/subject
tahuti list --view overdue --details    # overdue tasks with full descriptions
tahuti view 1000025                    # view single task by ID
tahuti view "https://your-school.managebac.com/student/classes/1000024/core_tasks/1000025"

# File Submission
tahuti submit 1000026 homework.pdf     # upload file to assignment dropbox

# Submission Lifecycle
tahuti submissions 1000026 --list      # list current submissions for a task
tahuti submissions 1000026 --add hw.pdf  # upload to the task dropbox
tahuti submissions 1000026 --delete hw.pdf  # delete a submission by asset ID or filename
tahuti submissions 1000026 --check-feedback  # teacher feedback for a task
tahuti submissions 1000026 --check-feedback hw.pdf  # …narrowed to one submission (asset ID or filename)
tahuti download 1000026                # download every attachment + submission for a task
tahuti download 1000026 --no-attachments --output-dir ./math  # student submissions only
tahuti download 1000026 --pages 5      # search 5 pages server-side when the task is not in snapshot.json
tahuti feedback 1000026                # fetch teacher feedback for a submitted task

> **`tahuti download` reports what it wrote.** Every run ends in one payload:
> `downloaded` and `failed` lists with `downloaded_count` / `failed_count`, the
> resolved `output_dir`, and the task title. A partial failure is still a success
> — exit 0 as long as at least one file landed — and exit 1 means nothing landed,
> or the task could not be resolved at all (`task_not_found`, `no_task_link`,
> `detail_fetch_failed`). Without `--output-dir` the files land in
> `./task_<id>_<slug>/` under the current directory.

# Grades & Analytics
tahuti grades                           # grades for every enrolled class
tahuti grades --class-id 1000023       # detailed task grades for one class
tahuti grades --subject "Physics"       # fuzzy match class name
tahuti count-grade-freq                 # grade distribution across all classes

# Notifications & Feed
tahuti notifications                    # list MNN notifications (page 1)
tahuti notifications --unread-only      # unread only
tahuti notifications --read 235151424   # mark notification as read
tahuti notifications --unread 235151424  # mark it unread again
tahuti notifications --read-all         # mark all notifications read

# Schedule & Calendar
tahuti calendar                         # calendar events for next 7 days
tahuti calendar --today                 # today's events
tahuti calendar --ical -o calendar.ics  # export raw iCal feed
tahuti timetable                        # view weekly class timetable

# Background Daemon
tahuti daemon run --webhook-url http://127.0.0.1:8000/webhook  # foreground loop, Ctrl+C to stop
tahuti daemon start -b                 # detached background loop (-b / --background)
tahuti daemon start                    # foreground loop; dies with your terminal
tahuti daemon start --once             # run one check cycle and exit
tahuti daemon stop                     # stop background loop
tahuti daemon status                   # show daemon process status
tahuti daemon install                  # register an auto-start service (launchd/systemd)
tahuti daemon uninstall                # remove the auto-start service
tahuti daemon configure-channel qq 123456789  # deliver via a zeroclaw channel instead of HTTP
```

> **`tahuti daemon start` does not background by default.** Without `-b` /
> `--background` the polling loop runs in the *foreground* and terminates when
> your terminal closes. `start -b` re-executes itself as `tahuti daemon run` in a
> new session, writes the child's PID to `~/.config/tahuti/daemon.pid`, and
> appends output to `~/.config/tahuti/daemon.log` (both `0600`). Override
> either location with `--pid-file` / `--log-file`: `start` and `status` accept
> both, `stop` accepts only `--pid-file`, and `install` accepts only
> `--log-file`.
>
> **`--interval` and `--poll-interval` are the same flag.** `daemon start`
> historically spelled it `--interval` and `daemon run` `--poll-interval`; both
> names are now accepted on both commands, so a command copied between the two
> keeps working. They share one destination, so passing both is not an error —
> the last one on the line wins. `start -b` forwards whichever you used to the
> detached child as `--poll-interval`.
>
> **`--daemon-config` is not `--config`.** The daemon subcommands above read
> their webhook URL, interval, and active-hours window from a separate JSON file
> selected with `--daemon-config` — it is not the ManageBac `config.json` that
> `--config` selects, and the two are not interchangeable.

### Output Formatting
- **Interactive TTY**: Formatted tables with color highlights.
- **Piped / Non-TTY**: Structured JSON output.
- **Explicit Override**: Add `--format pretty` or `--format json` to any command.
- **Environment Override**: Set `TAHUTI_FORMAT=json` or `TAHUTI_FORMAT=pretty` to
  pin the shape for a script that runs sometimes with and sometimes without a
  terminal (cron, CI, `tee`). `--format` still wins over the environment, and the
  environment wins over the TTY probe. The pre-rename `MB_CLI_FORMAT` is still
  read as a fallback, so an existing script does not break; if both are set,
  `TAHUTI_FORMAT` wins.
- **Streams**: Standard output (`stdout`) is reserved for command data; logs and progress go to standard error (`stderr`).

### Configuration Files
By default, `tahuti` stores credentials and daemon states in `~/.config/tahuti/`:
- `config.json` — School domain, preferences, and webhook settings
- `session.json` — Authenticated session cookies and tokens
- `creds.json` / `creds.<profile>.json` — **Plaintext ManageBac password**,
  written only by `tahuti login --keep-credentials`, so an expired cookie can be
  renewed without a prompt. The `default` profile keeps the plain `creds.json`
  name; every other profile gets its own file.
- `snapshot.json` — Coursework state cache for delta detection
- `daemon.log` / `daemon.pid` — Background daemon runtime files
- `cache/` — Cached HTTP responses, including grade pages and the MNN hub JWT
- `daemon_state.json` — Notification/reminder dedup state

Every file holding a credential or personal data is written with `0600` and the
directory with `0700`. On startup `tahuti` warns on stderr if a creds file,
`session.json`, or `config.json` is found group- or world-readable, since file
permissions are the only barrier protecting a cleartext password. Set
`MANAGEBAC_NO_PERM_WARN=1` to silence it.

> **The session is saved; the password is not, unless you ask.** Every
> successful `tahuti login` writes `session.json` (the cookie), so you are not
> asked for your password on every command. Nothing writes your password to disk
> unless you pass `--keep-credentials`:
>
> | Command | `session.json` | password |
> |---|---|---|
> | `tahuti login` | written | not written |
> | `tahuti login --keep-credentials` | written | written |
> | `tahuti login --no-remember-me` | written | not written |
>
> `--no-remember-me` only omits `remember_me` from the login POST, so the
> cookie's lifetime is ManageBac's default rather than a requested persistent
> one. It is a server-side setting and composes with `--keep-credentials`.
>
> The cost of the default is one password when the cookie expires: with no
> stored password, a command run after that fails with `missing_credentials`
> naming `tahuti login --keep-credentials` rather than prompting (a prompt would
> hang a daemon or a CI job). Pass `--keep-credentials` once and later commands
> renew the session by themselves.
>
> `tahuti logout` **deletes** this profile's creds file and any OS-keychain
> entry, as well as clearing the session cookie and the response cache. Pass
> `logout --keep-credentials` if you want silent re-login preserved instead;
> `logout --all` clears every profile's file and the legacy global one.
>
> None of those touch the profile itself: the school, domain, email and
> `defaults` in `config.json` survive a logout, so the next command still knows
> which school to talk to. `tahuti logout --purge` removes that entry too —
> wholesale — and `logout --all --purge` removes every profile's, leaving
> `config.json` with an empty `profiles` map and no `active_profile` (a later
> command then uses the default profile name unless you pass `--profile`).
> `--purge` implies the credential deletion, so combining it with
> `--keep-credentials` is refused rather than silently doing one or the other.
>
> To keep the password out of the cleartext file entirely, combine `--keep-credentials`
> with the OS keychain:
> ```bash
> tahuti login --keep-credentials --keychain   # or: MANAGEBAC_KEYCHAIN=1
> ```
> This stores the password in the macOS Keychain or Linux Secret Service via the
> `security` / `secret-tool` helpers already on the system — no extra dependency,
> and nothing is stored if you do not ask for it. `--keychain` decides only
> *where* a kept password goes, never *whether* it is kept. If the keychain is
> unavailable (for example a headless Linux box with no secret service), `tahuti`
> falls back to the cleartext file with a warning rather than losing the
> credential. See [SECURITY.md](SECURITY.md) for the limits of both backends.

`--config <file>` and `--session-file <file>` override the default config and
session paths, as do the environment variables `MANAGEBAC_CONFIG`,
`MANAGEBAC_SESSION`, and `MANAGEBAC_CREDS_PATH`. These come from the shared
auth-flag helper, so they exist on the task, grades, calendar, and submission
commands — and on `daemon run` / `daemon start`, which do log in to ManageBac.
They are **not** on the purely process-level daemon commands, which act on the
daemon and its own JSON settings rather than on your login. Where those need a
path they take `--daemon-config` (on `run`, `stop`, `test-webhook`,
`configure-webhook`, and `configure-channel`) and/or `--pid-file` / `--log-file`
(`start` and `status` take both, `stop` takes only `--pid-file`, `install` takes
only `--log-file`); `uninstall` takes no path flag at all.

Secrets may also be supplied through the environment, which keeps them out of
your shell history and out of `ps` output:
- `MB_WEBHOOK_SECRET` — HMAC secret for signing webhook payloads. Preferred over
  `--secret` when both are set.
- `MANAGEBAC_PASSWORD` — ManageBac password.
- `MANAGEBAC_COOKIE` — `_managebac_session` cookie value.
- `MANAGEBAC_KEYCHAIN` — set to `1` to keep a password stored by
  `--keep-credentials` in the OS keychain instead of the cleartext creds file
  (equivalent to `tahuti login --keychain`). Decides *where*, never *whether*.
- `MANAGEBAC_NO_PERM_WARN` — set to `1` to silence the loose-permission warning.

> **The old `MB_CRAWLER_*` spellings still work.** `MB_CRAWLER_CONFIG`,
> `MB_CRAWLER_SESSION`, `MB_CRAWLER_CREDS_PATH`, `MB_CRAWLER_PASSWORD`,
> `MB_CRAWLER_COOKIE`, `MB_CRAWLER_KEYCHAIN` and `MB_CRAWLER_NO_PERM_WARN` are
> read as deprecated fallbacks, so an existing systemd unit, CI job or shell
> profile keeps authenticating. The new name wins when both are set, and an
> exported-but-empty value counts as unset either way. They are deprecated and
> will go; move to `MANAGEBAC_*` when convenient.

`MANAGEBAC_PASSWORD` and `MANAGEBAC_COOKIE` are read back as **input** as well
as exported into the daemon child, so a non-interactive run needs no prompt:

```bash
MANAGEBAC_PASSWORD=... tahuti daemon run       # no prompt, secret not in argv
MANAGEBAC_COOKIE=... tahuti list --format json # cookie straight from the env
```

> **A password from the environment is never written to disk.** It is input for
> this run, not a request to store it: only `tahuti login --keep-credentials`
> writes a password, whatever the source. So `MANAGEBAC_PASSWORD=... tahuti list`
> authenticates and leaves no `creds.json` behind — and, by the same token, does
> not leave a renewal path behind either.

An explicit `--password` / `--cookie` takes precedence over the environment, and
an exported-but-empty value is treated as unset. `tahuti daemon start -b` still
copies them into the detached child's environment so the secret never travels in
`argv`. The trade-off: a leaked environment variable is now directly usable as a
credential, and a process's environment is readable by its own user.

---

## MCP Server (AI Coding Assistants)

`tahuti` includes a built-in Model Context Protocol (MCP) server for integration with Claude Desktop, Cursor, Gemini, and other AI agents:

```bash
tahuti-mcp
```

### Example Claude Desktop Configuration
Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "managebac": {
      "command": "tahuti-mcp"
    }
  }
}
```

The MCP server exposes 14 tools: `list_tasks`, `view_task`, `submit_file`, `delete_submission`, `get_teacher_feedback`, `get_notifications`, `mark_notification`, `mark_all_notifications_read`, `get_calendar_events`, `get_ical_feed`, `get_timetable`, `list_classes`, `get_class_grades`, and `count_grade_frequencies`.

---

## Downstream Integrations

`tahuti` intentionally avoids coupling itself to specific push providers, notification line limits, or personal course naming conventions. Instead, downstream consumers subscribe to events and apply customized logic:

- **[Event Stream Specification](docs/events.md)**: Full specification of the event data contract, lifecycle states, and JSON payloads.
- **[Downstream Notifier Guide](docs/downstream-notifier-guide.md)**: Operational guide for deploying `extras/mb-notifier` (Bark push alerts, 3-field / 4-line mobile screen budgeting, course aliases, and sound customization).
- **[HTTP Revalidation Findings](docs/http-revalidation-findings.md)**: Which endpoints answer `If-None-Match` with `304` and which never can, why the server's `ETag` is useless on the Rails HTML pages, and the six nonce classes a client-side content hash has to strip to be stable.

---

## Stability Note

This tool interfaces with ManageBac via automated HTTP requests and HTML parsing. If ManageBac updates its frontend layout, CSS selectors, or internal API structures, scrapers may require updates.

---

## Changelog & Security

Release history follows [Keep a Changelog](https://keepachangelog.com/) in [CHANGELOG.md](CHANGELOG.md). For reporting a vulnerability privately, see [SECURITY.md](SECURITY.md) — it also documents exactly which credentials are stored on disk and in what form.

---

## License

MIT