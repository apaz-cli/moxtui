"""Moxfield API client: rate limiting, retries, card-name resolution.

See FINDINGS.md for the endpoint details this is built on.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api2.moxfield.com"

# Moxfield blocks the default urllib agent. They ask automated clients to request
# a dedicated User-Agent from support@moxfield.com; set MOXFIELD_UA once you have
# one, and keep the rate conservative regardless.
UA = os.environ.get(
    "MOXFIELD_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36",
)
# Measured 2026-08-18, 40 requests per leg: 1.4 -> 0.45 req/s with 3x429;
# 0.7 -> 0.34 with 3x429; 0.4 -> 0.41 req/s with *zero* 429s. Asking for 0.4
# therefore delivers ~91% of what hammering at 1.4 delivers, while never being
# throttled at all -- a 429 now costs `Retry-After: 20`, so one of them undoes
# 28 well-paced requests. Overshooting is self-defeating, not merely rude.
RATE = float(os.environ.get("MOXFIELD_RATE", "0.4"))

PAGE_SIZE = 100        # server caps here
WINDOW = 10000         # hard result cap: 100 pages x 100


class QueryError(Exception):
    pass


def norm(s: str) -> str:
    return " ".join(s.casefold().split())


def name_variants(full: str) -> set:
    """A split/DFC card is stored under "A // B" but users type either face."""
    parts = [p.strip() for p in full.split("//")]
    return {norm(full)} | {norm(p) for p in parts if p}


class Client:
    def __init__(self, store, log=None):
        self.store = store
        self.requests = 0
        self.throttled = 0      # 429s absorbed; the gap between configured and
                                # actual throughput is invisible without this
        self._counts: dict = {}
        self.new_ids: set = set()   # ids the last page() had not seen before
        self._last = 0.0
        self._interval = 1.0 / RATE
        self._log = log or (lambda *a: print(*a, file=sys.stderr, flush=True))

    def log(self, *a) -> None:
        self._log(" ".join(str(x) for x in a))

    def get(self, path: str, params: dict | None = None, tries: int = 8):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        for attempt in range(tries):
            gap = time.monotonic() - self._last
            if gap < self._interval:
                time.sleep(self._interval - gap)
            self._last = time.monotonic()
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.requests += 1
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.throttled += 1
                    time.sleep(int(e.headers.get("Retry-After") or 5) + attempt * 2)
                    continue
                if e.code in (500, 502, 503, 504):
                    time.sleep(2 + attempt * 2)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(2 + attempt * 2)
        raise RuntimeError(f"giving up on {url}")

    # ---- search ------------------------------------------------------------

    def count(self, **params) -> int:
        """Deck count for a cell. Saturates at WINDOW: 10000 means ">= 10000"."""
        key = tuple(sorted(params.items()))
        if key in self._counts:
            return self._counts[key]
        try:
            d = self.get("/v2/decks/search", {"pageNumber": 1, "pageSize": 1, **params})
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            # Some values the frontend knows are rejected by search (`fmt=tlr`).
            # Empty is safe: a non-empty bucket would fail the partition check.
            self._counts[key] = 0
            return 0
        self._counts[key] = d.get("totalResults", 0)
        return self._counts[key]

    def page(self, n: int, sort="created", direction="descending", **params) -> list:
        """One page of search rows, stored as they arrive.

        Always sort explicitly: the default `updated` sort is unstable -- decks
        are re-saved constantly, so deep pagination silently drops and duplicates
        rows underneath you.
        """
        d = self.get("/v2/decks/search", {
            "pageNumber": n, "pageSize": PAGE_SIZE,
            "sortType": sort, "sortDirection": direction, **params})
        rows = d.get("data") or []
        self.new_ids = self.store.put_rows(rows) if rows else set()
        return rows

    # ---- card names --------------------------------------------------------

    def resolve_card(self, name: str) -> tuple:
        """Card name -> (printing id, canonical full name).

        Exact and face-aware, and it refuses to guess: fuzzy search alone
        resolves "Fire" to "Curse of the Fire Penguin". The id is a *printing*
        id, which is what the deck search wants -- `uniqueCardId` lives in a
        different id space and silently matches an unrelated card.
        """
        want = norm(name)
        row = self.store.db.execute(
            "SELECT card_id, full_name FROM cards WHERE query = ?", (want,)
        ).fetchone()
        if row:
            return row[0], row[1]

        card = None
        try:
            hit = self.get("/v2/cards/lookup", {"name": name})
            if want in name_variants(hit.get("name", "")):
                card = hit
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                raise
        if card is None:
            data = self.get("/v3/cards/named", {"q": name, "count": 6})
            hints = ", ".join(dict.fromkeys(
                c.get("name", "") for c in (data.get("cards") or [])))
            raise QueryError(
                f"no card named {name!r}." + (f" Did you mean: {hints}?" if hints else ""))

        cid, full = card["id"], card["name"]
        self.store.db.execute(
            "INSERT OR REPLACE INTO cards VALUES (?,?,?,?)",
            (want, cid, full, "".join(sorted(card.get("color_identity") or []))))
        self.store.db.commit()
        return cid, full

    # ---- deck body ---------------------------------------------------------

    def body(self, pid: str) -> dict | None:
        """Card lists by board. ~550KB decoded; we keep only names and counts."""
        cached = self.store.body(pid)
        if cached is not None:
            return cached
        try:
            d = self.get(f"/v3/decks/all/{pid}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):  # deleted or made private
                return None
            raise
        boards = {}
        for bname, b in (d.get("boards") or {}).items():
            cards = [((e.get("card") or {}).get("name"), e.get("quantity", 1))
                     for e in (b.get("cards") or {}).values()]
            cards = [(n, q) for n, q in cards if n]
            if cards:
                boards[bname] = cards
        self.store.put_body(pid, boards)
        return boards
