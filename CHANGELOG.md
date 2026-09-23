# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Dates for `0.3.0` and earlier are derived from git history (those releases were
never tagged); from `0.4.0` on, a date is the date of its `vX.Y.Z` git tag.

## [0.5.0] - 2026-09-23

### Added
- **Official gradebook extraction and composition.** ManageBac exposes server-calculated overall letter grades, scores, category weights, and grading scales in `#sidebar_info` on the class core tasks page. Extracted automatically during crawls without additional network requests.
- **Dedicated `class` and `grades` CLI commands.**
  - `mb class [list]` and `mb grades`: overview scoreboard of all enrolled classes with overall marks, scores, and category completion ratios.
  - `mb class <id|name>` / `mb class view <id|name>`: class metadata, overall grade, category composition breakdown table, and grading scale (strictly omitting task listings to preserve separation of concerns).
  - `mb grades -s <query>`: view class grade composition filtered by subject name.
  - `mb grades --composition` (`-C`): multi-class category weight and mark breakdown.
- **Overall grades in task list headers.** `mb list` headers now display overall grade standing alongside class names (e.g. `=== AP Calculus BC [B (81.67%)] ===`).
- **Enriched MCP server tools.** `list_classes` includes overall marks/scores, and new `get_class_grades` tool provides the canonical gradebook data model.

### Removed
- **Removed `tahuti download` subcommand.** Tasks, attachments, and submissions are accessed directly via `tahuti view` and `tahuti submit`.

## [0.4.3] - 2026-09-19

### Fixed
- **`tahuti view` no longer shows the class's subject line as the task body.**
  ManageBac labels the description `<div class="h4">Description</div>` — a
  *div* whose class is a heading name — so the heading selector, which tested
  `tag.name`, never matched it. The fallback then matched the class hero
  (`f-title__description f-hero__description`), and `view` printed "World
  Languages and Cultures — Chinese Language Arts I" under `[description]`
  while the actual assignment was never shown. It read as plausible, which is
  why it went unnoticed. The label selector now accepts a heading-shaped class,
  and the hero is excluded from the fallback so it fails closed (no description)
  rather than returning the wrong text.

### Added
- **`class_description` is reported separately from `description`.** The class's
  subject line is its own field, printed on its own line next to `class:`; the
  task's body stays under `[description]`. They are different things that
  ManageBac renders on the same page.
- **Task descriptions keep their formatting.** `get_task_detail` now returns
  `description_html` alongside the flattened `description`, and the pretty
  output renders it as ANSI — so a teacher's red "hand this in Monday" line is
  red in the terminal, along with bold, italic and underline. Depth follows the
  terminal (`$COLORTERM`/`$TERM`), and `$NO_COLOR` wins outright; the JSON
  payload stays escape-free so `tahuti view | jq` is unaffected. MCP receives
  the same `description_html` so the importer can clean ManageBac markup into
  `MBEvent` itself rather than a pre-flattened string.

## [0.4.2] - 2026-09-19

**Breaking:** the Python import package is `tahuti`, not `mb_cli`. `pip install
tahuti` followed by `import tahuti` now works; `import mb_cli` and
`python -m mb_cli` no longer do. The CLI command names (`tahuti`, `mb`) and the
`MB_CRAWLER_*` environment variables are unchanged.

### Fixed
- **Ctrl-C no longer prints a traceback.** `KeyboardInterrupt` is a
  `BaseException`, so the `except Exception` clause that converts unexpected
  errors into a machine-readable payload never caught it: a Ctrl-C at the
  `ManageBac password:` prompt unrolled the whole stack onto the terminal for
  what is a deliberate cancel, not a failure. It now exits `130` (128 + SIGINT)
  with no traceback, so a shell can tell a cancellation from a break.
- **A closed pipe is no longer an error.** `tahuti list | head` closes stdout
  early, after which Python reported the dead pipe a second time at interpreter
  shutdown as a confusing error beside output that had already succeeded. It now
  exits `0`.
- **`school_timezone` now actually works.** It never did. `parse_due_date`
  attached the *host's* zone to every naive due date, so `due_dt.tzinfo` was
  never `None` and the scheduler's school-zone branch could never run — the
  feature was written but never wired up. On a daemon in UTC serving a UTC+8
  school, a 23:59 deadline read as 23:59 UTC and every reminder fired about
  16 hours late, silently. `parse_due_date` takes an optional `school_tz`, so
  the school's reading is applied while the input is still a bare wall-clock
  time; an input carrying its own offset keeps it. The suite is now
  host-timezone independent — the full suite passing under UTC, Asia/Shanghai,
  America/New_York, Australia/Sydney and Europe/Berlin — where before, this
  only passed when the host happened to be UTC+8, which is why CI was red on
  every Python version while it looked fine locally in Beijing.
- **Declared `tzdata`.** CPython's `zoneinfo` is only a reader; the IANA
  database comes from the OS or that package, and it is absent from macOS,
  Windows and plenty of Linux containers (GitHub's runners among them). Without
  it `ZoneInfo("Asia/Shanghai")` raised, `resolve_school_timezone` swallowed
  it, and the scheduler fell back to the host clock. The test now asserts the
  database is reachable before asserting on the schedule, so a regression names
  the cause instead of reporting `assert 0 == 1`.

### Changed
- **The Python import package is `tahuti`.** `0.4.1` renamed the distribution,
  the CLI command and the repository but deliberately left the import path as
  `mb_cli`, so `pip install tahuti` followed by `import tahuti` raised
  `ModuleNotFoundError`. The package directory is now `src/tahuti/` and both
  `import tahuti` and `python -m tahuti` work; `python -m mb_cli` and
  `import mb_cli` no longer do.
- **`TAHUTI_FORMAT` replaces `MB_CLI_FORMAT`** as the documented name for the
  output-shape override. The old spelling is still read as a fallback and the
  new one wins when both are set, matching how every `MB_CRAWLER_*` variable
  already behaves — so an existing script does not break on upgrade.
- The daemon's stop-status reason string for a reclaimed PID is now
  `not_tahuti_process`, matching the `_is_tahuti_process` guard it reports on.

- **`tahuti login` asks for the domain, school and email before the password.**
  A fresh device no longer has to know that `--school` and `--domain` exist:
  it is asked, in the order those values are actually used. The domain is
  always confirmed and shows its current value — `Base domain
  [managebac.com]:` — with an empty answer meaning "keep it", so the one choice
  worth seeing is never inherited silently. School and email have no sensible
  default (the school *is* the hostname), so they are asked only when nothing
  is known yet; a configured device therefore sees at most the domain line.
  `--domain` is the override and is never second-guessed.
  Prompting is gated on `login` and on an interactive stdin, so the daemon,
  CI and every other command that shares `_build_client` cannot be stalled
  waiting on a human. `--domain`'s help text now names the real default
  (`managebac.com`) rather than implying `managebac.cn`.

## [0.4.1] - 2026-09-19

Security and parsing fixes from the 2026-09-19 full-codebase review.

Task classification output is unchanged from `0.4.0` — the one deliberate
deviation is latent-only on current data. See *Changed*.

### Security
- **The scraped MNN hub endpoint is validated on every construction site.**
  `get_notification_token()` returns `data-mnn-hub-endpoint` verbatim from
  scraped HTML, and the token is sent as `Authorization: Bearer <jwt>`. Four
  user-facing sites passed that value straight to the hub client — the
  `notifications` command, all three MCP notification tools, and the daemon's
  `MNNHubProvider._ensure_hub` — so a poisoned page, a compromised edge, or a
  TLS-stripping MITM could choose the host that received the JWT, including a
  cleartext `http://` one, and `mark_all_read` would additionally write
  notification state there. All four now go through
  `_validated_hub_endpoint`, which accepts the value only when it is https,
  carries no userinfo or port, and names a known Faria hub.
- **`submit` refuses tahuti's own state files.** Both the `submit_file` MCP tool
  and `tahuti submit` now reject any path resolving inside tahuti's own config
  or response-cache directory — `creds.json`, `session.json`, `config.json`,
  `daemon_state.json`, the task `snapshot.json`, and every cached response —
  following symlinks, so a link pointing at one of them is refused by its
  target. Those files hold the ManageBac password, the session cookie, cached
  grade pages and the MNN-hub JWT, and the upload target is a dropbox a teacher
  reads, so a confused tool call had a direct route to exfiltrating them. Scope
  is deliberately narrow: only tahuti's own directories are checked, and files
  elsewhere on the system are left to their file permissions.

### Fixed
- **The daemon state path no longer freezes `$HOME` at import.**
  `DEFAULT_STATE_PATH` was `config_dir() / "daemon_state.json"` evaluated when
  the module was first imported, so a process whose environment changed wrote
  state to the old directory while every other path followed the new one. It is
  now resolved per access, with `default_state_path()` as the resolver the
  containment check and tests call.
- **`view_task` resolves a bare task id instead of concatenating it onto the
  base URL.** The MCP tool computed the resolved id but then passed the *raw*
  argument to `get_task_detail`, so the documented primary input — a bare
  numeric id like `"1000099"` — matched no task URL pattern and produced
  `https://myschool.managebac.cn1000099`. It now resolves through the local
  snapshot and then a crawl fallback bounded by the previously unused `pages`
  argument, and surfaces a failed detail fetch as an error rather than nesting
  it inside a success envelope.
- **Due dates resolve the timezone offset from the zone's own DST rules.**
  `_school_display_tz` used `time.altzone if time.daylight`, but `time.daylight`
  is nonzero whenever a DST *rule* is defined, not when DST is in effect — so on
  Europe/Berlin a January due date came back UTC+2 instead of UTC+1, misfiling
  winter tasks in `--view overdue` and firing daemon reminders early for the
  whole standard-time season. The offset is now computed for the date being
  parsed, and an explicit `TZ=UTC` is honoured.
- **A negative `--retry` no longer raises
  `TypeError: exceptions must derive from BaseException`.**
  `range(retry + 1)` never ran the loop body, so the retry wrapper re-raised a
  `None` exception object.
- **A link-less calendar event no longer aborts `tahuti calendar`.**
  FullCalendar serializes a missing link as `"url": null`, and `dict.get("url",
  "")` then returned `None`, so `.startswith("/")` raised `AttributeError` for
  the entire listing.

### Changed
- **Nothing in task classification moved.** The submission-signal rewrite and
  the submit-button fix below are both additive at the parse layer, and every
  classifier function was diffed against `0.4.0` over a 301,056-row grid of
  synthetic task dicts (3,010,560 comparisons, ten functions) with zero
  differences, plus a live A/B over 58 real tiles that moved 0 tasks. Anyone
  upgrading should see identical `--view overdue` output.
- **Tasks carry two new fields: `submission_status` and `tile_declared_status`.**
  The tile parser read its "not submitted" badge as a bare word, so a tile could
  report `status="not-submitted"` while the parsed record said nothing about
  submission at all — 47 of 58 live tiles did exactly that, and all 32 the CLI
  calls *todo* were among them. The canonical `SubmissionStatus` token and the
  tile's own declared string are now recorded alongside `status`. No classifier
  reads them yet; they are there so the next change can be measured instead of
  guessed. `get_class_tasks` (the CLI's path) still drops them, so the CLI's task
  objects are unchanged.
- **A submit button is read as a control, not as any anchor saying "submit".**
  The class-page scan accepted any `<a>` or `<button>` anywhere in the card,
  including the card's own title link. A task really named "Submitted reading
  log" was therefore offered an upload it does not have and landed in `overdue`
  instead of `past`, because `has_submit_btn` is the class path's only route to
  PENDING. Heading anchors are excluded, an element no longer inherits the
  wording of what it wraps, and the detail page scans `<main>` rather than the
  whole document. Measured over the 58 live tiles: `has_submit_button` differs
  on 0, so no current task changes — the fix is latent-only, which is what makes
  it safe to land on a frozen classifier.
- **`grade_letter` reports "Not Assessed Yet" on tiles the site marks
  `--not-assessed`.** It previously stayed `None`, so callers could not tell a
  not-yet-assessed task from one whose grade the parser missed. These six tiles
  were already `NOT_ASSESSED` and display identically.
- **BREAKING: `tahuti login` saves the session but no longer saves your password.**
  One boolean, `remember`, used to gate four unrelated things — the `remember_me`
  form field, the response cache, password storage and the session write — so
  the only way to avoid writing a cleartext password was `login --temp`, which
  also threw away the session cookie and the cache. `remember` is split into
  three independent knobs: `remember_me` (what ManageBac is told), `refresh`
  (the cache) and the new `--keep-credentials` (the password). `session.json` is
  now written on every successful login, so you are not asked for your password
  on every command, and `creds.json` is written only when you pass
  `--keep-credentials`. **A script that relied on `login --temp` leaving no file
  behind now finds `session.json`; a script that relied on a bare `login`
  storing the password now has to ask for it.**
- **BREAKING: `login --temp` is gone.** Its promise — "writes nothing to disk" —
  was already false: `_relogin_from_creds` rewrote `session.json` unconditionally
  on the one path that needed no prompt, so a stale cookie plus a saved password
  left the file behind no matter what the caller asked. `--keep-credentials` and
  `--no-remember-me` replace it, each scoped to one decision.
- **BREAKING: the password file is per profile.** `creds.json` is now
  `creds.<profile>.json` for any profile other than `default`, which keeps the
  historical path for the single-profile case. It was written to one global path
  even though profiles already existed, so two accounts could not each keep a
  password and `logout` of one had to guess which file was whose. An install that
  already has a password in the global file keeps working: a profile with no file
  of its own still reads it, and the first `--keep-credentials` for the matching
  account removes the stale copy. `logout` resolves the *active* profile's file;
  `logout --all` clears every profile's plus the legacy one.
- **BREAKING: `MB_CRAWLER_*` is renamed to `MANAGEBAC_*`** for `CONFIG`,
  `SESSION`, `CREDS_PATH`, `KEYCHAIN`, `PASSWORD`, `COOKIE` and `NO_PERM_WARN`,
  including the daemon's secret plumbing. The old names still work as deprecated
  fallbacks, with the new name winning when both are set, so existing shells,
  systemd units and CI jobs keep authenticating; `MB_WEBHOOK_SECRET` is
  unaffected.
- **`login --no-remember-me`** omits `remember_me` from the login POST entirely,
  leaving the cookie's lifetime to ManageBac's default instead of requesting a
  persistent one. A server-side setting only: it changes nothing on disk and
  composes with `--keep-credentials`. It also applies to the unattended renewal
  path, which previously hardcoded `remember_me=1` whatever the caller asked for.
- **`login --keychain` now decides only where a kept password goes.** It no
  longer implies that a password is kept at all — that is `--keep-credentials`'s
  job — so `--keychain` alone cannot silently stop storing anything. Both flags'
  help text states the guarantee they actually make.
- **The response cache follows `--refresh` alone.** It used to hang off the
  credential flags as well, so `--temp` disabled a cache that holds grade pages
  and the hub JWT and a plain `tahuti list` paid for re-crawls it had not asked
  for. `logout` still clears it.

### Security
- **A password supplied through the environment is no longer written to disk.**
  `MANAGEBAC_PASSWORD=... tahuti list` authenticated and then persisted that
  password into `creds.json`, because the code could not tell "input for this
  run" from "store this for me". Only `tahuti login --keep-credentials` writes a
  password now, whatever the source.
- **A dead cookie with no stored password is a clean error.** It used to prompt,
  which hangs a daemon or a CI job with no TTY, and the message named neither the
  profile nor the fix. It now fails with `missing_credentials`, writes nothing,
  and names `tahuti login --keep-credentials`.
- **`tahuti daemon` keeps no credential of its own and says so up front.** The
  daemon deliberately stores no password — a long-lived process must not carry a
  copy of one in its config file — but with no password, no cookie and no stored
  credential it used to work until the cookie expired and then stop with nothing
  in its output saying why. `daemon run` and `daemon start` now print a startup
  warning on stderr naming the command that fixes it. The secret still reaches a
  detached child through its environment, never `argv`, because `ps` would
  otherwise expose it to any local user.
- **`insecure_state_files()` reports a per-profile password file.** It looked
  only at the global `creds.json`, so `creds.<profile>.json` sitting at `0644`
  passed the permission audit silently even though the loose-permission warning
  exists precisely because file modes are the only barrier in front of a
  cleartext password.

### Fixed
- **`_relogin_from_creds` no longer writes `session.json` behind the caller's
  back.** It saved unconditionally, so an unattended renewal overwrote the
  session file even when the caller had asked for nothing to be persisted — and a
  failed re-login left it half-updated. Persistence now has exactly one owner,
  `auth.build_client` (plus `auth.refresh_session` for the daemon's renewal
  entry point), and a renewal that fails leaves the previous session file
  untouched.

## [0.4.0] - 2026-09-19

Contains a **breaking webhook signature change** — see *Changed* below. Any
deployed receiver rejects every payload until it adds `X-MB-Timestamp` to its
signed material.

> Renamed from `mb-cli` to **`tahuti`**. The distribution, the CLI command and
> the repository are all `tahuti` now; the Python import path stays `mb_cli`, so
> `import mb_cli` and `python -m mb_cli` are unchanged. Environment variables
> stay `MB_CRAWLER_*` and the state directory stays `~/.config/tahuti/`.
> (`mb-cli`/`mb_cli` on PyPI is an unrelated project by another author, so
> nothing here overwrites it.)


### Added
- **Per-endpoint delivery outcomes.** `WebhookDispatcher.dispatch()` returns a
  machine-readable `outcome` per endpoint — `success`, `permanent_failure` or
  `transient_failure` — alongside the existing `success` boolean, plus
  `retryable`, `signed`, `attempts` and `url_display` fields. Previously the
  only signal was a bare boolean, which collapsed "delivered", "will never
  work" and "try again later" into one value.
- `WebhookDispatcher.retry_failed(event, results)` re-attempts only the
  endpoints that still owe the event, skipping the ones that already delivered
  and the ones that failed permanently. Module-level `retryable_results()`
  (what is still owed) and `all_delivered()` (is anything owed) give a caller
  everything needed to implement per-endpoint at-least-once.
- `daemon test-webhook` output now carries `url_display`, `outcome`,
  `retryable`, `signed` and `attempts`.

### Fixed
- **A webhook with no secret no longer ships unsigned payloads silently.**
  `if webhook.secret:` treated `""` as "no signing", so an empty secret sent
  unsigned payloads with no warning anywhere. The dispatcher now logs an ERROR
  once per endpoint explaining that every payload is UNSIGNED and that a
  verifying receiver will reject it, and every result carries `signed: false`.
- **`daemon test-webhook` now validates the URL.** `test_ping` bypassed
  `_validate_webhook_url`, so the scheme/host guard applied on every real
  dispatch was skipped on the one command where a user is most likely to paste
  a wrong URL — `file://` and friends reached `requests` and surfaced as a
  confusing network error. An invalid URL is now rejected before any network
  call with `invalid_webhook_url:<reason>`.
- **Webhook URLs are no longer logged verbatim.** `log.info`/`log.warning`/
  `log.error` on every delivery and retry printed the full configured URL, so
  providers that carry the credential in the path or query (Slack
  `hooks.slack.com/services/T…/B…/<token>`, Bark `api.day.app/<key>/…`, WeCom
  `…/send?key=…`) wrote a live token into `daemon.log`. URLs are now redacted
  before logging — scheme, host, port and path shape are kept, credential-
  bearing path segments and query values are masked, and any `user:password@`
  userinfo is dropped. `daemon test-webhook` output additionally carries a
  `url_display` field.
- **The webhook retry "hard ceiling" now bounds request time, not just
  sleeps.** `MAX_TOTAL_RETRY_SECONDS` was checked only before `time.sleep`, so
  each attempt's `requests.post(timeout=10)` was unbounded by it and the final
  attempt always ran its full timeout. With the default `max_retries=3` one
  hanging endpoint cost ~33s of blocked polling, and the cost was linear in the
  number of configured endpoints (2 endpoints ≈ 66s against a 30s poll
  interval). Each attempt's timeout is now clamped to the remaining budget and
  the backoff sleep is clamped to it too, so one event can never exceed the
  ceiling.
- **Permanent 4xx are no longer retried.** 400/401/403/404/410/422 were retried
  three times with backoff even though no backoff can fix them, which both
  stalled the poll loop and delayed the diagnosis. Only genuinely transient
  statuses are retried now: 5xx, 408, 429 (and 425).
- **Webhook redirects are no longer followed.** `requests.post` followed
  redirects with the signed body and the signature headers intact, so a 307
  re-sent them to a *different* host and a 301/302 could downgrade https to
  http — defeating the point of signing. Dispatch now passes
  `allow_redirects=False`; a 3xx is reported as a permanent failure naming the
  `Location` so the configured URL can be corrected.

### Changed
- **BREAKING PROTOCOL CHANGE — webhook signatures.** `X-MB-Signature` now covers
  `X-MB-Timestamp` as well as the body:
  `sha256=` + HMAC-SHA256(secret, `f"{X-MB-Timestamp}.".encode() + body`). It
  previously covered the body alone, which left `X-MB-Timestamp`
  unauthenticated — anyone who captured a single POST could replay it
  indefinitely by rewriting that header, because the original digest still
  validated and the receiver's freshness check (`MAX_TIMESTAMP_SKEW_SECONDS`)
  waved the replay through. **Any deployed receiver rejects every payload until
  it adds the timestamp to its signed material.** `extras/mb-notifier/bark_webhook_receiver.py`
  and the FastAPI recipe in `docs/events.md` are updated in the same change;
  `verify_signature` now fails closed on a missing `X-MB-Timestamp` and checks
  the digest before freshness, so a restamped payload reports
  `signature_mismatch` rather than merely `stale_timestamp`.
- `CHANGELOG.md`, `SECURITY.md`, and GitHub Actions CI (`.github/workflows/ci.yml`).
- `[dependency-groups]` `dev` group in `pyproject.toml` declaring the test
  dependencies (`pytest`, `requests-mock`, `mcp`) that the suite always needed
  but that were never declared, so a plain `uv sync` could not run the tests.
- **Piped output is now the documented JSON.** `resolve_format` returned `pretty`
  for any unset `--format`, with no TTY check, so `mb list | jq .` failed with a
  parse error and every JSON consumer had to remember `--format json`. It now
  picks `pretty` for an interactive terminal and `json` otherwise (matching the
  `--format` help text and the README), with `MB_CLI_FORMAT=json|pretty` as an
  escape hatch for scripts that run with and without a terminal.
- **MCP `list_tasks` no longer answers "no homework" for a misspelled `view`.**
  An unrecognised value matched none of the three section checks, so all three
  lists stayed empty and the tool returned `total_count: 0` — a valid-looking
  wrong answer. `view` is now validated against one canonical vocabulary
  (case-insensitive, with aliases like `Upcoming` / `upcoming tasks`) shared with
  `filters.result_views`, and an unknown value returns a structured error instead
  of crawling anything.
- **`--grade` / `grade=` now match `+`/`-` modifiers.** The extraction regex
  `^([A-F][+-]?)\b` could never capture the modifier — there is no word boundary
  between `+`/`-` and a following space, so it always backtracked to empty and
  `"A+ (95/100)"` was read as `"A"`. A task whose card carries only
  `grade_score: "A+ (95/100)"` was therefore invisible to `--grade A+`.
- **Mixed naive/aware due dates can no longer crash the pretty renderer.**
  `parse_due_date` returns an aware datetime for ISO input with an offset and a
  naive one for every HTML format; sorting a section that contained both raised
  `TypeError: can't compare offset-naive and offset-aware datetimes` out of
  `render_pretty`, which `main()` does not catch — the user got a traceback and
  no payload. Both `task_sort_key` and `classify_task_view` now normalise through
  one shared helper.
- **MCP tools validate their inputs.** `view_task` derived the task id with
  `target.split("core_tasks/")[-1].split("/")[0]`, so a URL without
  `/core_tasks/` made the entire string the id; `get_class_grades` interpolated
  `class_id` straight into a ManageBac URL path; `get_calendar_events` /
  `get_timetable` passed arbitrary strings into query params; and `submit_file`
  handed an unchecked path to the filesystem. Each now returns a structured,
  actionable error for a malformed argument instead of a 404 or a raw
  `FileNotFoundError`.
- **Packaging:** the sdist no longer leaks a nested copy of the repository. It
  previously shipped 76 entries under `.claude/`, including
  `.claude/worktrees/finish-security-audit/` — a complete clone of the repo with
  its own `.git` (~75 MB). Fixed in both belts: `.claude/` added to
  `.gitignore`, and an explicit `[tool.hatch.build.targets.sdist]` include/exclude
  list in `pyproject.toml` that pins exactly what ships. The sdist went from 153
  entries to 63 files; no `.pyc`, no `.git`, no `.venv`, and no
  `docs/superpowers/` internal design docs ship.
- `mb --config` / `--session-file` help text no longer claims the format is
  TOML; it is JSON (`config.json` / `session.json`).
- `docs/library.md` no longer points at a non-existent
  `~/.config/mb-crawler/config.toml`.
- **The `mcp` extra is now bounded (`mcp>=1.20,<2`).** It was unbounded, so a
  fresh resolve installed mcp 2.x, which removed `mcp.server.fastmcp` — every
  `mb-mcp` invocation died with `ModuleNotFoundError` on import, even when the
  extra was installed correctly.
- README Installation now documents the `mcp` extra
  (`pip install "mb-cli[mcp]"`) — the documented `mb-mcp` command imports `mcp`
  unguarded and failed with `ModuleNotFoundError` on a plain install.
- README now documents `mb daemon start -b` / `--background`. `daemon start`
  runs in the **foreground** by default and dies with the terminal; only `-b`
  detaches it. `--pid-file` / `--log-file` documented per subcommand.
- README now documents the previously undocumented `mb submissions`,
  `mb download`, `mb feedback`, and `mb daemon install` / `uninstall` /
  `configure-channel` subcommands.
- README now documents the `MB_CRAWLER_PASSWORD` and `MB_CRAWLER_COOKIE`
  environment variables alongside `MB_WEBHOOK_SECRET`.
- README now flags the `--poll-interval` (`mb daemon run`) vs `--interval`
  (`mb daemon start`) flag-name asymmetry, and qualifies the claim that
  `--config` / `--session-file` are universal — the daemon subcommands expose
  only `--daemon-config` and/or `--pid-file` / `--log-file`, varying by
  subcommand.

## [0.3.0] - 2026-09-17

### Added
- Complete library + downstream security audit.
- Comprehensive Python SDK and library reference (`docs/library.md`).

### Changed
- Bumped version to 0.3.0 and documented remote separated deployment.
- `ManageBacClient.from_config()` implemented; `docs/events.md` updated.

### Fixed
- CLI command flags, the systemd unit, and Python SDK snippets corrected in the
  README and the notifier guide.
- `student_name` fallback polished and covered by a test for the default
  `from_config` argument.

## [0.2.5] - 2026-09-14

### Added
- Real-time event streaming: `ManageBacDaemon.stream()` async event generator.
- Comprehensive event stream specification and integration guide (`docs/events.md`).
- Design specs and implementation plan for modular event stream and notifier
  separation.

### Changed
- Standardized the `MBEvent` payload schema.
- Extracted the Bark webhook receiver and personal config into
  `extras/mb-notifier`.
- Refreshed the README and added the downstream notifier guide.

### Fixed
- Stream lifecycle and queue-safety issues raised in review.
- Defensive fallback URL resolution in `service.py`.

## [0.2.4] - 2026-09-13

### Added
- `mb submissions` CLI for submission lifecycle management (`--list`, `--add`,
  `--delete`, `--check-feedback`), with a matching MCP tool.

### Changed
- Bumped version; grade-release alert suppression and webhook improvements.

## [0.2.3] - 2026-09-05

### Added
- Unified task status and lifecycle domain model.
- Teacher feedback fetching, including support for modern task detail page
  submissions and the PSPDFKit preview modal.
- `on_start` lifecycle callback for a full upcoming-task refresh.
- Just-in-time submission verification, eliminating unnecessary full recrawls.
- Lightweight Bark webhook receiver adapter.
- Real-time notification daemon and webhook engine with the MNN Hub provider and
  a DDL scheduler, plus a complete E2E integration test suite.
- Compact 3-field notification layout with exact course alias resolution.
- Design specs for the daemon, the compact Bark layout, the on-start refresh
  callback, unified task status, and submissions lifecycle management.

### Fixed
- Daemon no longer notifies for submitted or graded tasks.
- `_check_is_task_submitted` now checks the task page Submitted badge.
- Past milestones suppressed on task discovery; DDL reminders formatted with the
  exact remaining time.
- `new_task` event mapped; class and task names cleaned; richer 4-line Bark
  notification.
- Bark notification format enforced as a compact 4-line layout bounded to the
  date line width.
- `run_forever` alias restored on `DaemonService`.
- Resolved code review issues in the daemon.

### Changed
- Deduplicated task URL parsing (`parse_task_url`), task classification logic
  across main/client/formatters, snapshot IO and diffing in the daemon package,
  and submission/completion/classification logic in `filters.py`; the stealth
  crawler now reuses `client.get_submissions`.
- `.worktrees/` ignored; `course_aliases.json` ignore scoped to root level.

## [0.2.2] - 2026-06-13

### Added
- Auth health check and silent re-login on an expired cookie.
- `load_creds()` for external credential files.

### Fixed
- Cache namespaced by email hash; `list_tasks` crawl optimized.
- `submit_file` task ID resolution optimized.
- Cache no longer sleeps on cache hits inside crawler loops.
- Hub jitter adjusted to 1–3s for reads and removed from mutations.

## [0.2.0] - 2026-04-30

### Added
- Stealth daemon with active windows and index-only diffing.
- Daemon active hours and randomized polling interval.
- `channel_send` delivery mode.
- Remember-me login support (30-day sessions by default).
- Referer header and random jitter between requests.

### Fixed
- Pagination fixed; retry with backoff and grade frequency counting added.

## [0.1.0] - 2026-04-29

### Added
- Initial release: `mb` CLI, `ManageBacClient` SDK, disk-based response cache
  with configurable TTL.
- Status and grade filters, auto-aggregated grades, task grade parsing.
- Pretty-printed task listing table with CJK/double-width alignment padding.
- `mb list --tag / -t` filtering and tag filtering in the MCP `list_tasks` tool.
- `mb list --deleted` to include tasks deleted from the server; instant
  snapshot caching; grade-status rendering (Complete / Incomplete) and a
  combined letter + score grade column.
- Pretty table formatter for `count-grade-freq`; attachment downloads;
  student name captured and updated on dashboard loads.
- Legal disclaimer regarding the Faria/ManageBac terms of service.
- Project renamed from `mb-crawler` to `mb-cli` (command: `mb`).

[Unreleased]: https://github.com/allen/mb-crawler/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/allen/mb-crawler/compare/v0.2.5...v0.3.0
[0.2.5]: https://github.com/allen/mb-crawler/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/allen/mb-crawler/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/allen/mb-crawler/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/allen/mb-crawler/compare/v0.2.0...v0.2.2
[0.2.0]: https://github.com/allen/mb-crawler/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/allen/mb-crawler/releases/tag/v0.1.0
