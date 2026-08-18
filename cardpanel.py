"""The reserved space: one card, rendered into whatever rectangle it is given.

Placement is decided by where there is more room rather than by the card's
shape. A half-block card image is about 40 columns by 28 rows -- wider than tall
in *cells*, because a cell is roughly twice as tall as it is wide -- so the card
does not itself argue for one side or the other.
"""

from __future__ import annotations

import os

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container
from textual.message import Message
from textual.widgets import Static

from colors import card_style


def _seed_cell_size() -> None:
    """Tell textual-image the cell size before it asks the terminal itself.

    Left alone it writes an escape sequence and waits on stdin for the reply --
    but Textual owns stdin and eats the reply, so the probe always times out and
    dumps a traceback when the app releases the terminal on quit. An ioctl
    answers the same question without touching stdin; where even that fails,
    8x16 is the near-universal default and only the image aspect depends on it.

    This has to run *before* `textual_image.widget` is imported: the probe fires
    on import, so seeding afterwards is too late to prevent anything.
    """
    try:
        from textual_image import _terminal as term
    except Exception:
        return
    if hasattr(term.get_cell_size, "_result"):
        return
    width = height = 0
    try:
        rows, cols, px_w, px_h = term.get_tiocgwinsz()
        if rows and cols:
            width, height = int(px_w / cols), int(px_h / rows)
    except Exception:
        pass
    term.get_cell_size._result = term.CellSize(width or 8, height or 16)


# Optional: the panel is a text panel that can also show a picture. If the
# dependency is missing, or the terminal has no graphics protocol, the picture
# is simply absent -- nothing here may break the view.
# MOXFIELD_CARD_IMAGES picks the renderer: `0`/`no`/`off` for no pictures,
# `auto` (the default), or one of tgp/kitty, sixel, halfcell, unicode to force
# one. Forcing exists because auto-detection is unreliable by construction: the
# library asks the terminal over stdin and gives up after 100ms, which fails
# under any multiplexer that does not answer, and a terminal claiming a protocol
# it does not honour is indistinguishable from one that does.
MODE = os.environ.get("MOXFIELD_CARD_IMAGES", "auto").lower()

Image = None
if MODE not in ("0", "no", "off", "false", "none"):
    try:
        _seed_cell_size()                           # must precede the import
        from textual_image import widget as _tiw

        forced = {"tgp": _tiw.TGPImage, "kitty": _tiw.TGPImage,
                  "sixel": _tiw.SixelImage, "halfcell": _tiw.HalfcellImage,
                  "unicode": _tiw.UnicodeImage}
        if MODE in forced:
            Image = forced[MODE]
        elif os.environ.get("HERDR_ENV"):
            # herdr reads Kitty graphics out of the pane and repaints them to
            # the host terminal, so the protocol works -- but it does not reply
            # to the detection query, so auto-detection gives up and falls back
            # to half-blocks. Skip the question we know the answer to.
            Image = _tiw.TGPImage
        else:
            Image = _tiw.Image
    except Exception:                               # pragma: no cover
        Image = None

PANEL_W = 46        # columns, when placed beside the list
PANEL_H = 12        # rows, when placed below it
MIN_LIST_W = 60     # below this the decklist stops being readable
MIN_LIST_H = 10
WIDE = 120          # a terminal with columns to spare


def placement(width: int, height: int) -> str:
    """`side`, `bottom`, or `hidden` -- wherever there is more room.

    Wide terminals have columns to spare and rows are always scarcer, so the
    panel goes beside the list. Narrow ones cannot afford 46 columns and give up
    rows instead. Neither fits on a small terminal, and nothing is worth
    squeezing the list below readable.
    """
    if width >= WIDE and width - PANEL_W >= MIN_LIST_W:
        return "side"
    if height - PANEL_H >= MIN_LIST_H:
        return "bottom"
    if width - PANEL_W >= MIN_LIST_W:
        return "side"
    return "hidden"


# A half-block cell is one pixel wide and two tall, so a card -- 0.716 wide to
# tall -- covers about 0.7 rows for every column it spans.
ROWS_PER_COL = 0.7
MIN_TEXT_ROWS = 10      # oracle text needs this much to be worth reading
GUTTER_ROWS = 1         # blank line between the picture and the text
GUTTER_COLS = 2

# Layouts with a genuine second side, and so a second image to turn to. A split
# or adventure card also has two faces, but both are printed on one picture --
# there is nothing to flip to.
TWO_SIDED = {"transform", "modal_dfc", "double_faced_token", "reversible_card",
             "art_series"}


def image_box(where: str, width: int, height: int) -> tuple:
    """Columns and rows for the picture, given the box the panel was handed."""
    if where == "bottom":
        rows = max(0, height - 1)
        return int(rows / ROWS_PER_COL), rows
    cols = max(0, width)
    rows = int(cols * ROWS_PER_COL)
    if height - rows < MIN_TEXT_ROWS:               # text comes first
        rows = max(0, height - MIN_TEXT_ROWS)
        cols = int(rows / ROWS_PER_COL)
    return cols, rows


class _Row(dict):
    """sqlite rows and the dicts Scryfall lookups return, read the same way."""

    def __getitem__(self, k):
        return self.get(k) or ""


class CardPanel(Container):
    """Image and text, sharing whatever rectangle the placement gave them."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entry = None
        self.detail = None
        self.where = "side"
        self.box = (0, 0)
        self.has_image = False
        self.face = 0           # 0 front, 1 back

    def compose(self) -> ComposeResult:
        if Image is not None:
            yield Image(id="cardimg")
        yield Static(id="cardtext")

    def fit(self, where: str, width: int, height: int) -> None:
        """Side puts the picture above the text, bottom puts it beside."""
        self.where = where
        self.styles.layout = "vertical" if where == "side" else "horizontal"
        img = self.query("#cardimg")
        if not img:
            return
        box = img.first()
        if where == "side":
            box.styles.margin = (0, 0, GUTTER_ROWS, 0)
            self.box = image_box(where, width - 2, height - GUTTER_ROWS)
        else:
            box.styles.margin = (0, GUTTER_COLS, 0, 0)
            self.box = image_box(where, width - 2 - GUTTER_COLS, height)
        box.styles.width, box.styles.height = self.box
        self._reveal()

    def _reveal(self) -> None:
        """Show the picture only when there is one and it has somewhere to go.

        Without an image the whole panel goes to the text rather than leaving a
        reserved hole where a picture would have been.
        """
        img = self.query("#cardimg")
        if img:
            cols, rows = self.box
            img.first().display = self.has_image and cols > 4 and rows > 3

    class Flip(Message):
        """The reader turned the card over."""

        def __init__(self, face: int):
            super().__init__()
            self.face = face

    @property
    def two_sided(self) -> bool:
        d = self.detail
        if not d:
            return False
        d = _Row(d) if isinstance(d, dict) else d
        return bool(d["layout"] in TWO_SIDED and d["back_name"])

    def on_click(self, event) -> None:
        """Clicking the card turns it over, when there is a back to turn to."""
        if not self.two_sided:
            return
        self.face = 1 - self.face
        self.query_one("#cardtext", Static).update(self.render_card())
        self.post_message(self.Flip(self.face))

    def show(self, entry, detail, image_path=None, keep_face=False) -> None:
        if not keep_face:
            self.face = 0                       # a new card starts face up
        self.entry, self.detail = entry, detail
        self.query_one("#cardtext", Static).update(self.render_card())
        img = self.query("#cardimg")
        self.has_image = False
        if img and image_path:
            try:
                img.first().image = image_path
                self.has_image = True
            except Exception:                       # unreadable file, bad decode
                self.has_image = False
        self._reveal()

    def render_card(self):
        if self.entry is None:
            return Text("select a card", style="dim italic")
        name, colors = self.entry[0], (self.entry[2] if len(self.entry) > 2 else "")
        d = self.detail
        if isinstance(d, dict):
            d = _Row(d)
        if d is None:
            title = Text(name, style=f"bold {card_style(colors)}")
            return Group(title, Text("(no card detail stored yet)", style="dim"))

        # `p` reads whichever side is up; only the printing line is shared.
        back = self.face == 1 and self.two_sided
        p = (lambda k: d["back_" + k]) if back else (lambda k: d[k])
        # Stored `name` is the combined "A // B"; each side shows its own.
        shown = p("name") or name
        if self.two_sided and not back:
            shown = (d["name"] or name).split(" // ")[0]
        title = Text()
        title.append(shown, style=f"bold {card_style(colors)}")
        if p("mana_cost"):
            title.append(f"   {p('mana_cost')}", style="dim")
        if self.two_sided:
            title.append("   (back)" if back else "   (front)", style="dim italic")

        bits = [title, Text(p("type_line") or "", style="italic")]
        if p("oracle_text"):
            bits.append(Text(""))
            bits.append(Text(p("oracle_text")))
        pt = "/".join(x for x in (p("power"), p("toughness")) if x)
        if pt:
            bits += [Text(""), Text(pt, style="bold")]
        elif not back and d["loyalty"]:
            bits += [Text(""), Text(f"loyalty {d['loyalty']}", style="bold")]
        if p("flavor_text"):
            bits += [Text(""), Text(p("flavor_text"), style="dim italic")]

        # Printing line: the original where we have looked it up, otherwise
        # whatever printing this deck happens to use.
        orig = d["orig_set_name"] or ""
        if orig:
            line = f"{orig} · {(d['orig_released_at'] or '')[:4]}"
            if d["orig_artist"]:
                line += f" · {d['orig_artist']}"
            bits += [Text(""), Text(line, style="dim")]
        elif d["set_name"]:
            line = f"{d['set_name']} · {(d['released_at'] or '')[:4]}"
            bits += [Text(""), Text(line + "  (this deck's printing)", style="dim")]
        return Group(*bits)
