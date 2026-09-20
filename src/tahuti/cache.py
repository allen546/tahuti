"""Disk-based HTTP response cache with TTL for tahuti."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from .config import config_dir

def default_cache_dir() -> Path:
    """The response-cache root, resolved per call.

    A module-level constant freezes whatever ``$HOME`` was when this module was
    first imported, which is the bug :func:`tahuti.config.config_dir` documents;
    :data:`DEFAULT_CACHE_DIR` below is a snapshot of this for callers that
    cannot be handed a live path.  Anything that must follow a redirected home
    (the submit containment check, tests) calls this instead.
    """
    return config_dir() / "cache"


DEFAULT_CACHE_DIR = default_cache_dir()
DEFAULT_TTL = 900  # 15 minutes


class ResponseCache:
    """Simple disk-based cache keyed by URL hash.

    Each entry stores ``{url, status, body, timestamp}`` as a JSON file, plus
    whatever validator the caller supplies — an ``etag`` to revalidate with, a
    ``ttl`` of the entry's own.
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        ttl: int = DEFAULT_TTL,
        enabled: bool = True,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        # A None TTL is not "cache forever", it is a TypeError waiting for the
        # first get(), which compares `time.time() - ts` against it. config
        # coerces it, but a caller constructing a ResponseCache directly
        # bypasses that layer, so it is defended here too.
        self.ttl = DEFAULT_TTL if ttl is None else ttl
        self.enabled = enabled

    def _key(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:32]

    def _path(self, url: str) -> Path:
        return self.cache_dir / f"{self._key(url)}.json"

    def _read_entry(self, url: str) -> dict | None:
        """Read and decode the entry stored for *url*, or ``None`` if there is none.

        :meth:`get` and :meth:`get_entry` share this and nothing else.  They
        agree on what "no entry" means — a disabled cache, a missing file, an
        unreadable one, or JSON that is not an object — and they disagree about
        what a *decoded* entry is good for, which is why the rest is not shared.

        The read is a ``json.loads`` of a body that runs to 171,379 B on the
        calendar page, so doing it twice inside one request would be the
        expensive kind of duplication.  It was also the kind that drifts: this
        pair had already grown apart on whether an entry with no ``"body"``
        reads as absent or as a crash.
        """
        if not self.enabled:
            return None
        p = self._path(url)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def get(self, url: str, allow_stale: bool = False) -> tuple[str, int] | None:
        """Return ``(body, status)`` if cached and fresh, else ``None``.

        If ``allow_stale`` is True, returns the cached value even if expired or invalidated.

        An entry may record its own ``ttl`` — see :meth:`put` — and that wins
        over the cache's, because the two describe different horizons: the
        webcal token outlives the 171 KB page it is scraped from.
        """
        data = self._read_entry(url)
        if data is None:
            return None

        is_expired = time.time() - data.get("ts", 0) > data.get("ttl", self.ttl)
        is_invalidated = bool(data.get("invalidated", False))

        if (is_expired or is_invalidated) and not allow_stale:
            return None
        return data["body"], data["status"]

    def get_entry(self, url: str) -> dict | None:
        """Return the whole cached entry, or ``None``.

        :meth:`get` returns a ``(body, status)`` pair because that is all the
        HTML path wants.  A conditional request needs more: the stored ``etag``
        to send, *and* the body to replay when the server answers ``304`` — and
        by the time it runs the entry is normally past its TTL, so it has to be
        asked for deliberately rather than through :meth:`get`.

        Expiry and invalidation are the caller's business here.  An invalidated
        entry is returned too, because a ``304`` is precisely the evidence that
        the invalidated copy is still what the server would send.

        An entry with no ``"body"`` is not an entry, so it reads as absent.
        :meth:`get` disagrees and raises ``KeyError`` on the same file; that is
        the one asymmetry left between the two, and it is deliberate on both
        sides — see :meth:`_read_entry`.
        """
        data = self._read_entry(url)
        if data is None or "body" not in data:
            return None
        return data

    def _harden_tree(self) -> None:
        """Close the cache tree to other local users, and stop there.

        ``mkdir(parents=True)`` leaves the directories it creates at the umask
        default, and the cache holds full grade pages plus the MNN-hub Bearer
        JWT, so the credential-bearing tree is tightened to 0700.

        Only directories *inside this package's own state directory* are
        touched, up to and including that directory. The previous version walked
        ``self.cache_dir.parents`` all the way to ``/``: unprivileged, the
        chmods above ``$HOME`` failed and the OSError was swallowed, but under
        root or in a container they succeeded — locking ``/home`` and ``/`` to
        0700 and breaking every other account's home traversal. A single
        ``put()`` also reset the user's own ``$HOME`` from 0755 to 0700.

        A cache directory a caller placed outside our own tree keeps only its
        own directory hardened; that is not ours to widen.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        boundary = config_dir()
        if boundary not in self.cache_dir.parents:
            return
        # `parents` is ordered nearest-first, so this slice runs from just below
        # the cache dir up to the boundary; walking it outermost-in keeps the
        # traversal path valid as each level is tightened.
        ours = self.cache_dir.parents[: self.cache_dir.parents.index(boundary) + 1]
        for parent in reversed(ours):
            try:
                if parent.is_dir():
                    os.chmod(parent, 0o700)
            except OSError:
                pass

    def put(
        self,
        url: str,
        body: str,
        status: int,
        etag: str | None = None,
        ttl: int | None = None,
    ) -> None:
        """Write a response to the cache.

        *etag* is recorded when the server sent one, and omitted from the entry
        entirely when it did not.  *ttl* is the entry's own lifetime and is
        recorded the same way: a value's useful horizon is not always the
        cache's, and the webcal token — 36 characters scraped out of a 171 KB
        page — outlives the page that carries it.  Recording it on the entry
        keeps that in one cache object instead of a second one that has to be
        re-synced by hand every time ``client.cache`` is replaced.

        The key set is therefore not fixed, and a caller that stores neither
        validator writes exactly the four keys it always did.
        """
        if not self.enabled:
            return
        self._harden_tree()
        data = {
            "url": url,
            "body": body,
            "status": status,
            "ts": time.time()
        }
        if etag:
            data["etag"] = etag
        # `is not None`, not truthiness: an entry TTL of 0 means "expires at
        # once", which is as deliberate as the cache-level 0 and must not fall
        # back to self.ttl.
        if ttl is not None:
            data["ttl"] = ttl
        p = self._path(url)
        # Create 0600 from birth via a temp file, then atomically replace —
        # avoids any window where cached grade pages/JWTs are world-readable.
        fd, tmp = tempfile.mkstemp(
            dir=str(self.cache_dir), prefix=".cache_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear(self) -> int:
        """Delete every cache entry. Returns the number of files removed.

        Cached bodies include full grade pages and the MNN-hub Bearer JWT, so
        ``tahuti logout`` calls this to avoid leaving credentials on disk.

        The in-flight ``.cache_*.tmp`` files are swept too. Globbing only
        ``*.json`` left any temp file a concurrent ``put`` had created, and the
        ``os.replace`` that was about to land it could complete *after* the
        logout finished — so a "cleared" cache still gained a JWT-bearing entry
        behind the user's back. The temp file is deleted; the racing ``put``
        then fails its ``os.replace`` and cleans up after itself.
        """
        removed = 0
        if not self.cache_dir.exists():
            return 0
        for pattern in ("*.json", ".cache_*.tmp"):
            for f in self.cache_dir.glob(pattern):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def invalidate(self, url: str | None = None) -> None:
        """Mark one entry as invalidated (setting ts=0 and invalidated=True), or mark all entries if *url* is ``None``."""
        if url:
            p = self._path(url)
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    data["ts"] = 0
                    data["invalidated"] = True
                    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        elif self.cache_dir.exists():
            for f in self.cache_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    data["ts"] = 0
                    data["invalidated"] = True
                    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    try:
                        f.unlink()
                    except OSError:
                        pass
