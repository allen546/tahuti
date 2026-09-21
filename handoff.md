# Handoff: tahuti

Written 2026-09-21. This is for someone who has never opened this repository
before. It explains what the project is, what was wrong with it, what got fixed,
what was deliberately left alone, and where everything physically lives on disk.

If you read only one section, read **§7 Still open**.

---

## 1. What this project is

**tahuti** is a Python toolkit for **ManageBac**, a web platform that schools use
to publish assignments, grades, timetables and announcements to students.
ManageBac is a commercial product by Faria Education Group; it has no public API
for students. So tahuti works by signing in as you and reading the HTML pages you
would read, plus a couple of JSON endpoints the web app itself uses.

That makes it a scraper, and that is worth saying plainly because it shapes
everything else: **every defect in this repo is a defect about parsing a web page
that somebody else can change without telling us.** There is no contract to
depend on.

The toolkit ships four things, all in `src/tahuti/`:

| Piece | What it is | Entry point |
|---|---|---|
| **SDK** | `ManageBacClient` — typed methods that fetch and parse pages | `client.py` |
| **CLI** | `tahuti list`, `tahuti grades`, `tahuti submit`, … | `__main__.py` |
| **MCP server** | The same data exposed as 14 tools for AI assistants (Claude Desktop, Cursor) | `mcp_server.py` |
| **Daemon** | Polls ManageBac on a timer and emits events / fires webhooks | `daemon/__init__.py` |

There are two **origins** — `managebac.com` (international) and `managebac.cn`
(China) — and the school is a subdomain of one of them, e.g.
`beijing101.managebac.cn`. Both are supported.

### The one idea you need before reading any code

**The CLI is authoritative. The MCP server is a wrapper around it.**

Both surfaces answer the same questions ("what are my assignments?", "what are my
grades?"). They are supposed to agree. The most important class of bug in this
repo is when they stop agreeing — because the person asking an AI assistant
"did I submit the kinematics worksheet?" and the person typing
`tahuti list` in a terminal should get the same answer. Almost everything in
§3 is a variation on that theme.

---

## 2. Where everything lives on disk

The repository's **main checkout** is:

```
/Users/allen/Desktop/t8/mb-crawler
```

It is currently on branch `fix/mcp-divergences-efficiency`.

Alongside it are **git worktrees** — separate working directories sharing one
object store, so you can have several branches checked out at once without
`git stash` games. Here is the complete map:

### Worktrees that exist right now

| Path on disk | Branch | State |
|---|---|---|
| `/Users/allen/Desktop/t8/mb-crawler` | `fix/mcp-divergences-efficiency` | **the working branch**, 1596 tests pass |
| `/Users/allen/Desktop/t8/mb-crawler-wt-handoff` | `docs/handoff` | this document; also holds a trial merge of all three efficiency branches (**1605 tests pass**) |
| `/Users/allen/Desktop/t8/mb-crawler-wt-submit-local` | `fix/efficiency-submit-local` | verified, not merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/auth-head-simplify` | `fix/auth-head-simplify` | merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/cache-per-entry-ttl` | `fix/cache-per-entry-ttl` | merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/dead-config-and-paths` | `fix/dead-config-and-paths` | merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/efficiency-delete-soups` | `fix/efficiency-delete-soups` | verified, not merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/fix-efficiency-notifications` | `fix/efficiency-notifications` | verified, not merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/fix-test-hygiene` | `fix/test-hygiene` | merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/mcp-filter-ladder` | `fix/mcp-filter-ladder` | merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/mcp-share-cli` | `fix/mcp-share-cli-resolution` | merged |
| `/Users/allen/Desktop/t8/mb-crawler/.claude/worktrees/probe-evidence-integrity` | `fix/probe-evidence-integrity` | merged |

(`git worktree list` prints this same table. Note the two naming conventions —
some worktrees are prefixed `fix-`, some are not. It is cosmetic, but it will
confuse you once.)

### Branches with no worktree of their own

These exist only as refs; check one out in the main checkout or make a worktree:

| Branch | State |
|---|---|
| `main` | released 0.4.3, 34 h old |
| `fix/mcp-cli-divergences` | merged into the working branch |
| `fix/roster-single-source` | merged into the working branch |
| `trial5` | **scratch, from a merge experiment. Safe to delete.** |

### Remotes

Two, and they are not equivalent:

- `origin` → `git@github.com:allen546/tahuti.git`
- `pi` → `ssh://allen@100.95.33.78:22/mnt/pi-data/tahuti` (a NAS on the tailnet)

**Nothing has been pushed.** The plan is a PR from `fix/<summary>` to
`origin/main` once the remaining decisions in §7 are made.

### The virtualenv trap

`/Users/allen/Desktop/t8/mb-crawler/.venv` is an **editable** install pointing at
the main checkout's `src/`. It is gitignored, so a fresh worktree has no `.venv`.

**This means running pytest inside a worktree silently tests the main checkout's
source.** You will get green results that have nothing to do with the code in
front of you. Always do:

```bash
cd <worktree>
PYTHONPATH=$PWD/src /Users/allen/Desktop/t8/mb-crawler/.venv/bin/python -m pytest -q
```

and assert the resolution before trusting it:

```bash
PYTHONPATH=$PWD/src .venv/bin/python -c "import tahuti; print(tahuti.__file__)"
# must start with the worktree path
```

This has already produced one wrong conclusion in this project's history.

---

## 3. What was wrong, and got fixed

Everything in this section is on `fix/mcp-divergences-efficiency` and tested.
The commit subjects tell most of the story; this is what they mean.

### 3.1 The two divergences that started it all

**`list_tasks` was blind to whole classes.** The MCP tool got its tasks from
`get_tasks_by_view` (the "tasks and deadlines" pages). The CLI's `list` got its
tasks from `crawl_all` (dashboard → every class's own task page). Those are two
overlapping sources, and a class whose tasks only appear on its own page was
**missing from the MCP output entirely**. Asking an AI assistant "what's due?"
could omit real coursework.

Fixed by making the MCP tool call `crawl_all`. The `view` argument stopped being
"which page to crawl" and became "which slice of the result to show" — which is
what the CLI's `--view` already meant.

**`list_classes` forgot empty classes.** It derived the class roster from task
links found inside `crawl_all`'s output. An empty class contributes no task link,
so it contributed no row. The consequence was a contradiction you could trigger
in two calls: `list_classes` would *not* list a class, and then
`get_class_grades(class_name="…")` would report it as not found — while the CLI
listed it fine.

Fixed by taking the roster from `client.get_classes()` (the dashboard scrape that
`crawl_all` itself already performs to find classes).

Both fixes live in commit `540a560`. Note the deliberate scope limit recorded in
its message: **`tahuti grades` still does the task-link reconstruction locally,
so the CLI still misses classes with no tasks.** That asymmetry was left in place
on purpose at the time and is now closed by `fix/roster-single-source` (below).

### 3.2 Other correctness fixes on the working branch

- **A null `cache_ttl` crashed the cache** (`b0d9cd8`). `config` coerces the
  value, but a caller constructing `ResponseCache` directly bypasses that layer,
  so `__init__` now clamps it too.
- **The session health check cost 275 KB** (`b0d9cd8`). It was fetching a whole
  dashboard page just to answer "is this cookie still alive?".
- **The webcal token got its own TTL instead of its own cache**
  (`3848b19`). The 36-character token that unlocks the calendar feed outlives the
  171 KB page it is scraped from, so recording a per-entry TTL on the one cache
  beats maintaining a second cache object that has to be re-synced every time
  `client.cache` is replaced.
- **The containment rule and the MCP read could disagree about the snapshot
  filename** (`1e1de8d`). `config.snapshot_path` is now the single source.
- **`WITHOUT_F` was a two-class recipe** (`3b30c48`), so the probe's
  class-(f) evidence was never actually measured. This is the kind of bug that
  only exists because a *test* was lying — see §6.
- **The health check's two attempts got unrolled** (`48e9962`) into a plain
  "try HEAD, then fall through to GET", which is what it always did, minus the
  loop that made it look like something more general.
- **MCP `list_tasks` now filters through the CLI's helpers** (`347476d`) rather
  than keeping a private copy of the filter ladder.
- **One task-resolution ladder and one task-id rule, shared with the CLI**
  (`4b946c4`). Two places had grown their own.
- **The last two roster derivations come from the dashboard** (`842a21f`). This
  closes the `grades`/`count-grade-freq` asymmetry from §3.1 — but note it
  **changes output**: a class with no tasks now appears in `grades` where it
  previously did not.

### 3.3 Test hygiene

`3e6574b` removed one dead helper and one vacuous test, and factored six copies
of one setup. `67ea1bc` moved `verify_snapshot_paths.py` into `tests/` so
`testpaths = ["tests"]` actually collects it — it had been sitting in the repo
root, invisible to pytest, which is how a verification script ends up never
running.

---

## 4. What the investigation found (and did not change)

The substantial piece of research here is **HTTP caching**: can we ask ManageBac
"has this page changed?" and get a cheap `304 Not Modified` instead of 171 KB of
HTML?

Full write-up in `docs/http-revalidation-findings.md`, produced by the read-only
probe `extras/probe_revalidation.py`. The short version:

### The server's `ETag` is structurally useless on every HTML page

ManageBac *does* answer `If-None-Match` — on `events.json`, on the `.ics`
calendar feed, and on the MNN notification hub. It does **not** answer it on a
single Rails-rendered HTML page, and the reason is not configuration:

> The ETag is `sha256(body)[:32]` — a digest of the very body that changes on
> every render.

The page embeds at least six classes of per-render nonce:

- **(a–d)** New Relic browser-agent parameters
- **(b)** an autologout countdown clock
- **(c)** a CSRF token
- **(d)** the MNN-hub JWT
- **(e)** chart UUIDs
- **(f)** AWS S3 signed URLs

Every one of those rotates on render, so the body always differs, so the digest
always differs, so `If-None-Match` never matches. **This cannot be worked around
from the client.** It is not a header we are sending wrong.

There is also a completeness result worth keeping: normalising all six nonce
classes out of four pages and hashing gives an **identical** hash across all
three fetches of every page. That means no byte outside a transformed span varies
— so there is **no seventh nonce class** at the observed timescale. It does not
rule out something on a longer cadence (per-session, per-hour, per-day); the
probe says so explicitly rather than overclaiming.

### Auth always precedes the validator

A conditional request with a live credential gets a `304`; with a dead one it
gets `403`/`401` — never a `304` that would let a stale page be served. So the
cache can never mask an expired login. (Corroborated by `Cache-Control` flipping
to `no-cache`, the ETag going absent, and `X-Runtime` dropping.)

### Two things the cache *could* do that it currently does not

- **`events.json` and the `.ics` feed both answer `If-None-Match`** and are
  currently fetched cold. This is real, measured, unexploited.
- **The hub's Etag cache is per-process.** `_fetch_notifications` builds a fresh
  `MNNHubClient` per call, so its in-memory Etag dict dies with the process and
  both hub GETs re-fire on every warm run.

### A latent cache-key defect (found, not fixed)

`ResponseCache` keys on `sha256(url)` and **ignores request headers**. But
`/dropbox` content-negotiates on `X-Requested-With`. Today no code path sends
that header, so it is latent rather than live — but the day someone adds it, two
different documents will share a cache entry. Worth knowing before you touch the
cache.

---

## 5. The efficiency work

The goal was to cut HTTP requests per command. Requests here are expensive: the
CLI's default `request_delay` is 1.0 s, so a 9-request command costs at least
9 seconds of deliberate pacing on top of network time.

### How it was measured

Not by reading code — by counting. A `requests_mock` layer intercepting
`requests` transport, with `HOME` redirected to a throwaway `mktemp -d` **before
Python starts** (so module-level constants like `cache.DEFAULT_CACHE_DIR`
resolve into the sandbox and not into your real `~/.config/tahuti`), and the
on-disk response cache deleted between cold runs. **No live traffic, no
passwords, no real state touched.**

53 command forms were measured, cold and warm. Worst offenders:

| Command | Requests cold | For |
|---|---|---|
| MCP `get_teacher_feedback(bare id)` | 9 | 1 feedback item |
| `submit <bare id>`, empty snapshot | 9 | 1 upload |
| `submissions --delete` | 7 | 1 deletion, **5 of them redundant** |
| `grades --subject Math` | 6 | 1 class report |
| `list` | 6 | 4 tasks, **3 pure waste** |

### Three structural defects behind that ranking

1. **`crawl_all()` unconditionally fetches the notification block** — one page
   plus two MNN-hub calls — and **all six of its call sites throw the result
   away.** That is 3 wasted requests, roughly half of a cold `list`, and the only
   thing a *warm* `list`/`grades`/`count-grade-freq` still pays.
2. **`delete_submission()` fetches the task page three times and the dropbox page
   three times, every one `bypass_cache=True`.** No cache layer can collapse
   them, which is why warm costs exactly what cold costs.
3. **The session health check did an uncached dashboard GET for a boolean**, then
   `get_classes()` fetched the same URL again through the cache. *(Now fixed —
   it is a HEAD.)*

### What landed

| # | Change | Effect | Where |
|---|---|---|---|
| 1 | `fetch_notifications=False` kwarg on `crawl_all` | `list` 6→3, `grades` 6→3, warm 2→0 | `fix/efficiency-notifications` |
| 2 | `get_submissions(task_soup=, dropbox_soup=)` | `--delete` 7→5 cold **and** warm | `fix/efficiency-delete-soups` |
| 3 | roster from `get_classes()` | `grades` 6→3 | already in via `fix/roster-single-source` |
| 4 | local snapshot write in `cmd_submit` | `submit` 3→2 | `fix/efficiency-submit-local` |
| 5 | reorder `_resolve_task_ids` | 0–1 request | **deliberately not done** — see §7 |

All three unmerged branches were merged together in
`/Users/allen/Desktop/t8/mb-crawler-wt-handoff` as a trial: **clean merge, 1605
tests pass.** So the remaining work on the efficiency side is essentially
bookkeeping, not conflict resolution.

### Why the rest of the proposals were rejected

37 proposals were written and killed by adversarial verification, each on one of
four grounds: the arithmetic does not actually save anything, it breaks
correctness, it is unsafe, or it was already done. Two rejections are worth
knowing about because they are counter-intuitive:

- **The whole parallel-fan-out family died on a measurement nobody had made.**
  `_respect_rate_limit` is an unlocked read-modify-write, and it was measured
  passing **3.79 req/s at K=4 and 6.85 req/s at K=8 against a configured
  1 req/s**. The shipped limiter does not cap anything, the baseline burst rate
  is unknown, and every fan-out headline figure had been taken at
  `request_delay=0.0` — a value no user can set. So there is no safe headroom to
  parallelise into.
- **Caching the MNN-hub JWT was rejected on credential lifetime**, and the reason
  is that *nobody has read the token's `exp` claim*. That single offline read
  would settle it. See §7.

---

## 6. Things that will bite you

These are process lessons, not code facts. They are the reason several earlier
conclusions in this project were wrong.

1. **Any call to `build_client()` writes `~/.config/tahuti/session.json`** — even
   with a mocked login. A probe that only wanted to *read* a page destroyed a live
   session this way once already. Isolate `$HOME` before constructing a client,
   always.
2. **A stale `.pyc` reports a defect that does not exist.** Clear
   `__pycache__` before concluding anything about a file a sibling process is
   editing.
3. **Running pytest in a worktree without `PYTHONPATH=$PWD/src` tests the wrong
   tree** (§2). It fails silently and looks like the branch is broken.
4. **A green test can be lying.** `WITHOUT_F` was a two-class recipe while its
   name and its output claimed five — so the class-(f) evidence was never
   measured, and the probe's conclusion looked solid for weeks.
5. **When a check fails, check the check.** Print the raw value, not a summary of
   it.
6. **Never read `~/.config/tahuti/session.json` or `config.json`.** They hold
   cookies and a Bearer JWT. Both are gitignored, and both should stay that way.
   If you need to know whether a session works, run `tahuti list` and read its
   exit code.

---

## 7. Still open

Ordered by how likely it is to matter.

### 7.1 `fix/efficiency-notifications` silently changes `student_name` — **proven, unpinned**

`_capture_student_name` runs on *every* `_get` of HTML (`client.py:912, 935, 943,
1132`). The notifications page goes through `_get`. So on a school whose
**dashboard** lacks `a[href="/student/profile"]` but whose **notifications page**
has one, the notifications fetch was the only thing populating `student_name`.

Skipping it (which is what the branch does) leaves the field `None`. Verified
directly, not inferred:

```
after dashboard only          -> student_name = None
after the notifications page  -> student_name = 'Alice Zhang'
```

Nobody has measured how common that page shape is. **The probe that settles it is
one GET of `/student/dashboard` per school, grepped for the anchor.** If the
anchor is universal, this is a footnote; if any school lacks it, either capture
the name from the dashboard explicitly or keep the page GET and gate only the two
hub calls.

The branch shipped with neither a test nor a comment pinning this. **Add one
before merging.**

### 7.2 `submit` no longer refreshes grades at submit time — **needs a decision**

`fix/efficiency-submit-local` replaces the eager "re-fetch the whole class grade
page" with a local row write when the snapshot already has the row. That saves a
request per submit, and `status` / `has_submit_button` are written correctly and
immediately. But three things change, and they were measured:

1. **`grade_letter` is no longer refreshed** — it stays at its pre-upload value
   until the next crawl.
2. **Sibling rows in that class are no longer refreshed.**
3. **`has_submit_button` can genuinely disagree with `status="submitted"`** for
   cards ManageBac still hands a dropbox link to.

This violates the constraint the branch was written under ("no command's output
may change"). Either accept the trade and pin it with a test that says so out
loud, or revert the branch. **This is a product decision, not a technical one.**

Related and left out of scope: the MCP `submit_file` carries the same duplicated
eager-refetch block, so the two surfaces now differ on this behaviour.

### 7.3 `get_classes()` silently drops real classes — **latent, easy fix**

```python
if name and not any(kw in name.lower() for kw in ("all classes", "browse", "view")):
```

This is meant to skip navigation chrome like "View all classes" and "Browse". It
also skips any class legitimately named **"Worldview"**, **"Media Review"**,
**"Advanced View"**, or anything else containing those substrings. The class
vanishes from the roster with no error.

A class whose name contains "view" is not exotic. The fix is to filter on
something structural (the link's position in the nav, or an exact match against
known chrome strings) rather than a substring match on the name.

### 7.4 The probe count in the findings doc is wrong — **documentation**

`docs/http-revalidation-findings.md:36` claims the probe issued
**"8 conditional GETs, 0 × 304"**. The shipped probe issues **three** (events.json,
the `.ics` feed, the calendar page — lines 200–233). The 3-fetch-per-page study in
`q2_q3_hash` is a different thing and is not a conditional GET. Fix the number.

### 7.5 Nobody has read the MNN-hub JWT's `exp` claim — **one command**

This gates the entire credential-caching family (three rejected proposals). The
probe is offline and instant: scrape a token, read `exp`, done. Until it exists,
the token TTL must equal the page TTL, which is the configuration that saves
nothing. `docs/http-revalidation-findings.md` currently classifies `data-token`
as a per-render nonce, which argues the lifetime is short — but that is an
inference, not a measurement.

### 7.6 The rate limiter does not limit — **blocks a whole family**

`_respect_rate_limit` is an unlocked read-modify-write. Measured: 3.79 req/s at
K=4 and 6.85 req/s at K=8 against a configured 1 req/s, with a minimum
inter-request gap of 0.000 s. Until the real send rate is known and the limiter
actually holds, every parallelisation proposal is unsafe to evaluate.

### 7.7 Repo hygiene

- **A stray CLI error dump is committed** as the tracked file `json` (8 lines of
  `{"ok": false, "command": "view", …}`). It came in with the
  `mcp-share-cli-resolution` merge. It should not be in the repository.
- **Five stray test artifacts sit next to the repo**, not in it, so `git status`
  never sees them:
  ```
  /Users/allen/Desktop/t8/mb-crawler-test-config.json
  /Users/allen/Desktop/t8/mb-crawler-test-config.toml
  /Users/allen/Desktop/t8/mb-crawler-test-daemon.json
  /Users/allen/Desktop/t8/mb-crawler-test-session.json
  /Users/allen/Desktop/t8/mb-crawler-test-session.toml
  ```
  Nothing in the current tree creates them — they are from an older test that
  wrote beside the repo instead of into a tmpdir. Untracked and safe to delete.
- **`trial5` is a scratch branch** from a merge experiment. Safe to delete.

### 7.8 The local session is currently broken

`~/.config/tahuti/session.json` is fresh (written 07:33 on 2026-09-21) but its
school is `myschool` — a documentation placeholder, not a real subdomain.
`tahuti list`, `tahuti notifications` and `tahuti grades` all exit 1 with a 404
on `/student/dashboard` itself. Either a test wrote a fixture session into the
real config directory, or `myschool` was typed at the login prompt.

**Nothing was purged.** The remedy used previously is to delete both
`~/.config/tahuti/` and the pre-rename `~/.config/mb-crawler/`, then log in
fresh with the real school subdomain.

---

## 8. How to pick this up

1. **Read `docs/http-revalidation-findings.md`.** It is the intellectual core of
   the recent work and explains why the caching layer looks the way it does.
2. **Get a working session** (§7.8). Without it nothing can be verified against a
   live server, and every fixture-based claim stays fixture-based.
3. **Settle §7.1 and §7.2.** Both are one small change each; both currently ship
   with a behaviour change that nothing documents.
4. **Merge the three efficiency branches.** Already trial-merged clean at 1605
   passing in `mb-crawler-wt-handoff`.
5. **Run the code review for inelegant code** on the merged result — PEP8,
   elegance traded for negligible performance, duplication at the wrong altitude.
   That round was planned and has not happened.
6. **Then** create `fix/<summary>`, merge, and open the PR to `origin/main`.

---

## Appendix: test counts

| Branch / state | Result |
|---|---|
| `main` | baseline |
| `fix/mcp-divergences-efficiency` | **1596 passed**, 1 xfailed, 1 xpassed |
| `+ fix/efficiency-notifications` | 1600 |
| `+ fix/efficiency-delete-soups` | 1596 |
| `+ fix/efficiency-submit-local` | 1601 |
| **all three merged** (in `mb-crawler-wt-handoff`) | **1605 passed**, 1 xfailed, 1 xpassed |

All counts from `pytest -q` with `PYTHONPATH=$PWD/src` set to the worktree under
test, and `tahuti.__file__` asserted to resolve inside it.
