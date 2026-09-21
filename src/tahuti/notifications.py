"""MNN Hub notification client for ManageBac.

Why there is no sleep in this module
------------------------------------
This client used to call ``_jitter()`` — ``time.sleep(random.uniform(1.0, 3.0))``
— at the top of ``stats()`` and ``list()``, the two *read* methods, and inside
none of the mutating methods. Measured against the live hub
(``mnn-hub.prod.faria.cn``), 20 un-jittered requests to ``/notifications/stats``
came back min 71.1 ms / median 75.1 ms / mean 76.7 ms / max 92.4 ms, so the
sleep cost 26x the transport it was supposedly guarding, and it was attached to
exactly the cheap polls while every state-changing write went unslowed — the
inverse of what a rate-limit defence looks like. The hub returns no
``X-RateLimit-*`` and no ``Retry-After``, so it declares no ceiling to defend.

Pacing is the caller's decision. ``DaemonConfig.poll_jitter_seconds``
(``tahuti/daemon/events.py``) is applied by ``DaemonService`` between poll
cycles, which is the right altitude: a poll loop that sleeps inside its own
transport cannot poll tightly, which is the whole point of this client. Do not
re-add a sleep here on the theory that it is rate-limit defence — see
``docs/realtime-transport-findings.md`` for the measurements.

Why reads are conditional
-------------------------
The hub honours ``If-None-Match`` on both read endpoints: a 200 with an Etag is
answered 304 with a zero-byte body on the next identical request (32 bytes for
``/notifications/stats``, ~90 KB for ``/notifications``). So the client
remembers the Etag per distinct URL and replays it; a 304 re-returns the cached
payload instead of re-downloading. Steady-state cost of an idle poll is
therefore zero bytes. Any successful mutation drops that cache, because a write
changes what both reads would have returned.
"""

from __future__ import annotations

from typing import Any

import requests

HUB_ENDPOINTS = {
    "managebac.com": "https://mnn-hub.prod.faria.com",
    "managebac.cn": "https://mnn-hub.prod.faria.cn",
}


def hub_for_domain(domain: str) -> str:
    clean = str(domain or "").strip().lower().rstrip(".")
    return HUB_ENDPOINTS.get(clean, HUB_ENDPOINTS["managebac.com"])


class MNNHubClient:
    """REST client for the ManageBac Notification Network hub.

    Parameters
    ----------
    endpoint:
        Hub origin, e.g. ``https://mnn-hub.prod.faria.cn``.
    token:
        Bearer JWT for the hub.
    conditional:
        Send ``If-None-Match`` with the Etag last seen for a URL and replay the
        cached body on a 304 (default). Set ``False`` to always fetch in full.
    """

    def __init__(
        self,
        endpoint: str,
        token: str,
        verify: bool | str = True,
        timeout: float = 15.0,
        conditional: bool = True,
    ):
        self.base = f"{endpoint}/api/frontend/v2"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Content-Type"] = "application/json"
        self.session.verify = verify
        self.timeout = timeout
        self.conditional = conditional
        # Cache key -> Etag the hub last returned for that exact URL. The key is
        # the path plus its query (order-insensitive), because callers hit the
        # same endpoints with different ``page``/``per_page``/``filter`` values.
        self.etags: dict[tuple[str, tuple], str] = {}
        # Cache key -> last decoded body, replayed when the hub answers 304.
        self._bodies: dict[tuple[str, tuple], Any] = {}

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any] | None) -> tuple[str, tuple]:
        return (path, tuple(sorted((params or {}).items())))

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return its decoded JSON body, conditionally if possible."""
        key = self._cache_key(path, params)
        headers: dict[str, str] = {}
        if self.conditional:
            etag = self.etags.get(key)
            if etag:
                headers["If-None-Match"] = etag

        r = self.session.get(
            f"{self.base}{path}", params=params, headers=headers, timeout=self.timeout
        )

        if r.status_code == 304:
            if key in self._bodies:
                return self._bodies[key]
            # A 304 we did not ask for: our Etag was stale or an intermediary
            # answered on the hub's behalf. Drop it and re-ask in full rather
            # than returning an empty body or raising.
            self.etags.pop(key, None)
            r = self.session.get(
                f"{self.base}{path}", params=params, timeout=self.timeout
            )
            if r.status_code == 304:
                raise requests.HTTPError(
                    f"hub answered 304 twice with no cached body for {path}",
                    response=r,
                )

        r.raise_for_status()
        body = r.json()
        etag = r.headers.get("Etag")
        if etag:
            self.etags[key] = etag
            self._bodies[key] = body
        return body

    def _invalidate_cache(self) -> None:
        """Forget every Etag, because a write changed what the reads return.

        Without this, ``mark_read()``/``mark_all_read()``/... would leave the old
        Etag in place and the hub would keep answering 304, so ``stats()`` would
        replay a pre-write unread count indefinitely.
        """
        self.etags.clear()
        self._bodies.clear()

    # ── Read ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Unread counters. Answers from the cached body on a 304."""
        return self._get_json("/notifications/stats").get("stats", {})

    def list(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        filter_: str = "all",
    ) -> dict:
        """Notification page. Answers from the cached body on a 304."""
        params: dict = {"page": page, "per_page": per_page}
        if filter_ and filter_ != "all":
            params["filter"] = filter_
        data = self._get_json("/notifications", params)
        return {
            "items": data.get("items", []),
            "meta": data.get("meta", {}),
        }

    # ── Mutate ──────────────────────────────────────────────────────────

    def _put(self, path: str, json_body: Any = None) -> bool:
        """PUT ``path``; returns whether the hub accepted it, and drops read caches."""
        r = self.session.put(f"{self.base}{path}", json=json_body, timeout=self.timeout)
        if r.status_code not in (200, 204):
            return False
        self._invalidate_cache()
        return True

    def mark_read(self, notification_id: int) -> bool:
        return self._put(f"/notifications/{notification_id}/read")

    def mark_unread(self, notification_id: int) -> bool:
        return self._put(f"/notifications/{notification_id}/unread")

    def mark_all_read(self) -> bool:
        return self._put("/notifications/mark_as_read", json_body={"ids": "all"})

    def star(self, notification_id: int) -> bool:
        return self._put(f"/notifications/{notification_id}/star")

    def unstar(self, notification_id: int) -> bool:
        return self._put(f"/notifications/{notification_id}/unstar")
