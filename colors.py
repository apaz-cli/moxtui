"""Colour identity rendering, shared by the results table and the deck view."""

from __future__ import annotations

from rich.text import Text

WUBRG = "WUBRG"
# Mid-tone rather than literal: real white and black mana are invisible against
# one theme or the other, so each pip is pulled toward the middle where it reads
# on both a light and a dark background.
PIP = {"W": "#c9a227", "U": "#3b7fd4", "B": "#8b6fb0", "R": "#d4553b",
       "G": "#3fa05a"}
COLORLESS = "#9aa0a6"


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


# Card names are words, not single letters, so they can take a subtler palette
# than the pips -- and multicolour needs a slot that white is not already
# sitting in, hence the paler parchment for W here.
CARD = {"W": "#b8ac8c", "U": "#3b7fd4", "B": "#8b6fb0", "R": "#d4553b",
        "G": "#3fa05a"}
MULTICOLOR = "#d4a017"


def card_style(colors: str | None) -> str:
    """Style for a card name, by the card's own colours.

    Mono takes its colour, anything with two or more takes gold, and no colours
    at all -- artifacts, most lands -- stays neutral.
    """
    have = [c for c in WUBRG if c in (colors or "").upper()]
    if not have:
        return COLORLESS
    return CARD[have[0]] if len(have) == 1 else MULTICOLOR


def mana_key(colors: str | None) -> tuple:
    """Decklist order within one mana value: W U B R G, then multicolour (itself
    in WUBRG order), then colourless -- artifacts and lands last, the way a
    spoiler or a decklist is laid out."""
    have = [c for c in WUBRG if c in (colors or "").upper()]
    if not have:
        return (6,)
    if len(have) == 1:
        return (WUBRG.index(have[0]),)
    return (5, tuple(WUBRG.index(c) for c in have))


def color_key(identity: str):
    """Fewest colours first, then WUBRG order within a count -- so mono goes
    W U B R G and two-colour goes WU WB WR WG UB UR UG BR BG RG."""
    have = [c for c in WUBRG if c in (identity or "").upper()]
    return (len(have), tuple(WUBRG.index(c) for c in have))
