# Security Policy

## Supported Versions

Only the latest release line receives security fixes. Please upgrade before
reporting an issue against an older version.

| Version | Supported          |
| ------- | ------------------ |
| 0.5.x   | :white_check_mark: |
| < 0.5   | :x:                |

0.5.x is the current line (`tahuti --version` reports `0.5.1`). Earlier lines are
unsupported and, because the project has not shipped a stable 1.0, may change
or drop interfaces without notice.

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

### How to report

Open a private report here:

> **<https://github.com/allen546/tahuti/security/advisories/new>**

This is the only channel for security reports. It is private to the
maintainers, gives you a private fork to collaborate on a fix, and lets us
publish a coordinated advisory and CVE with credit to you.

There is deliberately **no email address published**. A security report is a
record that has to stay searchable, exportable, and auditable — an inbox is
none of those. GitHub advisories are also the only option that does not put a
personal address in a public repository where it will be scraped and indexed.

> **Prerequisite — private vulnerability reporting must be enabled for this
> link to work.** Until then the URL returns "not found", which reads as
> "no such repo" rather than "report here". To enable it: repository
> **Settings → Code security and analysis → Private vulnerability reporting →
> Enable**. No paid plan is required.

### What to include

Include as much of the following as you can:

- A description of the vulnerability and its impact.
- The affected version (`tahuti --version`) and how you installed it (pip, uv, sdist).
- Steps to reproduce, or a minimal proof of concept.
- Any relevant logs, with credentials and session cookies redacted.

### What to expect

| Stage | Target |
| ----- | ------ |
| Acknowledgement of your report | **3 business days** |
| Initial assessment and severity triage | **7 business days** |
| Status update while a fix is in progress | every **14 days** |
| Fix released for a confirmed issue in a supported version | as soon as practical, targeted within **30 days** |

We will credit you in the release notes and the advisory unless you ask us not
to. We will not pursue legal action against good-faith research that respects
the scope below.

### Scope

In scope: the `tahuti` Python package, the `tahuti` and `tahuti-mcp` console commands (also installed as the
  aliases `mb` and `mb-mcp`),
and `extras/mb-notifier/`.

Out of scope: anything on ManageBac's side; denial of service by polling
ManageBac aggressively; issues that require an attacker to already control your
user account, your machine, or your shell history. Note that this project's
disclaimer (see the README) already warns that automated access may violate
ManageBac's Terms of Service — that is a policy risk, not a vulnerability.

---

## Credential Storage Model and Its Limits

This tool needs your ManageBac password or session cookie to authenticate, so
understanding what is stored where matters.

By default `mb` keeps state in `~/.config/tahuti/` (override with
`MANAGEBAC_CONFIG` / `MANAGEBAC_SESSION` / `MANAGEBAC_CREDS_PATH`; the
pre-rename `MB_CRAWLER_*` names still work as deprecated fallbacks):

| File | Contents | Protection |
| ---- | -------- | --------- |
| `config.json` | School domain, preferences, webhook URL | `0600`, in a `0700` dir |
| `session.json` | Authenticated session cookie | `0600`, in a `0700` dir |
| `creds.json` / `creds.<profile>.json` | **Your ManageBac password, in plaintext** — written only by `login --keep-credentials` | `0600`, in a `0700` dir |
| `snapshot.json` | Cached coursework (task titles, grades) | `0600`, in a `0700` dir |
| `cache/` | Cached HTTP responses, including grade pages and the MNN Hub JWT | `0600` files, `0700` dir |
| `daemon_state.json` | Notification/reminder dedup state | `0600`, in a `0700` dir |
| `daemon.log` / `daemon.pid` | Daemon runtime files | `0600`, in a `0700` dir |

### What this design does

- Every credential-bearing file is written `0600`, created via `mkstemp` and
  atomically `os.replace`d into place, so a plaintext password is never visible
  at a permissive mode even briefly. Directories are `0700`, including the
  parents that `mkdir(parents=True)` would otherwise leave at the umask default.
- **`tahuti logout` deletes the stored password.** It removes this profile's
  creds file and any OS-keychain entry, in addition to clearing the session
  cookie and the response cache; `--all` reaches every profile's file and the
  legacy global one. Pass `--keep-credentials` if you want silent re-login
  preserved.
- **A password is written only when you ask for it.** `tahuti login` saves
  `session.json` — the cookie — so you are not prompted on every command, and
  writes no password at all. `login --keep-credentials` is the only thing that
  puts a password on disk or in the keychain, whatever its source: a password
  from `MANAGEBAC_PASSWORD` is input for that run and is never stored. The
  consequence to be aware of is the one on the other side of the trade: when the
  cookie expires and no password was kept, the next command fails with
  `missing_credentials` rather than prompting, and names the command that fixes
  it.
- **`--no-remember-me` is server-side only.** It omits `remember_me` from the
  login POST, so the cookie's lifetime is ManageBac's default rather than a
  requested persistent one. It writes nothing, deletes nothing, and composes
  with `--keep-credentials` — unlike the old `login --temp`, which it replaces
  and which claimed to write nothing to disk while `_relogin_from_creds`
  rewrote `session.json` unconditionally.
- **Loose permissions are reported.** On startup `mb` warns on stderr if a creds
  file, `session.json`, or `config.json` is group- or world-readable.
  File permissions are the only barrier here, so a `0644` creds file is worth
  shouting about. Set `MANAGEBAC_NO_PERM_WARN=1` to silence it.
- **An optional OS keychain exists.** `tahuti login --keep-credentials --keychain`
  (or `MANAGEBAC_KEYCHAIN=1`) stores the password in the macOS Keychain, the Linux
  Secret Service, or the Windows Credential Locker instead of the cleartext creds
  file. It adds no dependency — it shells out to `security`, `secret-tool`, or
  `powershell.exe` — and is strictly opt-in. `--keychain` decides only *where* a
  kept password goes; on its own it keeps nothing, because `--keep-credentials`
  is what decides whether a password is kept at all. `tahuti logout` deletes the
  keychain entry too. Two Windows-specific limits are worth knowing before you
  opt in: the Credential Locker **roams entries to your Microsoft account by
  default**, and `PasswordVault` needs Windows PowerShell 5.1 (PowerShell 7
  cannot load the WinRT type). If either is unacceptable, do not pass
  `--keychain` on Windows. The secret is handed to the helper over **stdin**,
  never `argv`, so it does not appear in process listings.
- **Windows credential support is implemented but has not been executed on real
  Windows hardware.** The code paths are exercised by tests that fake
  `sys.platform`, and the argv/script construction is verified, but the
  PowerShell/WinRT calls themselves have not run against a live Credential
  Locker. Treat `--keychain` on Windows as unvalidated until it has been.
- Secrets passed to a background daemon go through the child process's
  **environment** (`MB_WEBHOOK_SECRET`, `MANAGEBAC_PASSWORD`,
  `MANAGEBAC_COOKIE`) rather than `argv`, because `argv` is readable by any
  local user via `ps` for the life of the process.
- **The daemon stores no credential of its own, and says so at startup.** A
  long-lived process must not carry a copy of your password in its config file,
  where it would outlive every reason to have it. The cost is that a daemon with
  no `--password` / `--cookie`, no password in its environment and no stored
  credential works until the cookie expires and then stops — so `daemon run` and
  `daemon start` print a warning naming the profile and the command that fixes
  it (`tahuti login --keep-credentials`). It never asks for `keep_credentials`
  itself: whether a password is kept is the operator's decision, made once, at
  login.
- Webhook payloads are signed with HMAC-SHA256 and the bundled receiver in
  `extras/mb-notifier/` refuses unsigned, replayed (bounded id cache), or stale
  (±300s) pushes, comparing digests in constant time.

### What this design does **not** do — read this before using it

Be clear-eyed about the comparison set: rival ManageBac tools never store your
password at all (they keep a browser storage-state file), or encrypt it at rest
(AES256 with a local key). **We store your ManageBac password in cleartext.**
The keychain closes that gap only if you opt in. The honest position:

- **Default storage is cleartext at `0600`, and now it is opt-in.** File
  permissions are the *only* barrier. Anything running as your user — malware, a
  compromised editor extension, a stray backup that drops modes — can read it.
  `root` and any process with `CAP_DAC_OVERRIDE` can read it regardless. This is
  exactly why the loose-permission warning exists: the barrier is thin and worth
  monitoring. What changed is that a bare `tahuti login` no longer creates the
  file at all; `creds.json` / `creds.<profile>.json` exists only because someone
  passed `--keep-credentials`.
- **The keychain is optional and has real limits.** It is unlocked only while
  you are logged in, so on a headless Linux box with no secret service the store
  fails and `mb` falls back to the cleartext creds file with a warning rather
  than silently losing your credential. On macOS, `security add-generic-password`
  accepts a secret only as an argument, so the password is briefly visible in
  that short-lived child's `argv` — a window of milliseconds, after which the
  item is encrypted at rest by the login keychain. Linux `secret-tool` takes the
  secret on stdin and has no such exposure. A keychain item is also not covered
  by your normal file backups.
- **`--keep-credentials` deliberately re-opens the hole.** `tahuti logout`
  deletes the password by default because that is the safer default, and a bare
  `login` never writes it; the flag exists for users who prefer silent re-login
  over revocation. Know which one you are relying on.
- **`MANAGEBAC_PASSWORD` and `MANAGEBAC_COOKIE` are now read as input**, not
  just exported to the daemon child — so they work for non-interactive and CI
  use (`MANAGEBAC_PASSWORD=... tahuti daemon run` needs no prompt), with an
  explicit `--password` / `--cookie` taking precedence. That also means a
  leaked environment variable is now directly usable as a credential, and a
  process's environment is still readable by its own user. They are input only:
  a password from the environment is never written to disk.
- **Webhook payloads contain student PII.** Concretely, the JSON body carries
  task titles, class names, class and task ids, due dates, `grade_letter` /
  `grade_score`, and a task URL — plus `sender` (the teacher or staff member who
  triggered it) and `body_preview` (a truncated verbatim excerpt of the ManageBac
  notification text). It does **not** contain the student's name or free-text
  teacher feedback or rubric comments; `tahuti feedback` fetches those separately and
  never ships them to a webhook. Point `--webhook-url` at `https://`; an
  `http://` endpoint sends that data unencrypted and accepts forged events from
  anything that can reach the port.
- **`--no-verify-tls` disables certificate verification** on the ManageBac
  client's requests session, which makes a man-in-the-middle trivial. It is
  offered for self-hosted instances behind a proxy with an untrusted CA. Do not
  use it on an untrusted network. Note that it covers ManageBac API traffic
  only — webhook delivery has its own `verify_tls` setting in the daemon config,
  which this flag does **not** change, so webhook POSTs still verify normally.
- **`--password` / `-p` on the command line lands in your shell history.** Prefer
  the interactive prompt or `MANAGEBAC_PASSWORD`.

If any of these limits is unacceptable for your environment, run `mb` inside a
container or a dedicated locked-down user account.
