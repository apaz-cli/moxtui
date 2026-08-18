"""The decklist itself, full screen.

Not a modal: the query builder is a form you fill in and dismiss, but a
decklist is content you read, and a 100-card singleton list wants every row the
terminal will give it. So this is a pushed screen that fills the display and
flows the cards into as many columns as fit.
"""

from __future__ import annotations

import webbrowser

from rich.columns import Columns
from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from cardtypes import group, order
from colors import card_style, pips

# Display order. Anything the API returns that is not listed still shows, after
# these -- except tokens, which are generated cards rather than deck contents.
BOARDS = [
    ("commanders", "Command Zone"),
    ("companions", "Companion"),
    ("signatureSpells", "Signature Spells"),
    ("mainboard", "Mainboard"),
    ("sideboard", "Sideboard"),
    ("maybeboard", "Maybeboard"),
]
HIDDEN = {"tokens"}


def ordered(boards: dict):
    named = [(k, t) for k, t in BOARDS if boards.get(k)]
    known = {k for k, _ in BOARDS}
    extra = [(k, k) for k in sorted(boards)
             if k not in known and k not in HIDDEN and boards[k]]
    return named + extra


class DeckView(Screen):
    CSS = """
    DeckView { layout: vertical; }
    #meta { height: auto; padding: 0 1; background: $panel; }
    #cards { padding: 0 1; }
    """
    BINDINGS = [
        ("escape", "close", "back"),
        ("q", "close", "back"),
        ("o", "open", "open in browser"),
        ("c", "copy", "copy link"),
    ]

    def __init__(self, client, row):
        super().__init__()
        self.client, self.row = client, row
        self.pid = row["public_id"]
        self.loaded = False

    def compose(self) -> ComposeResult:
        yield Static(self.meta(), id="meta")
        yield VerticalScroll(Static("loading deck...", id="cards"))
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    # ---- content -----------------------------------------------------------

    def meta(self) -> Group:
        r = self.row
        title = Text(r["name"] or "(untitled)", style="bold")
        line = Text()
        line.append(f"{r['format'] or '-'}", style="cyan")
        if r["bracket"]:
            line.append(f"  bracket {r['bracket']}")
        line.append("  ")
        line.append_text(pips(r["color_identity"]))
        line.append(f"  by {r['author'] or '-'}", style="dim")
        line.append(f"   {r['likes'] or 0} likes  {r['views'] or 0} views",
                    style="dim")
        when = Text(f"created {(r['created'] or '')[:10]}  "
                    f"updated {(r['updated'] or '')[:10]}   "
                    f"moxfield.com/decks/{self.pid}", style="dim")
        return Group(title, line, when)

    @work(thread=True)
    def load(self) -> None:
        """A body is one request, so it may not be instant on a cold cache."""
        boards = self.client.body(self.pid)
        self.app.call_from_thread(self.show, boards)

    def show(self, boards) -> None:
        self.loaded = True
        target = self.query_one("#cards", Static)
        if not boards:
            target.update(Text("this deck is private or has been deleted",
                               style="italic"))
            return
        blocks = []
        for key, title in ordered(boards):
            entries = boards[key]
            head = Text()
            head.append(f"{title} ", style="bold")
            head.append(f"({sum(e[1] for e in entries)})", style="dim")
            # Only the mainboard earns type sections. A command zone is two
            # cards and a maybeboard is a pile of ideas -- headings there are
            # scaffolding around nothing.
            body = (self.by_type(entries) if key == "mainboard"
                    else self.flat(entries))
            blocks += [head, body, Text("")]
        target.update(Group(*blocks))

    @classmethod
    def flat(cls, entries: list):
        """Every other board: just the cards, in decklist order."""
        return Columns([cls.line(e) for e in sorted(entries, key=order)],
                       padding=(0, 3), column_first=True)

    @staticmethod
    def line(entry) -> Text:
        t = Text()
        t.append(f"{entry[1]} ", style="dim")
        t.append(entry[0], style=card_style(entry[2] if len(entry) > 2 else ""))
        return t

    @classmethod
    def by_type(cls, entries: list):
        """One block per card type, flowed across the width.

        Blocks rather than a single list because a decklist is read type by
        type, and side by side because that is what the terminal has spare.
        """
        sections = []
        for name, cards, elsewhere in group(entries):
            head = Text()
            head.append(name, style="bold")
            if cards:
                head.append(f" ({sum(e[1] for e in cards)})", style="dim")
            # This card is a Land too, but it is listed under Sorcery.
            if elsewhere:
                head.append(f" (+{elsewhere})", style="dim italic")
            sections.append(Group(head, *(cls.line(e) for e in cards)))
        # Bottom padding, so when the sections wrap onto a second row there is a
        # blank line between the rows rather than headings butting into cards.
        return Columns(sections, padding=(0, 3, 1, 0))

    # ---- actions -----------------------------------------------------------

    @property
    def url(self) -> str:
        return f"https://moxfield.com/decks/{self.pid}"

    def action_close(self) -> None:
        self.dismiss()

    def action_open(self) -> None:
        webbrowser.open(self.url)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.url)
        self.notify(self.url, title="copied to clipboard", timeout=3)
