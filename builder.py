"""A form for people who would rather not learn the query syntax.

It writes the query rather than replacing it: the preview line is the actual
string that will run, so the form doubles as a way to learn the language.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

FORMATS = [("any", ""), ("commander", "commander"), ("modern", "modern"),
           ("legacy", "legacy"), ("pauper", "pauper"), ("vintage", "vintage"),
           ("standard", "standard"), ("pioneer", "pioneer"),
           ("duel commander", "duelCommander"), ("pauper edh", "pauperEdh"),
           ("brawl", "brawl"), ("oathbreaker", "oathbreaker"),
           ("no format set", "none")]

# id, label, placeholder, and how the value becomes query text.
FIELDS = [
    ("cmdr", "Commander", "Rograkh, Son of Rohgahh", lambda v: [f'cmdr:"{v}"']),
    ("cards", "Contains cards", "Sol Ring, Rhystic Study",
     lambda v: [f'card:"{c.strip()}"' for c in v.split(",") if c.strip()]),
    ("main", "In the mainboard", "4x Lightning Bolt",
     lambda v: [f'main:"{c.strip()}"' for c in v.split(",") if c.strip()]),
    ("without", "Without cards", "Mana Crypt",
     lambda v: [f'-card:"{c.strip()}"' for c in v.split(",") if c.strip()]),
    ("by", "Author", "mtglab", lambda v: [f"by:{v}"]),
    ("name", "Deck title contains", "storm", lambda v: [f'name:"{v}"']),
    ("hub", "Hub", "budget", lambda v: [f"hub:{v}"]),
    ("likes", "Min likes", "100", lambda v: [f"likes>{v}"]),
    ("views", "Min views", "1000", lambda v: [f"views>{v}"]),
    ("created", "Created after", "2025", lambda v: [f"created>{v}"]),
]


class QueryBuilder(ModalScreen[str]):
    """Returns a query string, or nothing if cancelled."""

    CSS = """
    QueryBuilder { align: center middle; }
    #box { width: 80; height: auto; max-height: 90%; padding: 1 2;
           background: $surface; border: round $primary; }
    /* The fields scroll so the preview and the buttons are always reachable,
       whatever the terminal height. */
    #scroll { height: auto; max-height: 24; }
    #fields { grid-size: 2; grid-columns: 21 1fr; grid-rows: 1;
              grid-gutter: 0 1; height: auto; }
    #fields Label { text-align: right; width: 100%; }
    #fields Input { height: 1; border: none; padding: 0 1; background: $boost; }
    #fields Input:focus { background: $primary 25%; }
    #fields Select { height: 1; border: none; }
    #fields SelectCurrent { border: none; padding: 0 1; background: $boost; }
    #preview { padding: 1 0 0 0; color: $text-muted; height: auto; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    Button { margin-left: 1; min-width: 10; height: 1; border: none; }
    """
    BINDINGS = [("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("[b]Build a query")
            with VerticalScroll(id="scroll"), Grid(id="fields"):
                yield Label("Format")
                yield Select(FORMATS, id="fmt", value="", allow_blank=False)
                yield Label("Bracket")
                yield Select([("any", "")] + [(str(i), str(i)) for i in range(1, 6)],
                             id="bracket", value="", allow_blank=False)
                for fid, label, placeholder, _ in FIELDS:
                    yield Label(label)
                    yield Input(placeholder=placeholder, id=fid)
            yield Static("", id="preview")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Search", variant="primary", id="go")

    def on_mount(self) -> None:
        self.query_one("#cmdr", Input).focus()
        self.update_preview()

    def query_text(self) -> str:
        parts = []
        fmt = self.query_one("#fmt", Select).value
        if fmt:
            parts.append(f"f:{fmt}")
        bracket = self.query_one("#bracket", Select).value
        if bracket:
            parts.append(f"bracket:{bracket}")
        for fid, _, _, render in FIELDS:
            v = self.query_one(f"#{fid}", Input).value.strip()
            if v:
                parts += render(v)
        return " ".join(parts)

    def update_preview(self) -> None:
        q = self.query_text()
        self.query_one("#preview", Static).update(q or "[dim]nothing selected yet")

    def on_input_changed(self, _) -> None:
        self.update_preview()

    def on_select_changed(self, _) -> None:
        self.update_preview()

    def on_input_submitted(self, _) -> None:
        self.action_go()

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        self.action_go() if ev.button.id == "go" else self.action_cancel()

    def action_go(self) -> None:
        q = self.query_text()
        if q:
            self.dismiss(q)

    def action_cancel(self) -> None:
        self.dismiss("")
