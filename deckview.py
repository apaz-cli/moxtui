"""The decklist itself, full screen.

Not a modal: the query builder is a form you fill in and dismiss, but a
decklist is content you read, and a 100-card singleton list wants every row the
terminal will give it. So this is a pushed screen that fills the display and
flows the cards into as many columns as fit.
"""

from __future__ import annotations

import os
import webbrowser

from rich.console import Group
from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from cardlist import CardList
from cardpanel import PANEL_H, PANEL_W, CardPanel, placement
from colors import pips
from scryfall import Scryfall

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
    #body { height: 1fr; }
    #listwrap { width: 1fr; height: 1fr; }
    #cards { padding: 0 1; height: auto; }
    #panel { padding: 0 1; background: $boost; }
    """
    BINDINGS = [
        Binding("q", "close", "back (or left, backspace)"),
        Binding("backspace", "close", "back", show=False),
        Binding("escape", "quit", "quit (or ^c)"),
        Binding("ctrl+c", "quit", "quit", show=False),
        ("o", "open", "open in browser"),
        ("c", "copy", "copy link"),
    ]

    def __init__(self, client, row):
        super().__init__()
        self.client, self.row = client, row
        self.pid = row["public_id"]
        self.loaded = False
        self.scryfall = Scryfall(client.store)
        self._debounce = None

    def compose(self) -> ComposeResult:
        yield Static(self.meta(), id="meta")
        with Container(id="body"):
            with VerticalScroll(id="listwrap"):
                yield CardList({}, ordered, id="cards")
            yield CardPanel(id="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.place()
        self.load()

    def on_resize(self) -> None:
        self.place()

    def place(self) -> None:
        """Reserve the space, then let the panel render into what it got."""
        where = placement(self.size.width, self.size.height)
        body, panel = self.query_one("#body"), self.query_one("#panel")
        panel.display = where != "hidden"
        body.styles.layout = "horizontal" if where == "side" else "vertical"
        if where == "side":
            panel.styles.width, panel.styles.height = PANEL_W, "100%"
            panel.fit(where, PANEL_W, self.size.height - 4)
        else:
            panel.styles.width, panel.styles.height = "100%", PANEL_H
            panel.fit(where, self.size.width, PANEL_H)

    # ---- selection ---------------------------------------------------------

    def on_card_list_escaped(self, ev: CardList.Escaped) -> None:
        """Left, with nothing to the left of the cursor: leave the way we came."""
        self.action_close()

    def on_card_list_selected(self, ev: CardList.Selected) -> None:
        entry = ev.entry
        uid = entry[5] if entry and len(entry) > 5 else None
        detail = self.client.store.card(uid) if uid else None
        panel = self.query_one("#panel", CardPanel)
        # Text first, from what is already local, so the panel never lags the
        # cursor. The picture catches up if and when it can.
        panel.show(entry, detail, self.cached_image(detail))
        if self._debounce is not None:
            self._debounce.stop()
        # Holding an arrow key would otherwise fire a lookup per keypress.
        self._debounce = self.set_timer(0.2, self.fetch_art)

    def cached_image(self, detail, face: int = 0):
        if not detail:
            return None
        sid = detail["orig_scryfall_id"] or detail["scryfall_id"]
        if not sid:
            return None
        suffix = "" if face == 0 else "-back"
        path = os.path.join(self.scryfall.dir, f"{sid}-normal{suffix}.jpg")
        return path if os.path.exists(path) else None

    def on_card_panel_flip(self, ev: CardPanel.Flip) -> None:
        """The card was turned over; fetch that side's picture if we lack it."""
        panel = self.query_one("#panel", CardPanel)
        panel.show(panel.entry, panel.detail,
                   self.cached_image(panel.detail, ev.face), keep_face=True)
        self.fetch_art()

    @work(thread=True, exclusive=True)
    def fetch_art(self) -> None:
        """Original printing and its picture, once per card, ever."""
        entry = self._cards().selected
        uid = entry[5] if entry and len(entry) > 5 else None
        if not uid:
            return
        panel = self.query_one("#panel", CardPanel)
        face = panel.face
        try:
            detail = self.scryfall.original(uid, entry[0])
            path = self.scryfall.image_path(
                (detail or {}).get("orig_scryfall_id") or "", "normal",
                "front" if face == 0 else "back")
        except Exception:
            return                          # decoration; never break the view
        # The cursor -- or the side showing -- may have moved on while we were
        # away, in which case this picture is no longer the one being asked for.
        if self._cards().selected is entry and panel.face == face:
            self.app.call_from_thread(panel.show, entry, detail, path, True)

    def _cards(self) -> CardList:
        return self.query_one("#cards", CardList)

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
                    f"updated {(r['updated'] or '')[:10]}   ", style="dim")
        # The whole URL, and marked up as a real terminal hyperlink (OSC 8), so
        # the terminal can open or copy it without going through the app.
        when.append(self.url, style=f"dim underline link {self.url}")
        return Group(title, line, when)

    @work(thread=True)
    def load(self) -> None:
        """A body is one request, so it may not be instant on a cold cache."""
        boards = self.client.body(self.pid)
        self.app.call_from_thread(self.show, boards)

    def show(self, boards) -> None:
        self.loaded = True
        if not boards:
            self.query_one("#cards", CardList).update(
                Text("this deck is private or has been deleted", style="italic"))
            return
        cards = self._cards()
        cards.boards = boards
        cards.build(cards.size.width or self.size.width)
        cards.post_message(CardList.Selected(cards.selected))
        cards.focus()

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
