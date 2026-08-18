"""Enumeration and planning.

Two halves.

`enumerate_cell` turns a server-side query region into a complete set of deck
ids, subdividing until every piece fits inside the 10,000-result window and
recording in the ledger how each piece was closed. The ladder is

    exact (<10k)  ->  asc+desc  ->  other sort keys
                  ->  fmt  ->  bracket  ->  authorUserNames batches

with author last because it is the universal one: every deck has a creator, the
parameter takes comma-separated batches, and it works in formats that have no
command zone to split on.

`solve` turns a query AST into set algebra over those sets -- AND intersects,
OR unions, NOT subtracts -- so a deck body is fetched only for the one thing a
row and the server cannot settle between them: copy counts.
"""

from __future__ import annotations

import random
import urllib.error
from dataclasses import dataclass, field

import query as Q
from api import PAGE_SIZE, RATE, WINDOW, QueryError

# Long sweeps race deck churn, so a cover is accepted a little short of perfect
# rather than refused for movement it cannot control.
COVER_TOLERANCE = 0.97
MAX_URL_NAMES = 120        # authorUserNames batch size (URL length, not a server cap)
# `bracket` is in the frontend's sortType enum but the search API answers 400
# for it in every configuration, so it is not an ordering we can page.
SORTS = ("created", "views", "likes", "updated")


class Est:
    """A stand-in for an id set while planning: it knows its size, not its
    members. Lets `solve` cost a query without fetching anything."""

    def __init__(self, n: int):
        self.n = n

    def __len__(self):
        return self.n

    def __and__(self, o):
        return Est(min(len(self), len(o)))

    def __or__(self, o):
        return Est(len(self) + len(o))

    def __sub__(self, o):
        return Est(len(self))


@dataclass
class Result:
    ids: set
    status: str            # exact | unverified
    note: str = ""
    requests: int = 0      # estimated, when planning
    floor: bool = False    # True when `requests` is only a lower bound


@dataclass
class Plan:
    ids: set | None                 # candidate superset; None = cannot generate
    residual: list = field(default_factory=list)   # terms still to check locally
    status: str = "exact"
    notes: list = field(default_factory=list)
    est: int = 0                    # estimated requests to enumerate
    floor: bool = False             # est is a lower bound (something subdivides)

    @property
    def needs_body(self) -> bool:
        """Any card term left in the residual has to be settled from the card
        list, including one under a negation."""
        return any(_has_card(r) for r in self.residual)

    @property
    def budget(self) -> int:
        """Total requests to expect: enumeration, plus one deck body per
        candidate if a residual term needs the card list."""
        bodies = len(self.ids) if (self.needs_body and self.ids is not None) else 0
        return self.est + bodies

    def cost(self) -> str:
        mins = self.budget / RATE / 60
        return (f"{'at least ' if self.floor else ''}{self.budget:,} requests"
                f" (~{mins:.0f} min at {RATE} req/s)")


class Engine:
    def __init__(self, client, store, on_event=None, dry=False):
        self.c, self.s = client, store
        self.dry = dry          # plan only: count cells, fetch nothing
        self.on_event = on_event or (lambda kind, **kw: None)

    def ev(self, kind, **kw):
        self.on_event(kind, **kw)

    # ================================================================ cells

    def enumerate_cell(self, params: dict, refresh: bool = False) -> Result:
        """Every deck id in a server-side region, subdividing as needed."""
        if self.dry:
            n = self.c.count(**params)
            note = f"{brief(params)} -> {n}{'+' if n >= WINDOW else ''}"
            if n < WINDOW:
                return Result(Est(n), "exact", note, requests=_pages(n) + 1)
            # A saturated region costs at least a count per format plus a full
            # window scan; what the split beyond that costs is not knowable
            # until the counts come back, so this is reported as a floor.
            return Result(Est(n), "subdivides", f"{note}  (subdivides)",
                          requests=len(Q.FORMATS) + 6 * (WINDOW // PAGE_SIZE),
                          floor=True)
        cached = self.s.cell(params)
        if cached and not refresh:
            ids = self.s.cell_members(self.s.cell_key(params))
            self.ev("cell", params=params, status=cached["status"], n=len(ids),
                    note="cached")
            return Result(ids, cached["status"], f"{cached['evidence']} (cached)")

        out: dict = {}
        status, note = self._collect(params, self._axes(params), out, 0)
        self.s.close_cell(params, status, note, len(out))
        self.s.put_cell_members(self.s.cell_key(params), out)
        return Result(set(out), status, note)

    @staticmethod
    def _axes(params: dict) -> list:
        """Only axes the query has not already pinned, cheapest first."""
        axes = []
        if "fmt" not in params:
            axes.append("fmt")
        if not ({"minBracket", "maxBracket"} & set(params)):
            axes.append("bracket")
        if "authorUserNames" not in params:
            axes.append("author")
        return axes

    def _collect(self, params: dict, axes: list, out: dict, depth: int) -> tuple:
        total = self.c.count(**params)
        self.ev("cell", params=params, n=total, depth=depth, status="counting")
        if total == 0:
            return "exact", "empty"
        # `total` saturates at WINDOW, so hitting it means "at least this many".
        # Using <= here would drain one window and never subdivide -- the
        # difference between 10k of a card's decks and all of them.
        if total < WINDOW:
            self._drain(params, total, out)
            return "exact", f"count {total} < window, drained"

        for i, axis in enumerate(axes):
            rest = axes[i + 1:]
            if axis in ("fmt", "bracket"):
                key_lo, key_hi, values = (
                    ("fmt", None, Q.FORMATS) if axis == "fmt"
                    else ("minBracket", "maxBracket", [1, 2, 3, 4, 5]))
                children = self._try_axis(params, total, key_lo, key_hi, values)
                if children is None:
                    continue
                worst = "exact"
                for child in children:
                    st, _ = self._collect(child, rest, out, depth + 1)
                    if st != "exact":
                        worst = st
                return worst, f"split on {axis} ({len(children)} cells)"
            if axis == "author":
                got = self._by_author(params, total, out, depth)
                if got is not None:
                    return got

        # Nothing left to split on. Descending walks the newest 10k and ascending
        # the oldest, so they meet in the middle exactly when the region fits in
        # 20k -- testing whether they *overlap* is what makes this exact. A count
        # threshold under-fires (a full drain lands short of 20,000 through
        # deleted decks) and a pages-consumed test over-fires.
        if self._drain(params, total, out):
            return "unverified", f"exhausted every window at {len(out)} decks"
        return "exact", f"windows met at {len(out)} decks"

    def _walk(self, sort, direction, pages, params, out, on_row=None):
        """Page one ordering into `out`, or None if the server rejects it.

        A crawl runs for hours; one bad request must not take it down. But an
        ordering that silently returns nothing would truncate a window and lose
        decks, so a rejection is reported as *unavailable* and the caller
        decides what that costs it.
        """
        ids = set()
        for p in range(1, pages + 1):
            try:
                rows = self.c.page(p, sort, direction, **params)
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                self.ev("note", text=f"sortType={sort} rejected (400); "
                                     f"skipping that ordering")
                return None
            if not rows:
                break
            for r in rows:
                ids.add(r["publicId"])
                out[r["publicId"]] = r
                if on_row:
                    on_row(r)
            self.ev("page", n=len(out))
        return ids

    def _drain(self, params: dict, total: int, out: dict) -> bool:
        """Page a region. Returns True if it is provably incomplete."""
        saturated = total >= WINDOW
        pages = (min(total, WINDOW) + PAGE_SIZE - 1) // PAGE_SIZE

        def walk(sort, direction):
            return self._walk(sort, direction, pages, params, out)

        desc = walk("created", "descending")
        if desc is None:
            # Without `created` there is no overlap test, so completeness
            # cannot be established here whatever else we page.
            return True
        if not saturated:
            return False
        asc = walk("created", "ascending")
        if asc is None:
            return True

        # Every sort key is an *independent* 10k window onto the same region, not
        # a different view of one window: measured 45,057 distinct decks across
        # three keys where `created` alone reached 19,999.
        grew = True
        for sort in SORTS[1:]:
            before = len(out)
            for direction in ("descending", "ascending"):
                walk(sort, direction)
            grew = len(out) > before
        return bool(not (desc & asc) and grew)

    def _try_axis(self, params, total, key_lo, key_hi, values) -> list | None:
        """Split, but only if the split provably loses nothing -- otherwise the
        axis does not partition this region (brackets outside Commander) and we
        refuse it rather than silently dropping decks."""
        children, subtotal = [], 0
        for v in values:
            child = dict(params, **{key_lo: v})
            if key_hi:
                child[key_hi] = v
            n = self.c.count(**child)
            if n:
                children.append(child)
                subtotal += n
        # Above the cap the parent's true count is unknown and merely >= total,
        # which is exactly when we need to subdivide -- so require equality only
        # where the parent count is exact.
        ok = subtotal >= total if total >= WINDOW else subtotal == total
        if not ok:
            self.ev("note", text=f"axis {key_lo} sums to {subtotal}, expected {total}"
                                 f" -- skipping")
            return None
        return children

    # ---- the universal axis: authors ---------------------------------------

    def _by_author(self, params: dict, total: int, out: dict, depth: int):
        """Split a saturated region by deck author.

        Author is a true partition -- one creator per deck -- and unlike the
        command zone it exists in every format. `authorUserNames` takes
        comma-separated batches that return the union exactly, so a batch of a
        hundred users costs one query rather than a hundred.

        The authors are harvested from the region's own windows: paging them is
        not overhead (those decks belong in the answer) and every row names its
        creator, so we only ask about users who actually occur. The cover is then
        measured against a direct sample of the parent.
        """
        authors, halves = self._window_scan(params, total, out)
        if total < WINDOW and len(out) >= total:
            return "exact", f"windows covered {len(out)} decks"
        # The window scan has already paid for the overlap test, and that test is
        # *exact* where a sweep is only a sampled cover: descending walks the
        # newest 10k and ascending the oldest, so if they meet in the middle the
        # region provably fits inside them and there is nothing left to sweep
        # for. Cheaper and stronger, so it is checked first.
        if halves[0] & halves[1]:
            return "exact", (f"created windows met at {len(out)} decks; "
                             f"no author sweep needed")
        if not authors:
            return None
        self.ev("note", text=f"windows gave {len(out)} decks naming "
                             f"{len(authors)} authors")

        before = len(out)   # measure what the *sweep* adds, not the windows
        batches = [sorted(authors)[i:i + MAX_URL_NAMES]
                   for i in range(0, len(authors), MAX_URL_NAMES)]
        capped, seen = False, 0
        for i, b in enumerate(batches, 1):
            hit, rows = self._sweep_batch(params, b, out)
            capped |= hit
            seen += rows
            self.ev("note", text=f"author batch {i}/{len(batches)}, "
                                 f"{len(out)} decks (+{len(out) - before})")

        caught = self._sample_cover(params, out)
        found = len(out) - before
        if not seen:
            # The axis returned no rows at all, so it is not available here (the
            # parameter does not apply, or the harvest was empty). That is a
            # different thing from returning rows we already had -- fall through
            # and let the overlap test decide.
            self.ev("note", text="author sweep returned nothing -- axis unavailable")
            return None
        if caught < COVER_TOLERANCE:
            self.ev("note", text=f"author cover only caught {caught:.1%} -- refusing")
            return None
        note = (f"author sweep: {len(authors)} authors, {seen:,} rows, "
                f"+{found} decks, {caught:.1%} sampled")
        return ("unverified" if capped else "exact"), note

    def _sweep_batch(self, params: dict, names: list, out: dict) -> tuple:
        """Page one batch of authors, returning (hit the window, rows seen).

        A short page is the complete answer, so no count request is needed. A
        batch that fills the window is halved.

        The row count matters even when every row is already known: a sweep that
        returns decks we had is the axis *corroborating* the windows, while a
        sweep that returns nothing at all means the axis does not apply here.
        """
        p = dict(params, authorUserNames=",".join(names))
        seen = 0
        for pn in range(1, WINDOW // PAGE_SIZE + 1):
            rows = self.c.page(pn, "created", "descending", **p)
            seen += len(rows)
            for r in rows:
                out[r["publicId"]] = r
            if len(rows) < PAGE_SIZE:
                return False, seen
        if len(names) > 1:
            half = len(names) // 2
            a, na = self._sweep_batch(params, names[:half], out)
            b, nb = self._sweep_batch(params, names[half:], out)
            return a or b, seen + na + nb
        return True, seen  # one user with a full window; vanishingly unlikely

    def _window_scan(self, params: dict, total: int, out: dict) -> tuple:
        """Page every window the API gives for this region.

        Returns the authors that occur in it and the two `created` halves, which
        is everything the caller needs: the decks belong in the answer, every row
        names its creator, and descending-vs-ascending is the overlap test.

        Each sort key is a *different* 10k window onto the same region, so
        scanning several reaches decks no single ordering can -- a deck stranded
        in the middle of `created` order sits somewhere quite different under
        `views`. Harvest depth follows the region, not a constant: a fixed
        handful of pages misses whatever lives deeper, and with it every author
        who only appears there.
        """
        pages = (min(total, WINDOW) + PAGE_SIZE - 1) // PAGE_SIZE
        seen: set = set()
        halves = {"descending": set(), "ascending": set()}

        def note(r):
            u = (r.get("createdByUser") or {}).get("userName")
            if u:
                seen.add(u)

        for sort in ("created", "views", "likes"):
            for direction in ("descending", "ascending"):
                ids = self._walk(sort, direction, pages, params, out, note)
                if ids is None:
                    continue
                if sort == "created":
                    halves[direction] = ids
                if total < WINDOW and len(out) >= total:
                    return seen, (halves["descending"], halves["ascending"])
        return seen, (halves["descending"], halves["ascending"])

    def _sample_cover(self, params: dict, out: dict, n_pages: int = 8) -> float:
        """What fraction of decks drawn straight from the parent did we get?

        Comparing counts cannot work: a sweep takes hours and decks are created
        and deleted throughout, so a union assembled over that window never
        matches a count taken at the start of it. Sampling measures the cover
        rather than the drift, and still works when the parent is saturated --
        which is the only time this runs.

        Page 1 is skipped: sorted newest-first, the front of the window is where
        decks created *during* the sweep land, so sampling it measures churn. The
        pages are random rather than at fixed offsets, and half are drawn by
        `views` instead of `created`, because a block of decks contiguous in one
        ordering is scattered through the other.
        """
        top = WINDOW // PAGE_SIZE
        seen = hit = 0
        for sort in ("created", "views"):
            for p in random.sample(range(2, top), min(n_pages // 2, top - 2)):
                for r in self.c.page(p, sort, "descending", **params):
                    seen += 1
                    hit += r["publicId"] in out
        if seen == 0:
            for r in self.c.page(1, "created", "descending", **params):
                seen += 1
                hit += r["publicId"] in out
        return hit / seen if seen else 1.0

    # ---- sorted-prefix generator -------------------------------------------

    def prefix_enum(self, refs: dict, key: str, op: str, value) -> Result:
        """`likes>50` and friends, enumerated directly.

        There is no range parameter, but `sortType` gives a sorted prefix of the
        region, so a lower bound is a complete generator: page descending and
        stop at the first row past the bound. Complete unless the qualifying
        prefix is itself longer than the window."""
        sort = Q.PREFIX_SORT[key]
        if self.dry:
            n = self.c.count(**refs)
            # Where it stops depends on the data, so the whole window is the
            # only honest ceiling -- but it is a ceiling, not a floor.
            return Result(Est(min(n, WINDOW)), "exact",
                          f"{key}{op}{value} as a sorted prefix of {n}",
                          requests=_pages(min(n, WINDOW)) + 1)
        out: dict = {}
        for p in range(1, WINDOW // PAGE_SIZE + 1):
            rows = self.c.page(p, sort, "descending", **refs)
            if not rows:
                return Result(set(out), "exact", f"{key}{op}{value} prefix ({len(out)})")
            for r in rows:
                row = {"views": r.get("viewCount"), "likes": r.get("likeCount"),
                       "created": r.get("createdAtUtc")}[key]
                if row is None or not Q.compare(
                        row[:len(value)] if key == "created" else row, op, value):
                    return Result(set(out), "exact",
                                  f"{key}{op}{value} prefix ({len(out)})")
                out[r["publicId"]] = r
            self.ev("page", n=len(out))
        return Result(set(out), "unverified",
                      f"{key}{op}{value} filled the window at {len(out)}")

    # ================================================================ planning

    def solve(self, node, refs: dict | None = None) -> Plan:
        refs = refs or {}
        if isinstance(node, Q.CardTerm):
            r = self.enumerate_cell({**refs, **node.params()})
            # A copy count is the one thing neither the row nor the server can
            # settle, so it stays as a residual and costs deck bodies.
            resid = [node] if node.qty > 1 else []
            return Plan(r.ids, resid, r.status, [r.note], r.requests, r.floor)
        if isinstance(node, (Q.Refine, Q.Local)):
            return Plan(None, [node])
        if isinstance(node, Q.Not):
            return Plan(None, [node])
        if isinstance(node, Q.Or):
            ids, resid, status, notes = None, [], "exact", []
            est, floor = 0, False
            for ch in node.children:
                p = self.solve(ch, refs)
                if p.ids is None:
                    return Plan(None, [node])
                ids = p.ids if ids is None else ids | p.ids
                resid += p.residual
                notes += p.notes
                est += p.est
                floor |= p.floor
                if p.status != "exact":
                    status = p.status
            # A residual under OR cannot be checked branch-by-branch, so the
            # whole disjunction is re-checked locally over the union.
            return Plan(ids, [node] if resid else [], status, notes, est, floor)
        if isinstance(node, Q.And):
            return self._solve_and(node, refs)
        return Plan(None, [node])

    def _solve_and(self, node, refs: dict) -> Plan:
        local = dict(refs)
        for ch in node.children:
            if isinstance(ch, Q.Refine):
                local.update(_refine_params(ch))
        # Command-zone terms occupy their own server params, so they stack with a
        # sibling's cardId instead of competing with it: `cmdr:X card:Y` becomes
        # the small card query narrowed by the commander, not the huge one.
        for ch in node.children:
            if isinstance(ch, Q.CardTerm):
                slot = _zone_slot(local, ch.param, ch.card_id)
                if slot:
                    local[slot] = ch.card_id

        def settled(ch) -> bool:
            """Already enforced by the server params every candidate carries."""
            return (isinstance(ch, Q.CardTerm) and ch.qty == 1
                    and all(local.get(k) == v for k, v in ch.params().items()))

        checks = [c for c in node.children if isinstance(c, (Q.Refine, Q.Local))]
        pos = [c for c in node.children
               if not isinstance(c, (Q.Refine, Q.Local, Q.Not)) and not settled(c)]
        neg = [c for c in node.children if isinstance(c, Q.Not)]

        priced = sorted(((self._price(c, local), c) for c in pos),
                        key=lambda x: (x[0] is None, x[0] or 0))
        if priced and priced[0][0] is not None:
            first = priced[0][1]
            rest = priced[1:]
        else:
            # Nothing left to drive with. Either the pushed params are themselves
            # the driver (`cmdr:X` alone), or a sorted prefix is (`likes>1000`).
            first, rest = None, priced
            if not any(isinstance(c, Q.CardTerm) for c in node.children):
                gen = self._prefix_option(node, local)
                if gen is None:
                    return Plan(None, list(node.children))
                seed = Plan(gen.ids, [], gen.status, [gen.note],
                            gen.requests, gen.floor)
            else:
                r = self.enumerate_cell(local)
                seed = Plan(r.ids, [], r.status, [r.note], r.requests, r.floor)

        if first is not None:
            seed = self.solve(first, local)
            if seed.ids is None:
                return Plan(None, list(node.children))

        ids, status = seed.ids, seed.status
        resid, notes = list(seed.residual), list(seed.notes)
        est, floor = seed.est, seed.floor
        # Pricing a term is itself a request, and it happens whether or not the
        # term is then enumerated.
        est += len(priced) + len(neg)

        # Intersecting costs a page per 100 decks; verifying costs a body per
        # deck. Take whichever is cheaper for what is left -- and note `ids`
        # shrinks as we go, so each intersection makes the next one less
        # attractive and the bodies cheaper.
        for cost, ch in rest:
            if cost is not None and _enum_cost(cost) < len(ids):
                q = self.solve(ch, local)
                # Intersecting with a set we could not enumerate completely
                # would drop real matches, so an unverified term falls back to
                # being checked against deck bodies, which is exact.
                if q.ids is not None and q.status != "unverified":
                    ids = ids & q.ids
                    resid += q.residual
                    notes += q.notes
                    est += q.est
                    floor |= q.floor
                    continue
            resid.append(ch)

        # A negation is a set difference -- which is what makes `-card:X`
        # searchable at all, rather than a filter needing every candidate's body.
        for n in neg:
            cost = self._price(n.child, local)
            if cost is not None and _enum_cost(cost) < len(ids):
                q = self.solve(n.child, local)
                if q.ids is not None and q.status != "unverified":
                    ids = ids - q.ids
                    notes += q.notes
                    est += q.est
                    floor |= q.floor
                    continue
            resid.append(n)

        return Plan(ids, resid + checks, status, notes, est, floor)

    def _price(self, node, refs: dict):
        """Rough deck count for an enumerable child, or None."""
        if isinstance(node, Q.CardTerm):
            return self.c.count(**{**refs, **node.params()})
        if isinstance(node, Q.Or):
            subs = [self._price(c, refs) for c in node.children]
            return None if any(s is None for s in subs) else sum(subs)
        if isinstance(node, Q.And):
            subs = [s for s in (self._price(c, refs) for c in node.children)
                    if s is not None]
            return min(subs) if subs else None
        return None

    def _prefix_option(self, node, refs: dict) -> Result | None:
        for ch in node.children:
            if (isinstance(ch, Q.Local) and ch.key in Q.PREFIX_SORT
                    and ch.op in (">", ">=")):
                return self.prefix_enum(refs, ch.key, ch.op, ch.value)
        return None

    # ================================================================ running

    def iter_matches(self, ast, plan: Plan | None = None):
        """Yield every matching deck row. Bodies are fetched only for residual
        terms a row cannot answer."""
        plan = plan or self.solve(ast)
        if plan.ids is None:
            raise QueryError(
                "this query cannot be searched: nothing in it can generate "
                "candidates.\nAdd a positive card or commander term outside any "
                "'or' branch, or a lower bound like likes>100.")
        resid = Q.And(plan.residual) if len(plan.residual) > 1 else (
            plan.residual[0] if plan.residual else None)
        need_body = plan.needs_body
        ids = sorted(plan.ids)
        # Rows are loaded in chunks so a million-deck answer neither blocks on a
        # single query nor holds the whole corpus in memory.
        i = 0
        for start in range(0, len(ids), 2000):
            chunk = ids[start:start + 2000]
            rows = self.s.rows(chunk)
            for pid in chunk:
                i += 1
                row = rows.get(pid)
                if row is None:
                    continue
                if resid is not None:
                    body = self.c.body(pid) if need_body else None
                    if need_body and body is None:
                        continue
                    if not Q.evaluate(resid, row, body):
                        self.ev("checked", i=i, total=len(ids))
                        continue
                self.ev("checked", i=i, total=len(ids))
                yield row


    # ================================================================ crawling

    def tail(self, max_pages: int = 100) -> dict:
        """Ingest the global new-deck stream.

        The newest 10,000 decks span about 18 hours, so polling `created`
        descending more often than that sees every deck ever created from now
        on. A run of pages that are entirely already-known decks is the overlap
        proof: it means the stream never ran away from us.

        Two such pages, not one, because the search index is served by replicas
        of differing freshness -- page 1 can go backwards between calls -- so a
        single known page is not enough to conclude we are at the front.
        """
        new = seen = quiet = 0
        for p in range(1, max_pages + 1):
            rows = self.c.page(p, "created", "descending")
            if not rows:
                break
            fresh = len(self.c.new_ids)
            new += fresh
            seen += len(rows)
            quiet = 0 if fresh else quiet + 1
            self.ev("page", n=seen)
            if quiet >= 2:
                return {"new": new, "seen": seen, "caught_up": True}
        return {"new": new, "seen": seen, "caught_up": False}

    def sweep_users(self, limit: int = 1000) -> dict:
        """Enumerate every deck of users we know about but have not swept.

        A single user is always far below the window, so their deck list is
        exact -- which is what makes the author axis a proof rather than an
        estimate. Batched, so a hundred users cost one query.
        """
        names = self.s.unswept_users(limit)
        found, partial = 0, 0
        for i in range(0, len(names), MAX_URL_NAMES):
            batch = names[i:i + MAX_URL_NAMES]
            out: dict = {}
            capped, _ = self._sweep_batch({}, batch, out)
            per: dict = {n: 0 for n in batch}
            for r in out.values():
                u = (r.get("createdByUser") or {}).get("userName")
                if u in per:
                    per[u] += 1
            for n, k in per.items():
                # Only a drained batch gives an exact per-user count. One that
                # filled the window was split, and a split we could not finish
                # leaves counts that would be wrong to record as exact.
                if not capped:
                    self.s.set_user_count(n, k)
            self.s.db.commit()
            found += len(out)
            partial += capped
            self.ev("note", text=f"swept {i + len(batch)}/{len(names)} users, "
                                 f"{found} decks")
        return {"users": len(names), "decks": found, "partial_batches": partial}


def _has_card(node) -> bool:
    if isinstance(node, (Q.And, Q.Or)):
        return any(_has_card(c) for c in node.children)
    if isinstance(node, Q.Not):
        return _has_card(node.child)
    return isinstance(node, Q.CardTerm)


def brief(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items()) or "(all decks)"


def _pages(count: int) -> int:
    return (min(count, WINDOW) + PAGE_SIZE - 1) // PAGE_SIZE


# A saturated cell costs a subdivision, not a window: a count per format plus a
# full window scan, before whatever the split itself costs. Same floor the
# planner reports, so the decision and the estimate agree.
SATURATED_COST = len(Q.FORMATS) + 6 * (WINDOW // PAGE_SIZE)


def _enum_cost(count: int) -> int:
    """Requests to enumerate a cell of this size.

    `_pages` caps at the window, which is exactly wrong for the decision this
    feeds: it prices a saturated cell at one window, so the planner will happily
    enumerate 10,000+ decks to avoid fetching 152 deck bodies.
    """
    return _pages(count) + 1 if count < WINDOW else SATURATED_COST


def _refine_params(r) -> dict:
    if r.op == "!=":
        return {}
    if r.key == "fmt":
        return {"fmt": r.value}
    if r.key == "author":
        return {"authorUserNames": r.value}
    if r.key == "deckname":
        return {"deckName": r.value}
    if r.key == "bracket":
        v = r.value
        return {":": {"minBracket": v, "maxBracket": v},
                "=": {"minBracket": v, "maxBracket": v},
                ">=": {"minBracket": v}, ">": {"minBracket": v + 1},
                "<=": {"maxBracket": v}, "<": {"maxBracket": v - 1}}.get(r.op, {})
    return {}


# commanderCardId and partnerCardId both mean "in the command zone" and return
# the same set, so a second command-zone term can take the free slot.
COMMAND_SLOTS = ("commanderCardId", "partnerCardId")


def _zone_slot(params: dict, param: str, value: str) -> str | None:
    """Which server param a command-zone term should occupy, or None if the slots
    are taken -- in which case we drop the constraint, since over-fetching stays
    sound and under-fetching would not."""
    if param == "cardId":
        return None
    order = ((param,) + tuple(s for s in COMMAND_SLOTS if s != param)
             if param in COMMAND_SLOTS else (param,))
    for s in order:
        if params.get(s, value) == value:
            return s
    return None
