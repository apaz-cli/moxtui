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
                             RichLog, Static)

import engine as E
import query as Q
from api import Client, QueryError
from builder import QueryBuilder
from colors import color_key, pips
from deckview import DeckView
from store import Store

COLUMNS = (("format", 12), ("color", 5), ("deck", 38), ("author", 16),
           ("likes", 6), ("views", 7), ("created", 10))

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
        ("v", "view", "view deck"),
        ("b", "build", "build query"),
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
                yield RichLog(id="log", wrap=True, markup=True)
        with Horizontal(id="statusbar"):
            yield Static("ready", id="status")
            yield ProgressBar(id="progress", show_eta=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#results", DataTable)
        for name, w in COLUMNS:
            self.col_keys[name] = t.add_column(name, width=w)
        self.query_one("#query", Input).focus()
        self.set_interval(0.2, self.refresh_status)
        self.refresh_status()

    # ---- engine plumbing ---------------------------------------------------

    def on_engine_event(self, kind: str, **kw) -> None:
        """Called from the worker thread."""
        if kind == "cell":
            self.current = E.brief(kw.get("params", {}))
            self.call_from_thread(self.log_cell, kw)
        elif kind == "note":
            self.call_from_thread(self.post_note, kw.get("text", ""))
        elif kind == "page":
            self.counters["cand"] = kw.get("n", 0)
        elif kind == "checked":
            self.counters["checked"] = kw.get("i", 0)

    def log_cell(self, kw: dict) -> None:
        """Each region the crawl enters, with what it found and how it closed."""
        status = kw.get("status", "")
        tail = f"  [b]{status}" if status and status != "counting" else ""
        self.post_note(f"[dim]{E.brief(kw.get('params', {}))}[/dim]  "
                       f"{kw.get('n', 0):,}{tail}")

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
        self.dirty = False
        self.counters.update(cand=0, match=0, checked=0)
        self.set_phase("planning")
        self.query_one("#results", DataTable).clear()

    # ---- actions -----------------------------------------------------------

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.reset()
        self.run_search(ev.value)

    def action_build(self) -> None:
        self.push_screen(QueryBuilder(), self.on_built)

    def on_built(self, text: str | None) -> None:
        if not text:
            return
        self.query_one("#query", Input).value = text
        self.reset()
        self.run_search(text)

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        """Clicking (or Enter on) a row copies its link -- the thing you almost
        always want next."""
        url = self.url(ev.cursor_row)
        if url:
            self.copy_to_clipboard(url)
            self.notify(url, title="copied to clipboard", timeout=3)

    def url(self, i: int | None) -> str | None:
        """Deck URL for a table row index, or None if it points at nothing."""
        if i is None or not (0 <= i < len(self.hits)):
            return None
        return f"https://moxfield.com/decks/{self.hits[i]}"

    def results(self) -> DataTable | None:
        """The results table, or None when another screen is on top -- app-level
        bindings stay live over a pushed screen, so every one of them has to
        cope with the table not being there."""
        found = self.screen.query("#results")
        return found.first(DataTable) if found else None

    def action_view(self) -> None:
        """The decklist, full screen. `v` rather than enter, because enter and a
        mouse click are the same event and that one copies the link."""
        table = self.results()
        if table is None:
            return
        i = table.cursor_row
        if i is None or not (0 <= i < len(self.hits)):
            return
        row = self.store.row(self.hits[i])
        if row is not None:
            self.push_screen(DeckView(self.client, row))

    def action_open(self) -> None:
        table = self.results()
        url = self.url(table.cursor_row) if table is not None else None
        if url:
            webbrowser.open(url)

    # ---- workers -----------------------------------------------------------

    @work(thread=True, exclusive=True)
    def run_search(self, text: str) -> None:
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
            # What the plan cannot settle server-side, said plainly, since
            # those terms are what the deck-body fetches are for.
            for r in est.residual:
                self.call_from_thread(
                    self.post_note, "[dim]  local: " + Q.describe(r).strip())

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


def run(db: str = "moxfield.sqlite") -> None:
    MoxfieldTUI(db).run()


if __name__ == "__main__":
    run()
