"""Textual front end.

The results table is the obvious half. The other half is the coverage tree,
which is the point: it shows the ledger as it is built -- every server-side
region the crawl has entered, how many decks it holds, and whether it closed
exactly or could not be verified. That is the completeness argument, visible
while it happens instead of buried in a log.
"""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (DataTable, Footer, Header, Input, ProgressBar,
                             RichLog, Static, TabbedContent, TabPane, Tree)

import engine as E
import query as Q
from api import Client, QueryError
from builder import QueryBuilder
from store import Store

COLUMNS = (("format", 12), ("color", 5), ("deck", 38), ("author", 16),
           ("likes", 6), ("views", 7), ("created", 10))

WUBRG = "WUBRG"
# Mid-tone rather than literal: real white and black mana are invisible against
# one theme or the other, so each pip is pulled toward the middle where it reads
# on both a light and a dark background.
PIP = {"W": "#c9a227", "U": "#3b7fd4", "B": "#8b6fb0", "R": "#d4553b",
       "G": "#3fa05a"}
COLORLESS = "#9aa0a6"


def color_key(identity: str):
    """Fewest colours first, then WUBRG order within a count -- so mono goes
    W U B R G and two-colour goes WU WB WR WG UB UR UG BR BG RG."""
    have = [c for c in WUBRG if c in (identity or "").upper()]
    return (len(have), tuple(WUBRG.index(c) for c in have))


# column -> (sort key, descending by default)
SORTS = {
    "format": (lambda r: (r["format"] or "").lower(), False),
    "color": (lambda r: color_key(r["color_identity"]), False),
    "deck": (lambda r: (r["name"] or "").lower(), False),
    "author": (lambda r: (r["author"] or "").lower(), False),
    "likes": (lambda r: r["likes"] or 0, True),
    "views": (lambda r: r["views"] or 0, True),
    "created": (lambda r: r["created"] or "", True),
}


def pips(identity: str) -> Text:
    """Colour identity as coloured letters, always in WUBRG order."""
    have = set((identity or "").upper())
    if not have & set(WUBRG):
        return Text("C", style=COLORLESS)
    out = Text()
    for c in WUBRG:
        if c in have:
            out.append(c, style=f"bold {PIP[c]}")
    return out


class MoxfieldTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #query { dock: top; }
    #statusbar { height: 1; background: $panel; }
    #main { height: 1fr; }
    #status { width: 1fr; color: $text-muted; padding: 0 1; }
    #progress { width: auto; padding: 0 1; }
    #results { width: 3fr; }
    #side { width: 2fr; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        ("ctrl+c", "quit", "quit"),
        ("ctrl+b", "build", "build query"),
        ("ctrl+e", "explain", "explain"),
        ("ctrl+t", "tail", "tail"),
        ("o", "open", "open deck"),
    ]

    def __init__(self, db: str = "moxfield.sqlite"):
        super().__init__()
        self.store = Store(db)
        self.client = Client(self.store, log=lambda s: self.post_note(s))
        self.eng = E.Engine(self.client, self.store, on_event=self.on_engine_event)
        self.hits: list = []
        self.rows_data: list = []       # matches in arrival order
        self.col_keys: dict = {}
        # Header clicks cycle through three states: the column's natural
        # direction, reversed, and back to the order results arrived in.
        self.sort_col: str | None = None
        self.sort_flipped = False
        self.dirty = False
        self.cell_nodes: dict = {}      # subdivision depth -> tree node
        self.counters = {"cand": 0, "match": 0, "checked": 0}
        # Progress has two phases with different denominators: enumerating is
        # measured in requests against the planner's estimate, checking in
        # decks against a candidate count that is known exactly by then.
        self.phase = "ready"
        self.current = ""        # region being counted or paged right now
        self.budget = 0          # estimated requests for this run
        self.floor = False       # the estimate is only a lower bound
        self.req0 = 0            # request count when the phase began

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder='card:"Sol Ring" cmdr:"Rograkh, Son of Rohgahh"',
                    id="query")
        with Horizontal(id="main"):
            yield DataTable(id="results", cursor_type="row")
            with Vertical(id="side"):
                with TabbedContent():
                    with TabPane("coverage", id="tab-cov"):
                        yield Tree("cells", id="cells")
                    with TabPane("log", id="tab-log"):
                        yield RichLog(id="log", wrap=True, markup=True)
        with Horizontal(id="statusbar"):
            yield Static("ready", id="status")
            yield ProgressBar(id="progress", show_eta=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#results", DataTable)
        for name, w in COLUMNS:
            self.col_keys[name] = t.add_column(name, width=w)
        self.query_one("#cells", Tree).root.expand()
        self.query_one("#query", Input).focus()
        self.set_interval(0.2, self.refresh_status)
        self.refresh_status()

    # ---- engine plumbing ---------------------------------------------------

    def on_engine_event(self, kind: str, **kw) -> None:
        """Called from the worker thread."""
        if kind == "cell":
            self.current = E.brief(kw.get("params", {}))
            self.call_from_thread(self.add_cell, kw)
        elif kind == "note":
            self.call_from_thread(self.post_note, kw.get("text", ""))
        elif kind == "page":
            self.counters["cand"] = kw.get("n", 0)
        elif kind == "checked":
            self.counters["checked"] = kw.get("i", 0)

    def add_cell(self, kw: dict) -> None:
        """One node per region the crawl enters, nested by subdivision depth."""
        tree = self.query_one("#cells", Tree)
        label = f"{E.brief(kw.get('params', {}))}  [{kw.get('n', 0):,}]"
        status = kw.get("status", "")
        if status and status != "counting":
            label += f"  {status}"
        depth = kw.get("depth", 0)
        parent = self.cell_nodes.get(depth - 1, tree.root)
        node = parent.add(label, expand=True)
        # Anything deeper belongs under this one now, not under its predecessor.
        self.cell_nodes = {d: n for d, n in self.cell_nodes.items() if d < depth}
        self.cell_nodes[depth] = node
        tree.root.expand()

    def post_note(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def set_phase(self, phase: str, budget: int = 0, floor: bool = False) -> None:
        self.phase = phase
        if phase != "enumerating":
            self.current = ""
        self.budget, self.floor = budget, floor
        self.req0 = self.client.requests
        self.refresh_status()

    def refresh_status(self) -> None:
        if self.dirty:
            self.dirty = False
            self.render_rows()
        c, done = self.counters, self.client.requests
        bar = self.query_one("#progress", ProgressBar)
        if self.phase == "enumerating":
            spent = done - self.req0
            # A floor estimate can be overrun; grow it rather than pin the bar
            # at 100% and imply the run is over.
            if spent > self.budget:
                self.budget = int(spent * 1.25) + 1
            bar.update(total=self.budget, progress=spent)
            # Name the region: an axis split spends dozens of requests on
            # counts alone, and a bar that only says "0 decks seen" through it
            # looks stuck rather than busy.
            where = f" · {self.current[:44]}" if self.current else ""
            text = (f"enumerating · {spent:,} of {'≥' if self.floor else '~'}"
                    f"{self.budget:,} requests · {c['cand']:,} decks{where}")
        elif self.phase == "checking":
            bar.update(total=max(c["cand"], 1), progress=c["checked"])
            text = (f"checking · {c['checked']:,} of {c['cand']:,} decks · "
                    f"{c['match']:,} matches")
        else:
            bar.update(total=1, progress=1 if self.phase == "done" else 0)
            text = f"{self.phase} · {c['match']:,} matches"
        thr = f" ({self.client.throttled} throttled)" if self.client.throttled else ""
        self.query_one("#status", Static).update(
            f"{text} · {done:,} requests{thr}")

    @staticmethod
    def cells(row) -> tuple:
        return (row["format"] or "-", pips(row["color_identity"]),
                (row["name"] or "")[:38], row["author"] or "-",
                row["likes"], row["views"], (row["created"] or "")[:10])

    def add_hit(self, row) -> None:
        self.rows_data.append(row)
        self.counters["match"] = len(self.rows_data)
        if self.sort_col is None:
            self.hits.append(row["public_id"])
            self.query_one("#results", DataTable).add_row(*self.cells(row))
        else:
            # Re-sorting per row would be quadratic; the refresh timer folds
            # a burst of arrivals into one redraw.
            self.dirty = True

    def render_rows(self) -> None:
        table = self.query_one("#results", DataTable)
        rows = self.rows_data
        if self.sort_col is not None:
            key, desc = SORTS[self.sort_col]
            rows = sorted(rows, key=key, reverse=desc != self.sort_flipped)
        cursor = table.cursor_row
        table.clear()
        self.hits = [r["public_id"] for r in rows]
        for r in rows:
            table.add_row(*self.cells(r))
        if cursor is not None and cursor < len(rows):
            table.move_cursor(row=cursor)
        for name, ck in self.col_keys.items():
            arrow = ""
            if name == self.sort_col:
                _, desc = SORTS[name]
                arrow = " ↑" if desc == self.sort_flipped else " ↓"
            table.columns[ck].label = Text(name + arrow)
        table.refresh()

    def on_data_table_header_selected(self, ev: DataTable.HeaderSelected) -> None:
        # By key, not by label: the label carries the sort arrow.
        name = next((n for n, k in self.col_keys.items() if k == ev.column_key), None)
        if name not in SORTS:
            return
        if name != self.sort_col:
            self.sort_col, self.sort_flipped = name, False
        elif not self.sort_flipped:
            self.sort_flipped = True
        else:                       # third click: back to arrival order
            self.sort_col, self.sort_flipped = None, False
        self.render_rows()

    def reset(self) -> None:
        self.hits.clear()
        self.rows_data.clear()
        self.cell_nodes.clear()
        self.dirty = False
        self.counters.update(cand=0, match=0, checked=0)
        self.set_phase("planning")
        self.query_one("#results", DataTable).clear()
        self.query_one("#cells", Tree).clear()

    # ---- actions -----------------------------------------------------------

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.reset()
        self.run_search(ev.value)

    def action_explain(self) -> None:
        self.reset()
        self.run_search(self.query_one("#query", Input).value, dry=True)

    def action_tail(self) -> None:
        self.run_tail()

    def url(self, i: int | None) -> str | None:
        """Deck URL for a table row index, or None if it points at nothing."""
        if i is None or not (0 <= i < len(self.hits)):
            return None
        return f"https://moxfield.com/decks/{self.hits[i]}"

    def action_open(self) -> None:
        url = self.url(self.query_one("#results", DataTable).cursor_row)
        if url:
            webbrowser.open(url)

    # ---- workers -----------------------------------------------------------

    @work(thread=True, exclusive=True)
    def run_search(self, text: str, dry: bool = False) -> None:
        try:
            ast = Q.resolve(Q.parse(text), self.client)
            self.call_from_thread(self.post_note, "[b]" + Q.describe(ast).strip())

            # Price it first. Counts are memoised, so the real plan re-uses
            # them and the estimate costs nothing beyond the planning itself.
            self.eng.dry = True
            est = self.eng.solve(ast)
            self.eng.dry = False
            for n in est.notes:
                self.call_from_thread(self.post_note, f"  {n}")
            if est.ids is None:
                self.call_from_thread(
                    self.post_note, "[red]nothing in this query can generate "
                                    "candidates")
                self.call_from_thread(self.set_phase, "ready")
                return
            self.call_from_thread(
                self.post_note,
                f"[b]~{len(est.ids):,} candidates -- {est.cost()}")
            if dry:
                for r in est.residual:
                    self.call_from_thread(
                        self.post_note, "  local: " + Q.describe(r).strip())
                self.call_from_thread(self.set_phase, "ready")
                return

            self.call_from_thread(self.set_phase, "enumerating", est.budget,
                                  est.floor)
            plan = self.eng.solve(ast)
            self.counters["cand"] = len(plan.ids or [])
            self.call_from_thread(self.set_phase, "checking")
            for row in self.eng.iter_matches(ast, plan):
                self.call_from_thread(self.add_hit, row)
            self.call_from_thread(self.set_phase, "done")
            self.call_from_thread(
                self.post_note, f"[b]done -- completeness: {plan.status}")
        except QueryError as e:
            self.call_from_thread(self.post_note, f"[red]error: {e}")
            self.call_from_thread(self.set_phase, "ready")
        finally:
            self.eng.dry = False

    @work(thread=True, exclusive=True)
    def run_tail(self) -> None:
        # The window is 100 pages deep and we stop as soon as two pages hold
        # nothing new, so the page budget is the honest denominator here.
        self.call_from_thread(self.set_phase, "enumerating", 100)
        r = self.eng.tail()
        self.call_from_thread(self.set_phase, "done")
        self.call_from_thread(
            self.post_note,
            f"tail: {r['new']} new of {r['seen']} rows"
            + ("" if r["caught_up"] else "  [red]WARNING: never caught up"))


def run(db: str = "moxfield.sqlite") -> None:
    MoxfieldTUI(db).run()


if __name__ == "__main__":
    run()
