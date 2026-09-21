"""Configuration and session persistence for tahuti."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import sys
import tempfile

CONFIG_ENV = "MANAGEBAC_CONFIG"
SESSION_ENV = "MANAGEBAC_SESSION"
CREDS_ENV = "MANAGEBAC_CREDS_PATH"
# Escape hatch so scripts and CI can silence the loose-permission warning.
PERM_WARN_ENV = "MANAGEBAC_NO_PERM_WARN"
# Credentials read as *input*, not just exported into the daemon child.
PASSWORD_ENV = "MANAGEBAC_PASSWORD"
COOKIE_ENV = "MANAGEBAC_COOKIE"

# The response-cache TTL default, in seconds. Declared here rather than
# imported from .cache, because .cache imports this module for config_dir() and
# the reverse import would be circular. tests/test_config.py asserts the two
# agree, so the duplication cannot drift unnoticed.
DEFAULT_CACHE_TTL = 900

# The pre-rename spellings. Kept as deprecated fallbacks for exactly the reason
# the `mb` command alias survived the rename: a working setup must not break
# because a variable changed its name. The new name wins when both are set, so
# anyone who has already migrated is never overridden by a stale old one.
CONFIG_ENV_LEGACY = "MB_CRAWLER_CONFIG"
SESSION_ENV_LEGACY = "MB_CRAWLER_SESSION"
CREDS_ENV_LEGACY = "MB_CRAWLER_CREDS_PATH"
PERM_WARN_ENV_LEGACY = "MB_CRAWLER_NO_PERM_WARN"
PASSWORD_ENV_LEGACY = "MB_CRAWLER_PASSWORD"
COOKIE_ENV_LEGACY = "MB_CRAWLER_COOKIE"

# Permission floor for anything this package writes that can hold a secret.
# Any group- or other-readable bit means every local user can read the file.
SECURE_FILE_MODE = 0o600

#: The profile used when none is named. Also the one profile whose credential
#: file keeps the plain ``creds.json`` name — see :func:`creds_filename`.
DEFAULT_PROFILE_NAME = "default"

#: The credential filename the default profile keeps, and the name of the
#: pre-per-profile global file that :func:`legacy_creds_path` falls back to.
LEGACY_CREDS_FILENAME = "creds.json"


def env_value(new: str, legacy: str) -> str | None:
    """Read *new*, falling back to the deprecated *legacy* spelling.

    One helper so the rename's precedence rule is stated once instead of once
    per variable. An empty value counts as unset, matching how every caller
    treats these.
    """
    return os.environ.get(new) or os.environ.get(legacy) or None


def _coerce_cache_ttl(value: object) -> int:
    """Return *value* as a usable cache TTL, substituting the default.

    A config key that is present but null reaches `dict.get` as None rather
    than as the default, and a None TTL is not "cache forever" — it is a
    TypeError waiting for the first `get()`, which compares `time.time() - ts`
    against it. Anything non-numeric is treated the same way, since a config
    written by hand is not the place to be strict.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_CACHE_TTL
    return int(value)


def _as_dict(value: Any) -> dict:
    """Return *value* if it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


# The base domain is deliberately *not* defaulted here. ``None`` means "not
# configured", and ``auth.build_client`` substitutes the string
# ``"managebac.com"`` at the one point a domain has to become one — after the
# flags, the profile and the session have all had their say. It used to be the
# dataclass default below, which made a domain *never* absent: an unset profile
# reported ``"managebac.com"``, the "only ask for what is unknown" rule in
# ``_prompt_login_setup`` could never fire, and every interactive ``login``
# re-asked the one question a configured machine had already answered.


@dataclass
class ProfileConfig:
    name: str
    school: str | None = None
    domain: str | None = None
    email: str | None = None
    default_view: str = "all"
    default_pages: int = 10
    default_subject: str = ""
    default_details: bool = False
    default_format: str = "pretty"
    default_cache_ttl: int = DEFAULT_CACHE_TTL


@dataclass
class SessionConfig:
    name: str
    school: str | None = None
    domain: str | None = None
    email: str | None = None
    base_url: str | None = None
    cookie: str | None = None
    logged_in_at: str | None = None


@dataclass
class AppState:
    config_path: Path
    session_path: Path
    active_profile: str
    profile: ProfileConfig
    session: SessionConfig


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def config_dir() -> Path:
    """Directory holding all persisted state.

    Resolved on every call rather than captured at import. ``Path.home()``
    reads ``$HOME``, so a module-level constant froze whatever the environment
    was when :mod:`tahuti.config` was first imported — and could then disagree
    with :func:`resolve_creds_path` and its siblings, which re-resolve per call.
    One code path would write to one directory while another read from a
    different one, which is exactly how a saved password ends up invisible to
    the code that goes looking for it.
    """
    return Path.home() / ".config" / "tahuti"


def default_config_path() -> Path:
    return config_dir() / "config.json"


def default_session_path() -> Path:
    return config_dir() / "session.json"


# ── the saved password, keyed by profile ─────────────────────────────────
#
# Profiles have been in this config format since the first commit, but the
# saved-password file was added later without profile keying, so it landed on
# one global path. The damage was concrete: `logout` on one profile deleted the
# password every profile was relying on, and two accounts could not both keep
# one — the second `login` overwrote the first.
#
# So the file is per-profile now, with the *default* profile keeping the
# historical `creds.json` name. That is deliberate: the single-profile install
# is the common case, and leaving its path untouched means no migration, no
# surprise, and no second copy of a cleartext password to reason about. The
# suffix only appears once a second profile exists.


def creds_filename(profile: str | None = None) -> str:
    """The credential filename belonging to *profile*.

    ``creds.json`` for the default profile (the historical name, so an existing
    install keeps working with no migration), ``creds.<profile>.json`` for any
    other one.
    """
    if not profile or profile == DEFAULT_PROFILE_NAME:
        return LEGACY_CREDS_FILENAME
    return f"creds.{profile}.json"


def default_creds_path(profile: str | None = None) -> Path:
    """The file *profile*'s saved password is written to."""
    return config_dir() / creds_filename(profile)


def legacy_creds_path() -> Path:
    """The pre-per-profile global credential file.

    Kept as a read-only fallback: the password file was written without profile
    keying while profiles already existed, so a real install (the owner's
    included) has one account's password sitting in ``creds.json`` whichever
    profile it was saved from. Without this entry, upgrading would force a
    re-login for that account. It is *not* a long compatibility tail — the
    mechanism is weeks old and the fallback exists for files that are on disk
    right now.
    """
    return config_dir() / LEGACY_CREDS_FILENAME


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from_env = env_value(CONFIG_ENV, CONFIG_ENV_LEGACY)
    if from_env:
        return Path(from_env).expanduser()
    return default_config_path()


def resolve_session_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from_env = env_value(SESSION_ENV, SESSION_ENV_LEGACY)
    if from_env:
        return Path(from_env).expanduser()
    return default_session_path()


def resolve_creds_path(explicit: str | None = None, profile: str | None = None) -> Path:
    """Resolve the file holding *profile*'s saved password for silent re-login.

    An explicit path, then ``MANAGEBAC_CREDS_PATH`` (deprecated:
    ``MB_CRAWLER_CREDS_PATH``), then the profile's own file under the config
    directory.
    """
    if explicit:
        return Path(explicit).expanduser()
    from_env = env_value(CREDS_ENV, CREDS_ENV_LEGACY)
    if from_env:
        return Path(from_env).expanduser()
    return default_creds_path(profile)


def creds_paths(profile: str | None = None) -> list[Path]:
    """Every file *profile*'s saved password may live in, best first.

    Normally just the profile's own file. The pre-per-profile global
    ``creds.json`` is appended **only when the profile's own file does not
    exist**, which is the case where this profile would read it: the password
    file was written without profile keying while profiles already existed, so
    one account's password sits in ``creds.json`` no matter which profile saved
    it, and an upgrade must not force a re-login for it.

    Callers that *delete* (``logout``) clear every path returned, because
    leaving the fallback behind would keep the password on disk after the user
    asked to be logged out — and would let the very next command silently
    re-login from it. Callers that *write* use the first entry only.

    Nothing is appended when an explicit path or ``MANAGEBAC_CREDS_PATH`` names
    one exact file, and nothing is appended for the default profile, whose own
    file *is* ``creds.json``. A profile that already has its own file never
    reaches for another profile's — that collision is what per-profile
    credentials exist to remove.
    """
    primary = resolve_creds_path(profile=profile)
    if primary.exists():
        return [primary]
    if (
        profile
        and profile != DEFAULT_PROFILE_NAME
        and primary == default_creds_path(profile)
    ):
        legacy = legacy_creds_path()
        if legacy != primary:
            return [primary, legacy]
    return [primary]


def all_creds_paths() -> list[Path]:
    """Every credential file on disk, for ``logout --all``.

    Globbed rather than derived from the profile list, so a profile whose
    ``config.json`` entry has since been removed still has its password
    deleted. The pattern matches ``creds.json`` and ``creds.<profile>.json``
    and nothing else this package writes; the dot-prefixed temp files
    ``_write_json`` creates do not match.
    """
    directory = config_dir()
    found = {p for p in directory.glob("creds*.json") if p.is_file()}
    # An explicit path or env override can name a file outside that directory,
    # and `--all` still has to reach it.
    found.add(resolve_creds_path())
    return sorted(found)


def clear_creds(path: str | Path) -> bool:
    """Delete the saved password file.

    Returns *True* when a file was actually removed, *False* when there was
    nothing to delete or the unlink failed. Callers surface this so
    ``tahuti logout`` can report honestly rather than claiming a deletion that
    did not happen.
    """
    try:
        Path(path).unlink()
        return True
    except OSError:
        # FileNotFoundError lands here too — "nothing to delete" is not an error.
        return False


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    """Write JSON to *path* with 0600 permissions, atomically.

    The file is created via ``mkstemp`` (0600 from birth) and then
    ``os.replace``d into place, so the plaintext password is never visible
    at a permissive mode, even briefly.
    """
    _ensure_parent(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp_name, SECURE_FILE_MODE)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_state(
    profile_name: str | None = None,
    config_path: str | None = None,
    session_path: str | None = None,
) -> AppState:
    config_file = resolve_config_path(config_path)
    session_file = resolve_session_path(session_path)
    config_data = _read_json(config_file)
    session_data = _read_json(session_file)

    active_profile = (
        profile_name
        or session_data.get("active_profile")
        or config_data.get("active_profile")
        or DEFAULT_PROFILE_NAME
    )

    profile_data = _as_dict(_as_dict(config_data.get("profiles")).get(active_profile))
    defaults = _as_dict(profile_data.get("defaults"))
    session_profile_data = _as_dict(_as_dict(session_data.get("profiles")).get(active_profile))

    profile = ProfileConfig(
        name=active_profile,
        school=profile_data.get("school"),
        # No `"managebac.com"` fallback here, deliberately. `dict.get(key)`
        # returns None for an absent key *and* for a key present with an
        # explicit null, so a config written by hand or by an older
        # `save_profile` carrying `"domain": null` lands on exactly the same
        # `None` as a config with no domain key at all — which is the point:
        # "unset" has to be representable, or the login prompt cannot tell a
        # configured machine from a fresh one. `auth.build_client` substitutes
        # the default, at the one place a domain must become a string.
        domain=profile_data.get("domain"),
        email=profile_data.get("email"),
        default_view=defaults.get("view", "all"),
        default_pages=defaults.get("pages", 10),
        default_subject=defaults.get("subject", ""),
        default_details=defaults.get("details", False),
        default_format=defaults.get("format", "pretty"),
        # `dict.get(key, default)` returns the default only when the key is
        # ABSENT. A key present with a null value — `"cache_ttl": null`, which
        # a hand-edited or schema-defaulted config produces — returns None, and
        # ResponseCache(ttl=None) then raises `TypeError: '>' not supported
        # between instances of 'float' and 'NoneType'` on its first get(),
        # after put() has already written the entry. Coerce here, at the one
        # place the value enters the system.
        default_cache_ttl=_coerce_cache_ttl(defaults.get("cache_ttl")),
    )
    session = SessionConfig(
        name=active_profile,
        school=session_profile_data.get("school"),
        # Session falls back to the profile, which is already correct: the
        # session file is written on every login from the resolved client, so it
        # normally carries a domain of its own. When it does not, an unset
        # profile means `None` reaches `SessionConfig` too — still "unset", never
        # the built-in default, which is what keeps the login prompt honest.
        domain=session_profile_data.get("domain", profile.domain),
        email=session_profile_data.get("email"),
        base_url=session_profile_data.get("base_url"),
        cookie=session_profile_data.get("cookie"),
        logged_in_at=session_profile_data.get("logged_in_at"),
    )
    return AppState(
        config_path=config_file,
        session_path=session_file,
        active_profile=active_profile,
        profile=profile,
        session=session,
    )


def save_profile(state: AppState) -> None:
    config_data = _read_json(state.config_path)
    profiles = config_data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = config_data["profiles"] = {}
    profiles[state.active_profile] = {
        "school": state.profile.school,
        "domain": state.profile.domain,
        "email": state.profile.email,
        "defaults": {
            "view": state.profile.default_view,
            "pages": state.profile.default_pages,
            "subject": state.profile.default_subject,
            "details": state.profile.default_details,
            "format": state.profile.default_format,
            "cache_ttl": state.profile.default_cache_ttl,
        },
    }
    config_data["version"] = 1
    config_data["active_profile"] = state.active_profile
    _write_json(state.config_path, config_data)


def save_session(state: AppState) -> None:
    session_data = _read_json(state.session_path)
    profiles = session_data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = session_data["profiles"] = {}
    profiles[state.active_profile] = {
        "school": state.session.school,
        "domain": state.session.domain,
        "email": state.session.email,
        "base_url": state.session.base_url,
        "cookie": state.session.cookie,
        "logged_in_at": state.session.logged_in_at,
    }
    session_data["version"] = 1
    session_data["active_profile"] = state.active_profile
    _write_json(state.session_path, session_data)


def save_creds(path: str | Path, email: str, password: str) -> None:
    """Save email/password to an external JSON file for silent re-login."""
    p = Path(path)
    # Written 0600 from birth via _write_json's atomic temp-file path.
    _write_json(p, {"email": email, "password": password, "version": 1})


def load_creds(path: str | Path) -> dict | None:
    """Load email/password from an external JSON file.

    Returns a dict with ``email`` and/or ``password`` keys, or *None* if the
    file doesn't exist or can't be parsed.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {k: data[k] for k in ("email", "password") if k in data}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_session(state: AppState, all_profiles: bool = False) -> None:
    if all_profiles:
        if state.session_path.exists():
            state.session_path.unlink()
        return

    session_data = _read_json(state.session_path)
    profiles = _as_dict(session_data.get("profiles"))
    profiles.pop(state.active_profile, None)
    if profiles:
        session_data["profiles"] = profiles
        session_data["version"] = 1
        session_data["active_profile"] = state.active_profile
        _write_json(state.session_path, session_data)
    elif state.session_path.exists():
        state.session_path.unlink()


def purge_profiles(state: AppState, all_profiles: bool = False) -> list[str]:
    """Delete profile entries from ``config.json``, returning the names removed.

    ``logout`` used to stop at the session, the cache and the credential files,
    which left the ``profiles.<name>`` entry in place — so a machine that had
    just been logged out still knew which school it belonged to, and the next
    command silently re-authenticated against it. This is the other half.

    The entry goes **wholesale**: school, domain, email *and* the ``defaults``
    block. Emptying the fields instead would leave a half-profile that
    ``load_state`` happily resolves, so the machine would keep naming a school
    it no longer has any settings for.

    ``active_profile`` is dropped when it names a profile that is no longer in
    the map, because otherwise the next command would resolve straight back to
    the missing name and report an empty profile as configured. With no pointer
    left, ``load_state`` falls back to :data:`DEFAULT_PROFILE_NAME`, which is
    the documented outcome: re-supply ``--profile``, or accept the default.

    An empty ``profiles`` map is a shape ``load_state`` already tolerates — it
    reads ``config_data.get("profiles", {})`` — so ``--purge --all`` leaves a
    file that still parses and still loads, with every field reporting ``None``.
    The map is kept rather than removed so a later ``save_profile`` has
    ``setdefault`` to write into without reconstructing the file.

    Returns the names actually removed, which is empty both when there was
    nothing to remove and when there was no config file at all. A purge that
    deleted nothing is not a failure, and the caller reports it as such rather
    than claiming a deletion that did not happen.
    """
    config_data = _read_json(state.config_path)
    profiles = _as_dict(config_data.get("profiles"))

    if all_profiles:
        targets = sorted(profiles)
    else:
        targets = [state.active_profile] if state.active_profile in profiles else []

    removed: list[str] = []
    for name in targets:
        # Every name in `targets` was present in the map — `targets` is either
        # the map's own keys or a membership-checked single name — so the pop
        # always has something to remove and the name is always reportable. A
        # profile stored as an explicit null has no entry to lose but is gone
        # from the map all the same, which is the outcome asked for either way.
        profiles.pop(name, None)
        removed.append(name)

    # A pointer still naming a profile that is not in the map — whether this run
    # just deleted it or it was already gone — would send the next command
    # straight to a name with nothing behind it. `--purge` is the moment the
    # operator asked for the profile to stop existing, so clean it up here too.
    pointer = config_data.get("active_profile")
    dangling = pointer is not None and pointer not in profiles

    if not removed and not dangling:
        return []

    config_data["profiles"] = profiles
    config_data["version"] = 1
    if dangling:
        config_data.pop("active_profile", None)
    _write_json(state.config_path, config_data)
    return removed


def file_mode(path: str | Path) -> int | None:
    """Return the file's permission bits, or *None* if it cannot be stat'd."""
    try:
        return Path(path).stat().st_mode & 0o777
    except OSError:
        return None


def is_too_permissive(path: str | Path) -> bool:
    """True when *path* is readable or writable by group/other.

    ``creds.json`` and ``session.json`` are written 0600 by this package, so a
    looser mode means something outside `mb` changed it — a stray backup, a
    `cp` that dropped modes, a config-management tool. Since file permissions
    are the *only* barrier protecting a cleartext password here, silently
    accepting a 0644 creds file would undercut the whole storage model.
    """
    mode = file_mode(path)
    if mode is None:
        return False
    return bool(mode & 0o077)


def insecure_state_files() -> list[Path]:
    """Every existing credential-bearing state file with looser-than-0600 modes."""
    found: list[Path] = []
    seen: set[Path] = set()
    # `all_creds_paths()` rather than the single default: the password file is
    # per-profile now, so checking only one of them would report a clean bill of
    # health while another profile's cleartext password sat at 0644.
    candidates = [
        *all_creds_paths(),
        resolve_session_path(),
        resolve_config_path(),
    ]
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if is_too_permissive(candidate):
            found.append(candidate)
    return found


def warn_on_weak_permissions(stream=None) -> list[str]:
    """Warn on stderr about any credential file readable by other local users.

    Returns the warnings emitted. Writes to *stream* (stderr by default) rather
    than using :mod:`logging` so the message survives a caller that has
    reconfigured logging, and never contaminates ``--format json`` stdout.
    """
    stream = sys.stderr if stream is None else stream
    insecure = insecure_state_files()
    if not insecure:
        return []
    messages = [
        f"{path} is mode {file_mode(path):04o} — readable by other users on this "
        f"machine. Your ManageBac password or session cookie may be exposed; run "
        f"`chmod 600 {path}`."
        for path in insecure
    ]
    if not env_value(PERM_WARN_ENV, PERM_WARN_ENV_LEGACY):
        for message in messages:
            print(f"warning: {message}", file=stream)
    return messages


# ── Own-state containment ──────────────────────────────────────────────
# `submit` uploads whatever readable regular file it is handed, and the MCP
# tool wrapping it is driven by a model rather than by someone choosing a path
# on purpose — so a confused call could ship tahuti's own credential and cache
# material to a school dropbox that a teacher reads.  The response cache alone
# holds full grade pages plus the MNN-hub JWT.
#
# Scope is deliberately narrow: only paths resolving inside tahuti's *own*
# config and cache directories, plus its individual state files in case an env
# var has relocated one outside them.  Files elsewhere on the system
# (~/.ssh/id_rsa, /etc/passwd) are the operating system's business — they
# belong to restrictive file permissions, and an attacker who already has sudo
# has no reason to exfiltrate through tahuti.  Do not "fix" this into a general
# sandbox: no system-directory rules, no allowlists.

#: The daemon's task snapshot lives beside the *config file*, not in
#: ``config_dir()``, because ``MB_CRAWLER_CONFIG`` can move the whole set.
SNAPSHOT_FILENAME = "snapshot.json"


def snapshot_path(state) -> Path:
    """Return the snapshot file belonging to *state*'s config directory.

    The snapshot lives beside the config file, so ``--config`` relocates both
    and a non-default profile is found by the same rule as the default one.
    This is deliberately *not* :data:`DEFAULT_SNAPSHOT_PATH`, which ignores
    ``--config`` and is only a fallback for callers with no state at all.

    The rule lives here, next to the constant it names, because two features
    that otherwise have nothing to do with each other have to agree on the
    filename: :func:`own_state_locations` derives this same path so `submit`
    can refuse to upload tahuti's own snapshot, and the MCP server reads this
    path to answer ``view_task``. Each used to spell the name itself — one
    through the constant, one as a bare literal — so nothing tied the file the
    containment rule protects to the file the MCP server reads. Renaming the
    snapshot would have moved one and left the other looking at a name that no
    longer exists, which reads as an empty snapshot rather than as an error.
    """
    return state.config_path.parent / SNAPSHOT_FILENAME


def _as_path(value: object) -> Path | None:
    """Coerce *value* to a :class:`~pathlib.Path`, or ``None`` if it is not path-like.

    Accepts anything implementing ``__fspath__``, which is what a lazily
    resolved path constant has to expose for the rest of the code to use it —
    so such a proxy can be handed straight in and still be compared resolved.
    """
    if isinstance(value, Path):
        return value
    try:
        return Path(os.fspath(value))
    except TypeError:
        return None


def _resolved(path: Path | None) -> Path | None:
    """*path* fully resolved (``~`` and symlinks), or ``None`` if unresolvable."""
    if path is None:
        return None
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def own_state_locations() -> list[tuple[Path, str]]:
    """Every resolved location holding tahuti's own state, with a description.

    Most specific first, because the response cache sits *inside* the config
    directory and naming the cache is the more useful refusal.  Paths are
    resolved per call so a redirected ``$HOME`` or a monkeypatched config dir
    is honoured — see :func:`config_dir`.
    """
    # Imported here, not at module scope: both modules import this one.
    from .cache import default_cache_dir
    from .daemon.state import default_state_path

    config_file = resolve_config_path()
    candidates = [
        (resolve_creds_path(), "saved-password file"),
        (resolve_session_path(), "session file"),
        (config_file, "config file"),
        (default_state_path(), "daemon state file"),
        (config_file.parent / SNAPSHOT_FILENAME, "task snapshot"),
        (default_cache_dir(), "response-cache directory"),
        (config_dir(), "config directory"),
    ]

    locations: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path, description in candidates:
        resolved = _resolved(_as_path(path))
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        locations.append((resolved, description))
    locations.sort(key=lambda item: len(item[0].parts), reverse=True)
    return locations


def own_state_refusal(value: object, field: str = "file_path") -> str | None:
    """Refusal sentence when *value* is one of tahuti's own state files, else ``None``.

    ``None`` says only "not tahuti's own state" — it says nothing about whether
    the path is otherwise usable, and a path that will not resolve at all is
    left to the caller's own checks rather than guessed at here.  *value* is
    resolved before comparing, so a symlink pointing into tahuti's state is
    refused by its target.
    """
    resolved = _resolved(_as_path(value))
    if resolved is None:
        return None
    for location, description in own_state_locations():
        if resolved != location and location not in resolved.parents:
            continue
        return (
            f"{field} resolves to {resolved}, which is tahuti's own "
            f"{description} — tahuti's credentials, session cookie and cached "
            "ManageBac pages live there and are never uploaded, so pass a file "
            "from somewhere else"
        )
    return None


#: The pre-lazy names, kept importable so out-of-tree callers do not break.
#: Each resolves on attribute access, so unlike the module-level constants they
#: replaced they cannot go stale when ``$HOME`` changes after import.
_LEGACY_PATHS = {
    "CONFIG_DIR": config_dir,
    "DEFAULT_CONFIG_PATH": default_config_path,
    "DEFAULT_SESSION_PATH": default_session_path,
    "DEFAULT_CREDS_PATH": default_creds_path,
}


def __getattr__(name: str):
    """Resolve ``CONFIG_DIR`` / ``DEFAULT_*_PATH`` on access, not at import."""
    resolver = _LEGACY_PATHS.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()
