"""The decklist body: laid out by hand so every card knows where it is.

Rich's `Columns` renders a blob with no coordinate map, which is fine to look at
and useless to click. Doing the column packing here costs about fifty lines and
buys a cursor, mouse selection, and control over how the reserved panel changes
the width available.
"""

from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from cardtypes import group, order
from colors import card_style

GAP = 3


class Block:
    """One column's worth of content: an optional heading and its cards."""

    def __init__(self, header: Text | None, items: list):
        self.header = header
        self.items = items                      # [(Text, card index)]
        self.width = max([header.cell_len if header else 0]
                         + [t.cell_len for t, _ in items] or [0])

    def __len__(self) -> int:
        return len(self.items) + (1 if self.header else 0)

    def row(self, i: int):
        """(text, card index) for line i of this block, or (None, None)."""
        if self.header:
            if i == 0:
                return self.header, None
            i -= 1
        return self.items[i] if i < len(self.items) else (None, None)


class CardList(Static):
    """Renders the boards and maps screen positions back to cards."""

    # Focusable, and it owns the movement keys: the scroll container binds the
    # arrows to scrolling and would otherwise eat them before the cursor moves.
    can_focus = True
    BINDINGS = [
        ("up", "move(0, -1)", ""), ("down", "move(0, 1)", ""),
        ("left", "move(-1, 0)", ""), ("right", "move(1, 0)", ""),
        ("k", "move(0, -1)", ""), ("j", "move(0, 1)", ""),
        ("h", "move(-1, 0)", ""), ("l", "move(1, 0)", ""),
    ]

    cursor = reactive(0)

    def __init__(self, boards: dict, order_of_boards, **kw):
        super().__init__(**kw)
        self.boards = boards
        self.order_of_boards = order_of_boards
        self.cards: list = []       # flattened, in the order they appear
        self.lines: list = []
        self.spots: list = []       # (y, x0, x1, card index)
        self._width = 0

    # ---- layout ------------------------------------------------------------

    def on_resize(self, event) -> None:
        if event.size.width != self._width:
            self.build(event.size.width)

    def build(self, width: int) -> None:
        self._width = max(width, 20)
        self.cards, self.lines, self.spots = [], [], []
        for key, title in self.order_of_boards(self.boards):
            entries = self.boards[key]
            head = Text()
            head.append(f"{title} ", style="bold")
            head.append(f"({sum(e[1] for e in entries)})", style="dim")
            self.lines.append(head)
            # Only the mainboard earns type sections; elsewhere a heading is
            # scaffolding around nothing.
            blocks = (self._typed(entries) if key == "mainboard"
                      else self._flat(entries))
            self._emit(blocks)
            self.lines.append(Text(""))
        # layout=True, not a plain repaint: the content height just changed, and
        # an auto-height widget that is not re-measured keeps whatever height it
        # had when it was empty -- which is one row, showing one heading.
        self.refresh(layout=True)
        # Growing the content can raise a scrollbar, which takes a column or two
        # back off the width we just laid out for. Re-check once the new size is
        # settled; the width guard stops this recursing.
        self.call_after_refresh(self._refit)

    def _refit(self) -> None:
        width = self.content_size.width
        if width and width != self._width:
            self.build(width)

    def _item(self, entry) -> tuple:
        idx = len(self.cards)
        self.cards.append(entry)
        t = Text()
        t.append(f"{entry[1]} ", style="dim")
        t.append(entry[0], style=card_style(entry[2] if len(entry) > 2 else ""))
        return t, idx

    def _typed(self, entries: list) -> list:
        blocks = []
        for name, cards, elsewhere in group(entries):
            head = Text()
            head.append(name, style="bold")
            head.append(f" ({sum(e[1] for e in cards)})", style="dim")
            if elsewhere:                       # counted here, listed elsewhere
                head.append(f" (+{elsewhere})", style="dim italic")
            blocks.append(Block(head, [self._item(e) for e in cards]))
        return blocks

    def _flat(self, entries: list) -> list:
        items = [self._item(e) for e in sorted(entries, key=order)]
        widest = max((t.cell_len for t, _ in items), default=1)
        cols = max(1, (self._width + GAP) // (widest + GAP))
        rows = -(-len(items) // cols)           # read down, then across
        return [Block(None, items[i:i + rows]) for i in range(0, len(items), rows)]

    def _emit(self, blocks: list) -> None:
        """Pack blocks left to right, wrapping to a new row when out of width."""
        row, used = [], 0
        for b in blocks:
            if row and used + GAP + b.width > self._width:
                self._emit_row(row)
                row, used = [], 0
            used += (GAP if row else 0) + b.width
            row.append(b)
        if row:
            self._emit_row(row)

    def _emit_row(self, row: list) -> None:
        height = max(len(b) for b in row)
        for i in range(height):
            line, x = Text(), 0
            for b in row:
                if x > line.cell_len:
                    line.append(" " * (x - line.cell_len))
                text, idx = b.row(i)
                if text is not None:
                    if idx is not None:
                        self.spots.append((len(self.lines), x,
                                           x + text.cell_len, idx))
                    line.append_text(text)
                x += b.width + GAP
            self.lines.append(line)
        # A blank line between wrapped rows, so headings do not butt into cards.
        self.lines.append(Text(""))

    # ---- rendering and selection -------------------------------------------

    def render(self) -> Text:
        if not self.lines:
            return Text("")
        # Never wrap. A line too wide for the widget must be cropped, because a
        # wrapped remainder is emitted full-width and shoves every column after
        # it out of alignment -- one long card name would derange the page.
        out = Text(no_wrap=True, overflow="crop")
        marks = {}
        for y, x0, x1, idx in self.spots:
            if idx == self.cursor:
                marks[y] = (x0, x1)
        for y, line in enumerate(self.lines):
            copy = line.copy()
            if y in marks:
                x0, x1 = marks[y]
                copy.stylize("reverse", x0, x1)
            out.append_text(copy)
            out.append("\n")
        return out

    def watch_cursor(self) -> None:
        self.refresh()
        self.scroll_cursor_into_view()
        self.post_message(self.Selected(self.selected))

    def scroll_cursor_into_view(self) -> None:
        spot = self._spot(self.cursor)
        if spot is None or self.parent is None:
            return
        y, view = spot[0], self.parent
        top = view.scroll_offset.y
        bottom = top + view.size.height - 1
        if y < top:
            view.scroll_to(y=max(0, y - 2), animate=False)
        elif y > bottom:
            view.scroll_to(y=y - view.size.height + 3, animate=False)

    @property
    def selected(self):
        return self.cards[self.cursor] if self.cards else None

    def on_click(self, event) -> None:
        for y, x0, x1, idx in self.spots:
            if y == event.y and x0 <= event.x < x1:
                self.cursor = idx
                return

    def _spot(self, idx: int):
        return next((s for s in self.spots if s[3] == idx), None)

    def action_move(self, dx: int, dy: int) -> None:
        """Move by screen position, not list index -- the list is laid out in
        columns, so "down" means the card below, not the next one stored."""
        here = self._spot(self.cursor)
        if here is None:
            return
        y, x = here[0], here[1]
        if dy:
            ahead = [s for s in self.spots if (s[0] - y) * dy > 0]
            key = lambda s: (abs(s[0] - y), abs(s[1] - x))
        else:
            ahead = [s for s in self.spots if s[0] == y and (s[1] - x) * dx > 0]
            key = lambda s: abs(s[1] - x)
        if ahead:
            self.cursor = min(ahead, key=key)[3]
        elif dx < 0:
            # Nothing further left: the reader is at the edge and pressing on,
            # which on the first card means they just arrived and changed their
            # mind.
            self.post_message(self.Escaped())

    class Escaped(Message):
        """Left was pressed with nothing to the left of the cursor."""

    class Selected(Message):
        """The cursor moved onto a card."""

        def __init__(self, entry):
            super().__init__()
            self.entry = entry
