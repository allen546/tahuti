"""Shared client construction and authentication for CLI and MCP."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

from .cache import ResponseCache
from .client import ManageBacClient, _REDIRECT_STATUSES
from . import keychain
from .config import (
    AppState,
    clear_creds,
    creds_paths,
    legacy_creds_path,
    load_creds,
    load_state,
    resolve_creds_path,
    save_creds,
    save_profile,
    save_session,
)
from .exceptions import CommandError
from .notifications import MNNHubClient

log = logging.getLogger(__name__)


def hub_client(
    endpoint: str, token: str, *, verify: bool | str, timeout: float = 15.0
) -> MNNHubClient:
    """Build the MNN-hub client, honouring the caller's TLS decision.

    The single construction point for :class:`~tahuti.notifications.MNNHubClient`.
    ``verify`` is keyword-only and has no default on purpose: six call sites
    used to build the client directly and so kept ``verify=True``, which made
    ``--no-verify-tls`` apply to ManageBac and not to the hub. A user who
    explicitly accepted a self-signed or internal CA for the school host got a
    certificate error from the hub instead, with nothing in the message saying
    why. A call site that forgets the argument now fails with ``TypeError``
    rather than silently reverting to the stricter policy.

    Pass ``client.session.verify`` from an already-built
    :class:`~tahuti.client.ManageBacClient` so the hub cannot diverge from
    whatever TLS decision that client is using.
    """
    # `requests` accepts a bool or a CA-bundle path. Anything else — notably the
    # `MagicMock` a test's fake client hands back for `session.verify` — reaches
    # `requests` and is read as a URL scheme, producing "Invalid URL 'ep/...'"
    # from a call site that looks correct. Coerce a non-path truthy value to the
    # strict policy rather than passing it through.
    if not isinstance(verify, (bool, str)):
        verify = True
    return MNNHubClient(endpoint, token, verify=verify, timeout=timeout)


def _creds_path(profile: str | None = None) -> str:
    """Resolve the creds path per-call so tests can redirect it via env.

    ``build_client`` and friends must never touch the developer's real saved
    password when running under pytest.

    This is the *only* way any code here resolves that path — storing, reading,
    deleting and reporting all funnel through it. It used to be captured as a
    module-level constant at import while this function re-resolved dynamically,
    so a caller that imported ``_CREDS_PATH`` and a caller that called
    ``_creds_path()`` could name two different files: one would write the
    password to ``~/.config/tahuti/creds.json`` while the other went looking for
    it in ``~/.config/mb-crawler/creds.json`` and reported ``missing_credentials``.

    *profile* is passed through because the file is per-profile: two accounts
    must be able to keep a password each, and ``logout`` of one must not be able
    to reach the other's.
    """
    return str(resolve_creds_path(profile=profile))


def _store_password(
    email: str,
    password: str,
    use_keychain: bool | None = None,
    profile: str | None = None,
) -> str:
    """Persist a password for silent re-login.

    Returns the backend actually used: ``"keychain"``, ``"file"``, or
    ``"none"``. The OS keychain is opt-in and preferred when available; without
    it the password lands in the cleartext 0600 ``creds.json`` that
    :mod:`tahuti.config` writes. A keychain that fails to store falls back to
    the file rather than silently losing the credential.

    Only ever called when the caller asked for the password to be kept — see
    ``build_client(keep_credentials=...)``.
    """
    if keychain.enabled(use_keychain):
        if keychain.store(email, password):
            # Drop any cleartext copy left by an earlier non-keychain login, so
            # switching backends does not leave the password on disk twice.
            for path in creds_paths(profile=profile):
                clear_creds(path)
            return "keychain"
        log.warning("OS keychain unavailable — falling back to creds.json")
    path = resolve_creds_path(profile=profile)
    save_creds(path, email, password)
    _migrate_legacy_creds(profile, email, written=path)
    return "file"


def _migrate_legacy_creds(profile: str | None, email: str, written: Path) -> None:
    """Drop the pre-per-profile global copy once the profile has its own.

    The password file was written to one global path before it was keyed by
    profile, so an install upgrading mid-life has its password in
    ``creds.json`` while the profile that now owns it writes
    ``creds.<profile>.json``. Leaving the old file would mean two cleartext
    copies of one password, and ``logout`` would have to remember to delete
    both. Only the copy holding *this* account is removed: a different account's
    password in the global file is not ours to delete.

    *written* is the file just created. For the default profile it *is* the
    global file, so there is nothing to migrate — comparing against it is what
    stops this from deleting the password it was just asked to store.
    """
    legacy = legacy_creds_path()
    if legacy == Path(written) or not legacy.exists():
        return
    existing = load_creds(legacy)
    if existing and existing.get("email") == email:
        clear_creds(legacy)


def _load_creds(email_hint: str | None = None, profile: str | None = None) -> dict | None:
    """Load saved credentials, consulting the OS keychain as a fallback.

    ``creds.<profile>.json`` wins when it holds a password so an existing
    install keeps working unchanged. The pre-per-profile global ``creds.json``
    is tried next when this profile has no file of its own, so an upgrade does
    not force a re-login. The keychain is consulted when no file carries a
    password — i.e. after ``tahuti login --keychain`` — using the
    profile/session email as the account name.
    """
    creds = None
    for path in creds_paths(profile=profile):
        creds = load_creds(path)
        if creds is not None:
            break
    if creds and creds.get("password"):
        return creds
    account = (creds or {}).get("email") or email_hint
    if account and keychain.available():
        secret = keychain.lookup(account)
        if secret:
            merged = dict(creds or {})
            merged["email"] = account
            merged["password"] = secret
            return merged
    return creds


def session_email(state: AppState, override: str | None = None) -> str:
    """Return the email that identifies this profile's on-disk state.

    One function because two things are keyed by it and they used to disagree
    about which email they meant: the response-cache directory is a hash of it,
    and the OS-keychain item is filed under it. ``build_client`` preferred an
    explicit ``--email``, then the profile's email, then the session's; the
    ``logout`` handler took the session's first. With the two set to different
    values, ``logout`` deleted a *different* profile's hash directory and left
    the JWT-bearing entries in place while still reporting success — and left
    the keychain item behind for the account it actually deleted nothing for.

    ``logout`` passes no override (its subparser defines no ``--email``), so the
    two agree on ``profile.email or session.email``. Returns ``""`` rather than
    ``None`` when neither is set, so callers can treat the result as a string.
    """
    return override or state.profile.email or state.session.email or ""


def apply_authenticated(state: AppState, client: ManageBacClient, email: str) -> str:
    """Record in memory what this client just authenticated as.

    Returns the email the caller should report. The CLI uses this to fill its
    payload; ``build_client`` uses it before persisting. It lives here so the
    two cannot describe the same login differently — they used to, which is how
    ``logout`` ended up clearing a different profile's cache directory.
    """
    state.profile.school = client.school
    state.profile.domain = client.domain
    state.profile.email = email or state.profile.email

    state.session.school = client.school
    state.session.domain = client.domain
    state.session.email = email or state.session.email
    state.session.base_url = client.base
    state.session.cookie = client.session.cookies.get("_managebac_session")
    state.session.logged_in_at = datetime.now().isoformat()
    return email or state.profile.email or ""


def build_client(
    school: str | None = None,
    domain: str | None = None,
    email: str | None = None,
    password: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    refresh: bool = False,
    reauth: bool = False,
    verify: bool | str = True,
    cache_ttl: int | None = None,
    retry: int = 3,
    remember_me: bool | None = True,
    keep_credentials: bool = False,
    use_keychain: bool | None = None,
) -> tuple[AppState, ManageBacClient, str]:
    """Build and authenticate a :class:`ManageBacClient`.

    Returns ``(state, client, email)``.  Raises :class:`CommandError` on
    missing credentials or authentication failure.

    *remember_me* is what ManageBac is told — ``None`` omits the ``remember_me``
    field from the login POST entirely, which is a server-side cookie-lifetime
    decision and touches nothing on disk.

    *keep_credentials* is whether the password may be written to disk, for
    unattended renewal of an expired cookie. It defaults to *False*: typing a
    password once per expiry is the lower-friction *and* the safer default, and
    a plaintext password is the one artifact here with no upside to keeping.

    *use_keychain* overrides ``MANAGEBAC_KEYCHAIN`` for this call only; ``None``
    defers to the environment. It decides *where* a kept password goes, never
    whether it is kept.

    Everything else is derived, so there is no fourth knob to get out of step:
    the response cache follows ``refresh`` alone (it is on by default and
    ``logout`` clears it), and ``session.json`` is always written — it holds the
    session cookie, and withholding it would mean retyping the password for
    every command. That is the whole point of the split below.
    """
    state = load_state(profile)
    school = school or state.profile.school or state.session.school
    # The single place the base domain's string default belongs, and it is
    # already correct: flag, then profile, then session, then "managebac.com".
    # Everything upstream reports `None` for "not configured"
    # (`ProfileConfig.domain` / `SessionConfig.domain` default to it, and
    # `load_state` reads the config key without a fallback), so this `or` chain
    # is what turns "nobody said" into a hostname — and it is the *only* such
    # substitution, which is why `_prompt_login_setup` can now ask only when
    # nothing supplies one. Do not move the default upstream of here.
    domain = domain or state.profile.domain or state.session.domain or "managebac.com"

    if not school:
        raise CommandError("missing_credentials", "Missing school in args or config")

    email_val = session_email(state, email)
    if not email_val:
        try:
            creds = _load_creds(profile=state.active_profile)
            if creds:
                email_val = creds.get("email")
        except Exception:
            pass

    import hashlib
    from .cache import DEFAULT_CACHE_DIR
    if email_val:
        email_hash = hashlib.sha256(email_val.encode()).hexdigest()[:16]
        cache_dir = DEFAULT_CACHE_DIR / email_hash
    else:
        cache_dir = DEFAULT_CACHE_DIR

    resolved_ttl = (
        cache_ttl if cache_ttl is not None else state.profile.default_cache_ttl
    )
    # `refresh` alone decides. The cache used to hang off `remember` as well, so
    # `--temp` turned it off along with the password it was not saving anyway —
    # and a plain `tahuti list` paid for it in re-crawls it had not asked for.
    # It holds grade pages and the MNN-hub JWT, which is why `logout` clears it
    # rather than why a login may switch it off.
    cache = ResponseCache(cache_dir=cache_dir, enabled=not refresh, ttl=resolved_ttl)
    client = ManageBacClient(
        school, domain=domain, cache=cache, verify=verify, retry=retry
    )

    if cookie:
        client.set_cookie(cookie)
    elif password:
        if not email_val:
            raise CommandError(
                "missing_credentials", "Missing email for password login"
            )
        if not client.login(email_val, password, remember=remember_me):
            raise CommandError("authentication_failed", "ManageBac login failed")
        if keep_credentials:
            _store_password(
                email_val, password, use_keychain, profile=state.active_profile
            )
    elif state.session.cookie and not reauth:
        # Health check: try saved cookie, re-login if stale
        client.set_cookie(state.session.cookie)
        if _is_session_alive(client):
            pass  # cookie is good
        else:
            log.info("Saved cookie expired — attempting silent re-login")
            _relogin_from_creds(client, state, remember_me=remember_me)
    else:
        # No session cookie and no explicit password — try loading from config
        creds = _load_creds(email_val, profile=state.active_profile)
        login_email = email_val or (creds.get("email") if creds else None)
        login_pass = password or (creds.get("password") if creds else None)
        if not login_email or not login_pass:
            raise CommandError(
                "missing_credentials",
                "No session, no password — pass password= or set a password via `tahuti login`",
            )
        if not client.login(login_email, login_pass, remember=remember_me):
            raise CommandError("authentication_failed", "ManageBac login failed")
        # A password supplied by the environment is input, not a request to keep
        # it: `MANAGEBAC_PASSWORD=... tahuti list` must not end up writing that
        # password into creds.json behind the user's back.
        if keep_credentials:
            _store_password(
                login_email, login_pass, use_keychain, profile=state.active_profile
            )

    email_out = apply_authenticated(state, client, email_val or "")
    # One owner for every write. `_authenticate_client` used to save the session
    # too, from outside, with a `persist` switch whose only job was to patch the
    # policy decided here — and `_relogin_from_creds` saved unconditionally, so a
    # stale cookie plus a stored password rewrote session.json whatever the
    # caller asked for. Deciding in one place is what makes the flag honest.
    _persist(state)
    return state, client, email_out


# Statuses this health check knows how to read an answer from. Anything else is
# a host that does not implement HEAD, and gets a GET. Derived from the
# client's own redirect set rather than restating it: those five 3xx codes
# appeared here *and* a third time as a bare tuple in the verdict below, and no
# test referenced any of the three spellings by name, so a status added to one
# and not the others would have moved the behaviour with nothing noticing.
_SESSION_ALIVE_STATUSES = _REDIRECT_STATUSES | {200, 401, 403}


def _is_session_alive(client: ManageBacClient) -> bool:
    """Lightweight health check — HEAD a protected page, return True if the
    session is valid.

    Checks both for login redirects (3xx → /login) and auth failures (401/403).
    Uses a page that requires authentication so an expired session reliably
    redirects.

    HEAD rather than GET because this reads only a status code and a Location
    header, and the GET it would otherwise make is the expensive one:
    ``docs/http-revalidation-findings.md`` measures ``/student/dashboard`` at
    200 with 275,003 B of body for a live credential and 401 with 26 B for a
    dead one. Those are the *GET's* numbers. HEAD is expected to answer with
    the same status and no body, but that was checked in an ad-hoc script that
    was never committed, so it is a design expectation here and not a
    measured claim. An unexpected status falls back to GET rather than being
    guessed at, so a host without HEAD support costs one extra request instead
    of a wrong answer.
    """
    url = f"{client.base}/student/dashboard"
    # allow_redirects=False so the Location header can be inspected directly.
    # r.url always reflects the *request* URL, never the redirect target.
    # The two bound methods rather than session.request(method, ...), so a
    # caller stubbing session.get keeps stubbing the fallback.
    #
    # Two attempts, not a variable number of them. This was
    # `for method, call in (("HEAD", ...), ("GET", ...))`, and every branch
    # below meant something different depending on which iteration it ran in:
    # `continue` meant "try the GET" in one arm and "give up" in the other, and
    # only `method == "HEAD"` kept the two apart. Unrolled, HEAD is the attempt
    # allowed to come back with nothing — either the transport refused it, or
    # the host answered a status this cannot read — and the answer itself is
    # read once, by whichever attempt produced it.
    r = None
    try:
        r = client.session.head(url, allow_redirects=False)
    except Exception:
        # A proxy that mishandles HEAD must not cost a re-login.
        r = None
    if r is not None and r.status_code not in _SESSION_ALIVE_STATUSES:
        # A host that does not implement HEAD answers 405. That says nothing
        # about the session, so it earns a GET rather than a guess.
        r = None
    if r is None:
        try:
            r = client.session.get(url, allow_redirects=False)
        except Exception:
            # Nothing left to try, and no answer: report the session dead.
            return False
    if r.status_code in (401, 403):
        return False
    if r.status_code in _REDIRECT_STATUSES:
        return "/login" not in r.headers.get("Location", "")
    # 200 OK on an auth-required page means the session is valid. An
    # unreadable *GET* status lands here too — the loop did the same, because
    # only the HEAD arm carried the `continue` — and "dead" would be a guess.
    return True


def _relogin_from_creds(
    client: ManageBacClient, state: AppState, remember_me: bool | None = True
) -> None:
    """Re-login using saved credentials. Raises CommandError on failure.

    *remember_me* is the caller's policy, threaded through rather than
    hardcoded: this used to pass ``remember=True`` no matter what the caller
    asked for, so ``--no-remember-me`` silently became ``remember_me=1`` on the
    one path that runs unattended.

    A dead cookie with no stored password is an error, not a prompt and not a
    fallback. Prompting would hang a daemon or a CI job; falling back would
    invent a credential the user never gave. Nothing is written in that case.
    """
    creds = _load_creds(
        state.session.email or state.profile.email, profile=state.active_profile
    )
    if not creds or "email" not in creds or "password" not in creds:
        raise CommandError(
            "missing_credentials",
            f"Cookie expired and no password is saved for profile "
            f"{state.active_profile!r} (looked in "
            f"{', '.join(str(p) for p in creds_paths(profile=state.active_profile))}"
            "). Run `tahuti login --keep-credentials` once to store one, and "
            "later commands will renew the session by themselves; without it, "
            "re-authenticate with `tahuti login`.",
        )
    if not client.login(creds["email"], creds["password"], remember=remember_me):
        raise CommandError("authentication_failed", "Silent re-login failed")
    # In memory only: the caller decides when that reaches disk, so a failed
    # re-login leaves the previous session file untouched rather than half
    # overwritten.
    apply_authenticated(state, client, creds["email"])


def _persist(state: AppState) -> None:
    """Write the profile and session this client authenticated as.

    The single place persistence policy is expressed. ``build_client`` calls it
    once on its way out; :func:`refresh_session` calls it for a caller that is
    renewing a client it already holds. Nothing else in the package writes these
    two files, which is what stops a flag from being silently overruled by a
    second owner.
    """
    save_profile(state)
    save_session(state)


def refresh_session(
    client: ManageBacClient, state: AppState, remember_me: bool | None = True
) -> None:
    """Renew an expired session from saved credentials and persist the result.

    The daemon's re-auth entry point: it holds a client it built earlier and
    cannot call ``build_client`` again mid-loop.
    """
    _relogin_from_creds(client, state, remember_me=remember_me)
    _persist(state)
