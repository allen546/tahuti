"""Probe ManageBac for HTTP revalidation: what the ETag can and cannot do.

Read-only. Answers four questions, each measured against a live account:

  Q1  Can `tahuti calendar` revalidate?  (`events.json`, the `.ics` feed, and
      the `/student/calendar` page that yields the webcal token)
  Q2  Is a client-side *normalized* content hash stable across renders?
      (the only way to detect "unchanged" on the Rails HTML pages, whose
      server ETag rotates every render by construction)
  Q3  Does that hash still discriminate — i.e. can it detect a real change?
  Q4  Does `/dropbox` content-negotiate on `X-Requested-With`, and can a cached
      body of one representation be served to a caller expecting the other?

Uses the saved session cookie only — never the password, never creds.json.
Writes nothing except the evidence directory you name with --out.  Safe to run
against the live account: every request is a GET, paced at >= 1s.

Usage:
    cd <repo root>
    MB_CRAWLER_CREDS_PATH=/nonexistent .venv/bin/python extras/probe_revalidation.py --out /tmp/probe-evidence

The evidence directory holds full grade pages and, critically, a live AWS STS
session token and a ManageBac hub JWT scraped out of the raw HTML.  Treat it as
a credential: mode 0600, and delete it when you are done.  It is deliberately
created outside the repo so hatchling's sdist include list cannot ship it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Never let this script see or touch real credentials.
assert os.environ.get("MB_CRAWLER_CREDS_PATH"), "set MB_CRAWLER_CREDS_PATH=/nonexistent"

from tahuti.auth import build_client  # noqa: E402

# ── The normalization transform, in order ────────────────────────────────
# Surgical VALUE replacement only.  The surrounding markup and every byte of
# page content survive; nothing is deleted.  Each entry is
# (name, compiled regex, replacement).
#
# These are the per-render nonces that make the server's ETag useless: the
# server ETag is sha256(body)[:32], so anything here rotating per render
# rotates the ETag with it, and If-None-Match can never match.
TRANSFORMS = [
    # (a) New Relic NREUM.info — instrumentation inside an inline <script>.
    #     applicationTime is a per-render server clock; queueTime and
    #     transactionName are per-render transaction identifiers.
    ("a1 applicationTime",
     re.compile(r'"applicationTime":[0-9]+'), '"applicationTime":0'),
    ("a2 queueTime",
     re.compile(r'"queueTime":[0-9]+'), '"queueTime":0'),
    ("a3 transactionName",
     re.compile(r'"transactionName":"[^"]*"'), '"transactionName":"NR"'),
    ("a4 agent",
     re.compile(r'"agent":"[^"]*"'), '"agent":"NR"'),

    # (b) autologout meta — a server-side render clock in seconds.
    ("b autologout meta",
     re.compile(r"(<meta content=')[0-9.]+(' name='autologout')"), r"\1NR\2"),

    # (c) csrf-token meta — a fresh Rails CSRF token on every render.
    ("c csrf-token meta",
     re.compile(r'(<meta name="csrf-token" content=")[^"]*(")'), r"\1NR\2"),

    # (d) data-token on the notifications-bell anchor — a freshly minted
    #     MNN hub JWT.
    ("d data-token",
     re.compile(r'(data-token=")[^"]*(")'), r"\1NR\2"),

    # (e) custom-pattern-<uuid> SVG pattern ids inside the inline
    #     gradebook-chart JSON, and the url(#...) references to them.
    ("e custom-pattern uuid",
     re.compile(r"custom-pattern-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
     "custom-pattern-NR"),

    # (f) NEW — pre-signed AWS S3 V4 URLs on inline avatar background-images.
    #     ManageBac mints a fresh signature (Credential, Date, Expires,
    #     Security-Token) per render, so the whole query string rotates.
    #     Without this class, core_tasks is UNSTABLE.  See the findings doc.
    ("f S3 signed-query",
     re.compile(r"\?X-Amz-[^)\"']*"), "?X-Amz-NR"),
]

# The five classes (a)-(e) alone, for the before/after comparison that shows
# why (f) is not optional.
WITHOUT_F = TRANSFORMS[:5]


def normalize(text: str, transforms=TRANSFORMS) -> str:
    for _name, rx, rep in transforms:
        text = rx.sub(rep, text)
    return text


def normhash(text: str, transforms=TRANSFORMS) -> str:
    return hashlib.sha256(normalize(text, transforms).encode("utf-8")).hexdigest()


# ── Probe plumbing ───────────────────────────────────────────────────────

LEDGER: list[dict] = []
_LAST = 0.0


def pace() -> None:
    """>= 1s between requests.  client.session.get bypasses the client's own
    rate limiter (which lives in _request_with_retry), so it is re-created
    here rather than assumed."""
    global _LAST
    now = time.time()
    if now - _LAST < 1.0:
        time.sleep(1.0 - (now - _LAST))
    _LAST = time.time()


def head(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}", flush=True)


class Probe:
    def __init__(self, client, out: Path):
        self.client = client
        self.out = out

    def get(self, url: str, label: str, headers: dict | None = None):
        """One paced GET through the project's own session.  Records the
        ledger.  Nothing credential-bearing is ever printed."""
        pace()
        r = self.client.session.get(url, timeout=40, headers=headers or {})
        etag = r.headers.get("ETag")
        LEDGER.append({
            "label": label,
            "url": url,
            "method": "GET",
            "status": r.status_code,
            "bytes": len(r.content),
            "etag_len": len(etag) if etag else None,
            "etag_prefix4": etag[:4] if etag else None,
            "cache_control": r.headers.get("Cache-Control"),
            "content_type": r.headers.get("Content-Type"),
            "redirects": [h.status_code for h in r.history],
            "sent_if_none_match": bool((headers or {}).get("If-None-Match")),
        })
        print(
            f"  [{len(LEDGER):>2}] GET {label}\n"
            f"       status={r.status_code} bytes={len(r.content)} "
            f"etag_len={len(etag) if etag else None} "
            f"etag4={(etag[:4] if etag else None)} "
            f"cc={r.headers.get('Cache-Control')} "
            f"ct={r.headers.get('Content-Type')} "
            f"redirects={[h.status_code for h in r.history]}",
            flush=True,
        )
        return r

    def save(self, name: str, text: str) -> Path:
        """Write one piece of evidence.  0600, because these bodies carry a
        live AWS STS token and a hub JWT."""
        p = self.out / name
        p.write_text(text, encoding="utf-8")
        os.chmod(p, 0o600)
        return p


# ── Q1: can the calendar command revalidate? ─────────────────────────────

def q1_calendar(p: Probe) -> None:
    head("Q1 - calendar revalidation")
    base = p.client.base
    today = time.strftime("%Y-%m-%d")
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() + 6 * 86400))
    ev_url = f"{base}/student/events.json?start={today}&end={end}"

    ev1 = p.get(ev_url, "events.json cold (ResponseCache bypassed)")
    p.save("events_1.json", ev1.text)
    if ev1.headers.get("ETag"):
        ev2 = p.get(ev_url, "events.json revalidated",
                    headers={"If-None-Match": ev1.headers["ETag"]})
        p.save("events_2.bin", ev2.text)
    else:
        print("  (no ETag on events.json - cannot revalidate)")

    cal = p.get(f"{base}/student/calendar", "student/calendar HTML")
    p.save("raw_calendar.html", cal.text)
    m = re.search(r'href="(webcal://[^"]+)"', cal.text)
    if not m:
        print("  !! no webcal link on /student/calendar")
        return
    ics_url = m.group(1).replace("webcal://", "https://")
    print(f"  webcal token length={len(ics_url.rsplit('/', 1)[-1])} (value withheld)")

    ics1 = p.get(ics_url, "ics cold (ResponseCache bypassed)")
    p.save("ics_1.ics", ics1.text)
    if ics1.headers.get("ETag"):
        ics2 = p.get(ics_url, "ics revalidated",
                     headers={"If-None-Match": ics1.headers["ETag"]})
        p.save("ics_2.bin", ics2.text)
    else:
        print("  (no ETag on the .ics - cannot revalidate)")

    # The /student/calendar page is the one that cannot be revalidated, and it
    # is the bulk of the command's cost.  Prove it directly.
    if cal.headers.get("ETag"):
        cal2 = p.get(f"{base}/student/calendar", "student/calendar revalidated",
                     headers={"If-None-Match": cal.headers["ETag"]})
        p.save("raw_calendar_2.html", cal2.text)
        same = cal2.text == cal.text
        print(f"  -> body byte-identical to the first fetch? {same}")
        print(f"  -> ETag unchanged? {cal2.headers.get('ETag') == cal.headers['ETag']}")


# ── Q2/Q3: normalized-hash stability and sensitivity ─────────────────────

def q2_q3_hash(p: Probe, class_ids: list[str]) -> dict[str, list[str]]:
    head("Q2 - normalized-hash stability (3 fetches per page)")
    targets = {
        "coreA": f"{p.client.base}/student/classes/{class_ids[0]}/core_tasks",
        "tad_upcoming": f"{p.client.base}/student/tasks_and_deadlines?view=upcoming&page=1",
    }
    if len(class_ids) > 1:
        targets["coreB"] = f"{p.client.base}/student/classes/{class_ids[1]}/core_tasks"
    targets["tad_past"] = f"{p.client.base}/student/tasks_and_deadlines?view=past&page=1"

    bodies: dict[str, list[str]] = {}
    for name, url in targets.items():
        bodies[name] = []
        for i in (1, 2, 3):
            r = p.get(url, f"{name} #{i}")
            bodies[name].append(r.text)
            p.save(f"raw_{name}_{i}.html", r.text)

    print("\n  raw byte sizes (per fetch):")
    for k, v in bodies.items():
        print(f"    {k:<14} {[len(b) for b in v]}")

    print("\n  per-transform substitution counts, coreA #1:")
    for name, rx, _rep in TRANSFORMS:
        print(f"    {name:<24} matches={len(rx.findall(bodies['coreA'][0]))}")

    hashes: dict[str, list[str]] = {}
    for g, group in bodies.items():
        hashes[g] = [normhash(b) for b in group]
        verdict = "STABLE" if len(set(hashes[g])) == 1 else "UNSTABLE"
        print(f"\n  {g:<14} {verdict} (with all six classes)")
        for h in hashes[g]:
            print(f"      sha256={h}")

    # The comparison that matters: the five-class recipe leaves core_tasks
    # unstable.  Show it rather than asserting it.
    print("\n  same bodies, the original FIVE classes only (no S3 signed-query):")
    for g, group in bodies.items():
        hs = [normhash(b, WITHOUT_F) for b in group]
        verdict = "STABLE" if len(set(hs)) == 1 else "UNSTABLE"
        print(f"    {g:<14} {verdict}  distinct={len(set(hs))}")

    # Completeness.  If the normalized hash is identical across every fetch of
    # every page, then not one byte outside a transformed span varies - so every
    # raw difference between two renders lies inside one of classes (a)-(f), and
    # there is no seventh nonce class at this timescale.  State it rather than
    # leaving the reader to infer it.
    if all(len(set(v)) == 1 for v in hashes.values()):
        print("\n  COMPLETENESS: every page's normalized hash is identical across")
        print("  its fetches, so no byte outside a transformed span varies and every")
        print("  raw difference lies inside one of classes (a)-(f).")
        print("  => no seventh nonce class at this timescale.")
        print("  Caveat: three closely-spaced fetches cannot rule out a nonce on a")
        print("  longer cadence (per-session, per-hour, per-day).")

    head("Q3 - normalized-hash sensitivity")
    names = list(hashes)
    if len(names) >= 2:
        print(f"  (i)   {names[0]}[0] vs {names[1]}[0] differ? "
              f"{hashes[names[0]][0] != hashes[names[1]][0]}   <-- MUST be True")
    tad = [n for n in names if n.startswith("tad_")]
    if len(tad) >= 2:
        print(f"  (ii)  {tad[0]}[0] vs {tad[1]}[0] differ? "
              f"{hashes[tad[0]][0] != hashes[tad[1]][0]}   <-- MUST be True")
    print(f"  (iii) same URL x3 matches? "
          f"{all(len(set(v)) == 1 for v in hashes.values())}   <-- MUST be True")

    # Negative control: what the hash CANNOT see.
    demo = bodies["coreA"][0]
    print("\n  negative control - changes that MUST be invisible:")
    cases = [
        ("applicationTime changed",
         re.sub(r'"applicationTime":[0-9]+', '"applicationTime":999999', demo)),
        ("csrf-token replaced",
         re.sub(r'(<meta name="csrf-token" content=")[^"]*(")',
                r'\1DIFFERENT\2', demo)),
        ("data-token (hub JWT) replaced",
         re.sub(r'(data-token=")[^"]*(")',
                r'\1SYNTHETIC-NOT-A-REAL-JWT\2', demo)),
        ("S3 signed-query replaced",
         re.sub(r"\?X-Amz-[^)\"']*",
                "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=1", demo)),
    ]
    for label, variant in cases:
        seen = "INVISIBLE" if normhash(demo) == normhash(variant) else "detected"
        print(f"    {label:<32} {seen}")

    # Over-strip check: is real content preserved?
    print("\n  over-strip check - real content must survive:")
    n = normalize(demo)
    for label, rx in [
        ("task card links", re.compile(r'/student/classes/\d+/core_tasks/\d+')),
        ("grade letters", re.compile(r'>\s*([A-F]|N/A)\s*<')),
        ("autologout meta tag survives", re.compile(r"<meta content='NR' name='autologout'")),
        ("csrf meta tag survives", re.compile(r'<meta name="csrf-token" content="NR"')),
    ]:
        print(f"    {label:<32} raw={len(rx.findall(demo)):>4}  "
              f"normalized={len(rx.findall(n)):>4}")

    return bodies


# ── Q4: dropbox content negotiation ──────────────────────────────────────

def q4_dropbox(p: Probe, bodies: dict[str, list[str]]) -> None:
    head("Q4 - dropbox content negotiation")
    tid = None
    for mm in re.finditer(r'/student/classes/\d+/core_tasks/(\d+)/dropbox',
                          bodies["coreA"][0]):
        tid = mm.group(1)
        break
    if tid is None:
        # No task on this account has a dropbox.  Fall back to the first task
        # id so the request is still made, and say so.
        print("  note: no /dropbox link found in any core_tasks page for this")
        print("        account; falling back to a bare task id")
        for mm in re.finditer(r'/student/classes/\d+/core_tasks/(\d+)',
                              bodies["coreA"][0]):
            tid = mm.group(1)
            break
    if not tid:
        print("  !! no task id found at all - skipping Q4")
        return

    class_id = re.search(r'/student/classes/(\d+)/core_tasks',
                         bodies["coreA"][0]).group(1)
    dbx = f"{p.client.base}/student/classes/{class_id}/core_tasks/{tid}/dropbox"
    print(f"  target: class={class_id} task={tid}")

    js = p.get(dbx, "dropbox AS JS (X-Requested-With: XMLHttpRequest)",
               headers={"X-Requested-With": "XMLHttpRequest"})
    p.save("dropbox_js.txt", js.text)
    print(f"  -> JS  bytes={len(js.content)} ct={js.headers.get('Content-Type')}")

    html = p.get(dbx, "dropbox AS HTML (no X-Requested-With)")
    p.save("dropbox_html.html", html.text)
    print(f"  -> HTML bytes={len(html.content)} ct={html.headers.get('Content-Type')}")
    print(f"  -> byte-identical? {js.content == html.content}")

    # Does the project ever send X-Requested-With on a dropbox GET?  Static
    # answer, no request needed.
    print("\n  which representation each caller receives (static analysis):")
    print("    ManageBacClient._get passes no headers= kwarg;")
    print("    _request_with_retry adds only Referer; session.headers holds")
    print("    only User-Agent.  So no code path sends X-Requested-With on a")
    print("    dropbox GET, and all three callers receive the HTML body.")


# ── main ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None,
                    help="evidence directory (default: a fresh temp dir). "
                         "Holds live AWS/JWT credentials - delete when done.")
    args = ap.parse_args()

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="tahuti-probe-"))
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    print(f"evidence dir: {out}  (0700; treat as a credential)")

    head("STAGE 0 - build_client() (no arguments)")
    state, client, email = build_client()
    print(f"  base={client.base}")
    print(f"  email_domain={email.split('@')[-1] if '@' in email else '?'} "
          f"(local part withheld)")
    print(f"  session cookie present: "
          f"{bool(client.session.cookies.get('_managebac_session'))} "
          f"(value never printed)")
    print(f"  response cache enabled={client.cache.enabled} ttl={client.cache.ttl}")
    p = Probe(client, out)

    head("STAGE 1 - class discovery")
    dash = p.get(f"{client.base}/student/dashboard", "dashboard")
    if "/login" in dash.url:
        print("  !! SESSION DEAD - aborting")
        return 2
    p.save("raw_dashboard.html", dash.text)
    class_ids: list[str] = []
    for m in re.finditer(r'/student/classes/(\d+)(?:[/"?]|$)', dash.text):
        if m.group(1) not in class_ids:
            class_ids.append(m.group(1))
    print(f"  distinct class ids, in order of appearance: {len(class_ids)} found")

    q1_calendar(p)
    bodies = q2_q3_hash(p, class_ids)
    q4_dropbox(p, bodies)

    head("REQUEST LEDGER")
    print(f"  total HTTP requests issued by this script: {len(LEDGER)}")
    (out / "requests.json").write_text(json.dumps(LEDGER, indent=1))
    os.chmod(out / "requests.json", 0o600)
    print(f"  evidence written to {out}")
    print("  reminder: that directory holds a live AWS STS token and a hub JWT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
