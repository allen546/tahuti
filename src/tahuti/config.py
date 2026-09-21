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


def resolve_creds_path(explicit: str | Path | None = None) -> Path:
    """Resolve the path to creds.json (explicit arg, env var, or default)."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = env_value(CREDS_ENV, CREDS_ENV_LEGACY)
    if from_env:
        return Path(from_env).expanduser()
    return config_dir() / "creds.json"


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
        or config_data.get("active_profile")
        or session_data.get("active_profile")
        or DEFAULT_PROFILE_NAME
    )

    # Flat root keys first, falling back to legacy profiles shape for backward compatibility
    if "school" in config_data or "defaults" in config_data:
        profile_data = config_data
        defaults = _as_dict(profile_data.get("defaults"))
    elif "profiles" in config_data:
        profile_data = _as_dict(_as_dict(config_data.get("profiles")).get(active_profile))
        defaults = _as_dict(profile_data.get("defaults"))
    else:
        profile_data = {}
        defaults = {}

    if "school" in session_data or "cookie" in session_data or "base_url" in session_data:
        session_profile_data = session_data
    elif "profiles" in session_data:
        session_profile_data = _as_dict(_as_dict(session_data.get("profiles")).get(active_profile))
    else:
        session_profile_data = {}

    profile = ProfileConfig(
        name=active_profile,
        school=profile_data.get("school"),
        domain=profile_data.get("domain"),
        email=profile_data.get("email"),
        default_view=defaults.get("view", "all"),
        default_pages=defaults.get("pages", 10),
        default_subject=defaults.get("subject", ""),
        default_details=defaults.get("details", False),
        default_format=defaults.get("format", "pretty"),
        default_cache_ttl=_coerce_cache_ttl(defaults.get("cache_ttl")),
    )
    session = SessionConfig(
        name=active_profile,
        school=session_profile_data.get("school"),
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
    config_data.pop("profiles", None)
    config_data.pop("active_profile", None)
    config_data.update({
        "version": 1,
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
    })
    _write_json(state.config_path, config_data)


def save_session(state: AppState) -> None:
    session_data = _read_json(state.session_path)
    session_data.pop("profiles", None)
    session_data.pop("active_profile", None)
    session_data.update({
        "version": 1,
        "school": state.session.school,
        "domain": state.session.domain,
        "email": state.session.email,
        "base_url": state.session.base_url,
        "cookie": state.session.cookie,
        "logged_in_at": state.session.logged_in_at,
    })
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
    if state.session_path.exists():
        state.session_path.unlink()


def purge_config(state: AppState, all_profiles: bool = False) -> list[str]:
    """Delete configuration settings from config.json, returning the names removed."""
    config_data = _read_json(state.config_path)
    had_config = any(
        k in config_data
        for k in ("school", "domain", "email", "defaults", "profiles")
    )
    if not had_config:
        return []
    removed = (
        sorted(config_data.get("profiles", {}).keys())
        if all_profiles and isinstance(config_data.get("profiles"), dict)
        else [state.active_profile or DEFAULT_PROFILE_NAME]
    )
    for k in ("school", "domain", "email", "defaults", "profiles", "active_profile"):
        config_data.pop(k, None)
    config_data["version"] = 1
    _write_json(state.config_path, config_data)
    return removed


purge_profiles = purge_config


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
        resolve_creds_path(),
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
    "DEFAULT_CREDS_PATH": resolve_creds_path,
}


def __getattr__(name: str):
    """Resolve ``CONFIG_DIR`` / ``DEFAULT_*_PATH`` on access, not at import."""
    resolver = _LEGACY_PATHS.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()
