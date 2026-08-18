"""Offline tests: parsing, evaluation, and the planner's set algebra.

No network. The planner is driven by a fake client whose `count` comes from a
table, which is enough to pin down every decision it makes.
"""

import os
import tempfile
import unittest
import urllib.error

import cardtypes as CT
import engine as E
import query as Q
from store import Store


class FakeClient:
    """Counts from a table; enumeration returns synthetic ids."""

    def __init__(self, counts, sets=None, store=None):
        self.counts, self.sets, self.store = counts, sets or {}, store
        self.requests = 0
        self.pages = []
        self.pages_params = []

    def resolve_card(self, name):
        return f"id:{name}", name

    def count(self, **p):
        self.requests += 1
        return self.counts.get(_key(p), 0)

    def page(self, n, sort="created", direction="descending", **p):
        self.requests += 1
        self.pages.append((n, sort, direction, _key(p)))
        self.pages_params.append(_key(p))
        ids = sorted(self.sets.get(_key(p), set()))
        return self._rows(ids[(n - 1) * 100:n * 100])

    def author_of(self, deck_id):
        return "u"

    def _rows(self, ids):
        rows = [{"publicId": i,
                 "createdByUser": {"userName": self.author_of(i) or "u"},
                 "viewCount": 0, "likeCount": 0, "createdAtUtc": "2026-01-01"}
                for i in ids]
        if self.store and rows:
            self.new_ids = self.store.put_rows(rows)
        return rows

    def body(self, pid):
        return {"mainboard": [("Lightning Bolt", 4)]}


def _key(p):
    return tuple(sorted((k, v) for k, v in p.items()))


def fixture(counts, sets=None):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    store = Store(path)
    c = FakeClient(counts, sets, store)
    return c, store, E.Engine(c, store), path


def scrap(store, path):
    store.close()
    os.unlink(path)


class Parsing(unittest.TestCase):
    def ast(self, text):
        return Q.resolve(Q.parse(text), FakeClient({}))

    def test_juxtaposition_is_and(self):
        a = self.ast('card:X card:Y')
        self.assertIsInstance(a, Q.And)
        self.assertEqual(len(a.children), 2)

    def test_or_and_negation(self):
        a = self.ast('(card:X or card:Y) -card:Z')
        self.assertIsInstance(a.children[0], Q.Or)
        self.assertIsInstance(a.children[1], Q.Not)

    def test_quantity_and_board(self):
        t = self.ast('main:"4x Lightning Bolt"')
        self.assertEqual((t.qty, t.board, t.name), (4, "mainboard", "Lightning Bolt"))

    def test_bare_quoted_name_is_a_card(self):
        self.assertIsInstance(self.ast('"Sol Ring"'), Q.CardTerm)

    def test_command_zone_scopes(self):
        self.assertEqual(self.ast('cmdr:X').param, "commanderCardId")
        self.assertEqual(self.ast('companion:X').param, "companionCardId")

    def test_format_aliases(self):
        self.assertEqual(self.ast('f:edh').value, "commander")
        self.assertEqual(self.ast('f:PDH').value, "pauperEdh")

    def test_errors(self):
        for bad in ('card:"unterminated', 'card:X)', '', 'wat:1'):
            with self.assertRaises(Q.QueryError):
                self.ast(bad)


class CardTypes(unittest.TestCase):
    """A card is listed under the first of its types, and every other section it
    belongs to says how many of its cards live elsewhere."""

    def test_multiple_types_pick_the_first_in_order(self):
        self.assertEqual(CT.primary("Legendary Artifact Creature — Kobold"),
                         "Creature")
        # Land outranks everything but creatures: anything that taps for mana
        # belongs in the part of the list you count.
        self.assertEqual(CT.primary("Enchantment Land — Urza's Saga"), "Land")
        self.assertEqual(CT.primary("Artifact Land"), "Land")
        self.assertEqual(CT.primary("Legendary Creature — Dryad // Land"),
                         "Creature")

    def test_both_faces_count(self):
        self.assertEqual(CT.types_of("Sorcery // Land"), ["Sorcery", "Land"])
        self.assertEqual(CT.primary("Instant // Land"), "Land")

    def test_supertypes_are_not_types(self):
        self.assertEqual(CT.types_of("Basic Land — Island"), ["Land"])
        self.assertEqual(CT.types_of("Legendary Planeswalker — Chandra"),
                         ["Planeswalker"])

    def test_tribal_is_kindred(self):
        self.assertEqual(CT.types_of("Tribal Instant — Zombie"),
                         ["Kindred", "Instant"])

    def test_the_rest_is_other(self):
        for line in ("Stickers", "Plane — Dominaria", "Scheme", "", None):
            self.assertEqual(CT.primary(line), "Other")

    def test_grouping_counts_and_sorting(self):
        #        name,       qty, colors, type_line,                      cmc
        deck = [("Bolt",       1, "R", "Instant",                          1),
                ("Sol Ring",   1, "",  "Artifact",                         1),
                ("Bridge",     1, "",  "Artifact Land",                    0),
                ("Saga",       1, "",  "Enchantment Land — Urza's Saga",   0),
                ("Island",     7, "",  "Basic Land — Island",              0),
                ("Kobold",     1, "R", "Legendary Artifact Creature — Kobold", 0),
                ("Ulamog",     1, "",  "Legendary Creature — Eldrazi",    11)]
        got = {t: ([e[0] for e in cards], extra) for t, cards, extra in CT.group(deck)}
        # Kobold is an Artifact Creature, so it lists under Creature and the
        # Artifact section notes it; same for the two odd lands.
        self.assertEqual(got["Creature"][0], ["Kobold", "Ulamog"])   # by cmc
        # Both odd lands land under Land, so Artifact and Enchantment only
        # report them -- Artifact also carries the Artifact Creature.
        self.assertEqual(got["Land"], (["Bridge", "Island", "Saga"], 0))
        self.assertEqual(got["Artifact"], (["Sol Ring"], 2))         # Kobold, Bridge
        self.assertEqual(got["Enchantment"], ([], 1))                # Saga
        self.assertNotIn("Planeswalker", got)                        # empty, absent

    def test_display_order_is_not_precedence_order(self):
        """Where a card is counted and where its section is shown are separate:
        a land is counted high so nothing hides from the land count, but shown
        last so it does not bury the spells."""
        self.assertLess(CT.RANK["Land"], CT.RANK["Artifact"])
        self.assertGreater(CT.DISPLAY.index("Land"), CT.DISPLAY.index("Artifact"))
        deck = [("Bridge", 1, "", "Artifact Land", 0),
                ("Bolt", 1, "R", "Instant", 1),
                ("Sol Ring", 1, "", "Artifact", 1)]
        got = [t for t, _, _ in CT.group(deck)]
        self.assertEqual(got, ["Instant", "Artifact", "Land"])
        counted = {t: [e[0] for e in c] for t, c, _ in CT.group(deck)}
        self.assertEqual(counted["Land"], ["Bridge"])     # counted as a land
        self.assertEqual(counted["Artifact"], ["Sol Ring"])

    def test_sorted_by_value_then_symbols_then_name(self):
        two = lambda name, colors: (name, 1, colors, "Instant", 2)
        deck = [two("zed", "G"), two("alpha", "U"), two("beta", "U"),
                two("gold", "UR"), two("rock", ""), two("white", "W"),
                ("cheap", 1, "B", "Instant", 1)]
        cards = dict((t, [e[0] for e in c]) for t, c, _ in CT.group(deck))
        self.assertEqual(cards["Instant"],
                         # cmc 1 first; then W U U G, multicolour, colourless;
                         # the two blues tie on symbols and fall back to name
                         ["cheap", "white", "alpha", "beta", "zed", "gold", "rock"])


class Evaluating(unittest.TestCase):
    row = {"format": "commander", "bracket": 4, "author": "apaz", "name": "Rog Storm",
           "likes": 30, "views": 900, "created": "2025-06-01T00:00:00Z",
           "updated": "2026-01-01T00:00:00Z", "hubs": "budget,combo",
           "color_identity": "BR"}
    body = {"mainboard": [("Lightning Bolt", 4), ("Bonecrusher Giant // Stomp", 1)],
            "commanders": [("Rograkh, Son of Rohgahh", 1)],
            "sideboard": [("Sol Ring", 1)]}

    def ok(self, text):
        ast = Q.resolve(Q.parse(text), FakeClient({}))
        return Q.evaluate(ast, self.row, self.body)

    def test_row_predicates(self):
        self.assertTrue(self.ok('f:edh bracket>=4 likes>10 by:apaz'))
        self.assertFalse(self.ok('f:modern'))
        self.assertTrue(self.ok('name:Storm hub:combo ci:BR'))
        self.assertTrue(self.ok('created>2025 updated>2025-12-31'))

    def test_board_scoping(self):
        self.assertTrue(self.ok('main:"Lightning Bolt"'))
        self.assertFalse(self.ok('main:"Sol Ring"'))
        self.assertTrue(self.ok('side:"Sol Ring"'))
        self.assertTrue(self.ok('card:"Sol Ring"'))          # any board
        self.assertTrue(self.ok('cmdr:"Rograkh, Son of Rohgahh"'))

    def test_faces(self):
        self.assertTrue(self.ok('card:Stomp'))
        self.assertTrue(self.ok('card:"Bonecrusher Giant"'))

    def test_quantities(self):
        self.assertTrue(self.ok('main:"4x Lightning Bolt"'))
        self.assertFalse(self.ok('main:"5x Lightning Bolt"'))

    def test_boolean(self):
        self.assertTrue(self.ok('(card:Nope or card:"Sol Ring") -card:Missing'))
        self.assertFalse(self.ok('card:"Sol Ring" -card:"Sol Ring"'))


class Planning(unittest.TestCase):
    def tearDown(self):
        scrap(self.store, self.path)

    def plan(self, text, counts, sets=None):
        self.c, self.store, eng, self.path = fixture(counts, sets)
        ast = Q.resolve(Q.parse(text), self.c)
        eng.dry = True
        return eng.solve(ast), ast

    def test_and_takes_the_cheapest_driver(self):
        p, _ = self.plan('card:A card:B', {
            (("cardId", "id:A"),): 5000,
            (("cardId", "id:B"),): 90,
        })
        self.assertEqual(len(p.ids), 90)

    def test_command_zone_stacks_with_card(self):
        """cmdr:X card:Y must plan as one narrowed query, not two."""
        p, _ = self.plan('cmdr:X card:Y', {
            (("commanderCardId", "id:X"),): 10000,
            (("cardId", "id:Y"),): 40000,
            (("cardId", "id:Y"), ("commanderCardId", "id:X")): 145,
        })
        self.assertEqual(len(p.ids), 145)
        self.assertEqual(p.residual, [])       # both terms settled server-side

    def test_or_unions_every_branch(self):
        p, _ = self.plan('(card:A or card:B)', {
            (("cardId", "id:A"),): 100, (("cardId", "id:B"),): 50})
        self.assertEqual(len(p.ids), 150)

    def test_refinements_are_pushed_down(self):
        p, _ = self.plan('f:edh bracket:4 card:A', {
            (("cardId", "id:A"),): 9000,
            (("cardId", "id:A"), ("fmt", "commander"), ("maxBracket", 4),
             ("minBracket", 4)): 300})
        self.assertEqual(len(p.ids), 300)

    def test_a_saturated_term_is_not_worth_intersecting(self):
        """With 152 candidates in hand, checking two saturated terms against
        deck bodies beats enumerating 10,000+ decks twice to intersect."""
        p, _ = self.plan('cmdr:X card:A card:B', {
            (("commanderCardId", "id:X"),): 10000,
            (("cardId", "id:A"), ("commanderCardId", "id:X")): 152,
            (("cardId", "id:B"), ("commanderCardId", "id:X")): 10000,
        })
        self.assertEqual(len(p.ids), 152)
        self.assertTrue(p.needs_body)
        self.assertLess(p.budget, 300)          # ~152 bodies, not ~1,290 pages
        self.assertEqual(p.status, "exact")     # and no cell left unenumerated

    def test_a_cheap_term_still_is_worth_intersecting(self):
        """The fix must not tip the other way: 30 pages beats 3,000 bodies."""
        p, _ = self.plan('card:A card:B', {
            (("cardId", "id:A"),): 3000,
            (("cardId", "id:B"),): 2900,
        })
        self.assertEqual(p.residual, [])        # both enumerated and intersected
        self.assertLess(p.budget, 200)

    def test_copy_count_stays_a_residual(self):
        p, ast = self.plan('main:"4x A"', {
            (("board", "mainboard"), ("cardId", "id:A")): 500})
        self.assertEqual(len(p.ids), 500)
        self.assertTrue(p.needs_body)

    def test_unstartable_query_is_refused(self):
        p, ast = self.plan('-card:A f:edh', {(("cardId", "id:A"),): 10})
        self.assertIsNone(p.ids)
        _, _, eng, _ = self.c, self.store, E.Engine(self.c, self.store), None
        with self.assertRaises(Q.QueryError):
            list(eng.iter_matches(ast, p))

    def test_lower_bound_can_start_a_search(self):
        p, _ = self.plan('f:edh likes>1000', {(("fmt", "commander"),): 10000})
        self.assertIsNotNone(p.ids)


class SetAlgebra(unittest.TestCase):
    """The real (non-dry) path: enumeration, intersection, difference."""

    def tearDown(self):
        scrap(self.store, self.path)

    def run_query(self, text, counts, sets):
        self.c, self.store, eng, self.path = fixture(counts, sets)
        ast = Q.resolve(Q.parse(text), self.c)
        return {r["public_id"] for r in eng.iter_matches(ast)}

    def test_intersection(self):
        A, B = {f"d{i}" for i in range(50)}, {f"d{i}" for i in range(40, 90)}
        got = self.run_query('card:A card:B', {
            (("cardId", "id:A"),): 50, (("cardId", "id:B"),): 50},
            {(("cardId", "id:A"),): A, (("cardId", "id:B"),): B})
        self.assertEqual(got, A & B)

    def test_difference(self):
        A, B = {f"d{i}" for i in range(50)}, {f"d{i}" for i in range(40, 90)}
        got = self.run_query('card:A -card:B', {
            (("cardId", "id:A"),): 50, (("cardId", "id:B"),): 50},
            {(("cardId", "id:A"),): A, (("cardId", "id:B"),): B})
        self.assertEqual(got, A - B)

    def test_union(self):
        A, B = {"d1", "d2"}, {"d2", "d3"}
        got = self.run_query('(card:A or card:B)', {
            (("cardId", "id:A"),): 2, (("cardId", "id:B"),): 2},
            {(("cardId", "id:A"),): A, (("cardId", "id:B"),): B})
        self.assertEqual(got, A | B)


class Ledger(unittest.TestCase):
    def test_cell_reuse(self):
        A = {f"d{i}" for i in range(30)}
        counts, sets = {(("cardId", "id:A"),): 30}, {(("cardId", "id:A"),): A}
        c, store, eng, path = fixture(counts, sets)
        try:
            first = eng.enumerate_cell({"cardId": "id:A"})
            n = c.requests
            second = eng.enumerate_cell({"cardId": "id:A"})
            self.assertEqual(first.ids, second.ids)
            self.assertEqual(c.requests, n)          # nothing re-fetched
            self.assertEqual(store.cell({"cardId": "id:A"})["status"], "exact")
        finally:
            scrap(store, path)


class WindowClient(FakeClient):
    """Models the real thing: a corpus of N decks, of which any one
    (sortType, sortDirection) shows only its own 10,000-row window."""

    def __init__(self, n, store):
        super().__init__({}, {}, store)
        self.corpus = [f"d{i:06d}" for i in range(n)]
        self.n = n
        # Each sort key is an independent ordering of the same decks.
        self.orders = {k: sorted(self.corpus, key=lambda c, k=k: hash((k, c)))
                       for k in SORT_OFFSET}

    def count(self, **p):
        self.requests += 1
        # No fmt/bracket split is available for this region, so the drain path
        # and its overlap test are what decide completeness.
        return 0 if set(p) - {"cardId"} else min(self.n, E.WINDOW)

    def author_of(self, deck_id):
        return None                         # no author axis available

    def page(self, n, sort="created", direction="descending", **p):
        self.requests += 1
        self.pages_params.append(_key(p))
        if "authorUserNames" in p:
            want = set(p["authorUserNames"].split(","))
            hits = [d for d in self.corpus if self.author_of(d) in want]
            return self._rows(hits[(n - 1) * 100:n * 100])
        order = self.orders[sort]
        if direction == "ascending":
            order = order[::-1]
        return self._rows(order[:E.WINDOW][(n - 1) * 100:n * 100])


SORT_OFFSET = {"created": 0, "views": 1, "likes": 2, "updated": 3, "bracket": 4}


class Windows(unittest.TestCase):
    """The completeness test itself: descending walks the newest 10k and
    ascending the oldest, so they overlap exactly when the region fits."""

    def cell(self, n):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        store = Store(path)
        eng = E.Engine(WindowClient(n, store), store)
        try:
            return eng.enumerate_cell({"cardId": "id:A"})
        finally:
            scrap(store, path)

    def test_fits_in_one_window(self):
        r = self.cell(9000)
        self.assertEqual(r.status, "exact")
        self.assertEqual(len(r.ids), 9000)

    def test_halves_overlap_so_it_is_complete(self):
        r = self.cell(15000)
        self.assertEqual(r.status, "exact")
        self.assertEqual(len(r.ids), 15000)

    def test_halves_come_apart_so_it_is_unverified(self):
        r = self.cell(60000)
        self.assertEqual(r.status, "unverified")
        self.assertGreater(len(r.ids), E.WINDOW)


class RejectingClient(WindowClient):
    """One ordering the server refuses, as `sortType=bracket` really does."""

    def page(self, n, sort="created", direction="descending", **p):
        if sort == "views":
            self.requests += 1
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        return super().page(n, sort, direction, **p)


class RejectedOrdering(unittest.TestCase):
    """A crawl runs for hours; one rejected ordering must not take it down."""

    def cell(self, cls, n):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        store = Store(path)
        try:
            return E.Engine(cls(n, store), store).enumerate_cell({"cardId": "id:A"})
        finally:
            scrap(store, path)

    def test_a_400_skips_the_ordering_not_the_crawl(self):
        r = self.cell(RejectingClient, 15000)
        self.assertEqual(r.status, "exact")
        self.assertEqual(len(r.ids), 15000)   # `created` still proves it

    def test_bracket_is_not_a_sort_key(self):
        self.assertNotIn("bracket", E.SORTS)


class AuthorClient(WindowClient):
    """Same windows, but every deck has one of 100 authors and the author
    parameter answers exactly -- which is the real shape of the axis."""

    def author_of(self, deck_id):
        return f"u{int(deck_id[1:]) % 100:03d}"


class AuthorAxis(unittest.TestCase):
    """The claim the whole design rests on: sweeping authors reaches decks no
    ordering can show, so a region past the window ceiling still closes."""

    def cell(self, n):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        store = Store(path)
        c = AuthorClient(n, store)
        try:
            return c, E.Engine(c, store).enumerate_cell({"cardId": "id:A"})
        finally:
            scrap(store, path)

    def test_sweep_passes_the_window_ceiling(self):
        c, r = self.cell(25000)
        self.assertEqual(r.status, "exact")
        self.assertEqual(len(r.ids), 25000)   # every deck, from a 10k-capped cell

    def test_overlap_short_circuits_the_sweep(self):
        """A region that fits inside the two `created` windows is proven complete
        by their overlap, which is exact -- so no sweep should be paid for."""
        c, r = self.cell(15000)
        self.assertEqual(r.status, "exact")
        self.assertEqual(len(r.ids), 15000)
        swept = [p for p in c.pages_params if "authorUserNames" in dict(p)]
        self.assertEqual(swept, [])

    def test_oversized_batches_are_halved(self):
        """A batch of 100 authors is 25,000 decks -- past the window -- so it
        has to split before it can be drained."""
        c, r = self.cell(25000)
        widths = {len(dict(p)["authorUserNames"].split(","))
                  for p in c.pages_params if "authorUserNames" in dict(p)}
        self.assertIn(100, widths)                    # the batch as first tried
        self.assertTrue(any(w < 100 for w in widths), widths)   # and halved


class StreamClient(FakeClient):
    """The global new-deck stream: one fixed newest-first list."""

    def __init__(self, ids, store):
        super().__init__({}, {}, store)
        self.ids = ids

    def author_of(self, deck_id):
        return f"u{int(deck_id[1:]) % 5}"

    def page(self, n, sort="created", direction="descending", **p):
        self.requests += 1
        self.pages_params.append(_key(p))
        ids = self.ids
        if "authorUserNames" in p:
            want = set(p["authorUserNames"].split(","))
            ids = [d for d in ids if self.author_of(d) in want]
        return self._rows(ids[(n - 1) * 100:n * 100])


class Crawlers(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.store = Store(self.path)
        self.c = StreamClient([f"d{i:04d}" for i in range(500)], self.store)
        self.eng = E.Engine(self.c, self.store)

    def tearDown(self):
        scrap(self.store, self.path)

    def test_tail_ingests_then_reports_caught_up(self):
        first = self.eng.tail(max_pages=10)
        self.assertEqual(first["new"], 500)
        self.assertFalse(first["caught_up"])      # ran out of stream, not overlap
        self.assertEqual(self.store.stats()["decks"], 500)
        self.assertEqual(self.store.stats()["users"], 5)

        second = self.eng.tail(max_pages=10)
        self.assertEqual(second["new"], 0)
        self.assertTrue(second["caught_up"])
        # Two quiet pages, not one: the index is replicated with differing
        # freshness, so a single known page does not prove we are at the front.
        self.assertEqual(second["seen"], 200)

    def test_sweep_counts_each_users_decks_exactly(self):
        self.eng.tail(max_pages=10)
        r = self.eng.sweep_users(limit=10)
        self.assertEqual(r["users"], 5)
        self.assertEqual(r["partial_batches"], 0)
        counts = self.store.user_counts([f"u{i}" for i in range(5)])
        self.assertEqual(counts, {f"u{i}": 100 for i in range(5)})
        self.assertEqual(self.store.unswept_users(10), [])


class LikesClient(FakeClient):
    """Rows sorted by likes, descending -- the shape a prefix generator walks."""

    def __init__(self, store, n=300):
        super().__init__({}, {}, store)
        self.n = n

    def count(self, **p):
        self.requests += 1
        return self.n

    def page(self, n, sort="created", direction="descending", **p):
        self.requests += 1
        start = (n - 1) * 100
        rows = [{"publicId": f"d{i:04d}", "createdByUser": {"userName": "u"},
                 "viewCount": 0, "likeCount": self.n - i,
                 "createdAtUtc": "2026-01-01"}
                for i in range(start, min(start + 100, self.n))]
        self.new_ids = self.store.put_rows(rows) if rows else set()
        return rows


class SortedPrefix(unittest.TestCase):
    """`likes>N` needs no range parameter: the sort order is the range."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.store = Store(self.path)
        self.c = LikesClient(self.store)
        self.eng = E.Engine(self.c, self.store)

    def tearDown(self):
        scrap(self.store, self.path)

    def test_stops_at_the_bound(self):
        r = self.eng.prefix_enum({}, "likes", ">", 250)
        self.assertEqual(r.status, "exact")
        self.assertEqual(len(r.ids), 50)          # likes 300 down to 251
        self.assertLessEqual(self.c.requests, 2)  # and stops paging there

    def test_a_bound_nothing_clears_is_empty(self):
        self.assertEqual(len(self.eng.prefix_enum({}, "likes", ">", 10**6).ids), 0)


class Residual(unittest.TestCase):
    """Copy counts are the one thing neither a row nor the server can settle,
    so they are the only reason a deck body is ever fetched."""

    def run_query(self, text):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        store = Store(path)
        key = (("board", "mainboard"), ("cardId", "id:Lightning Bolt"))
        c = FakeClient({key: 3}, {key: {"d1", "d2", "d3"}}, store)
        eng = E.Engine(c, store)
        try:
            ast = Q.resolve(Q.parse(text), c)
            plan = eng.solve(ast)
            return plan, {r["public_id"] for r in eng.iter_matches(ast, plan)}
        finally:
            scrap(store, path)

    def test_copy_count_met(self):
        plan, got = self.run_query('main:"4x Lightning Bolt"')
        self.assertTrue(plan.needs_body)
        self.assertEqual(got, {"d1", "d2", "d3"})

    def test_copy_count_not_met(self):
        _, got = self.run_query('main:"5x Lightning Bolt"')
        self.assertEqual(got, set())

    def test_single_copy_needs_no_body(self):
        plan, got = self.run_query('main:"Lightning Bolt"')
        self.assertFalse(plan.needs_body)
        self.assertEqual(got, {"d1", "d2", "d3"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
