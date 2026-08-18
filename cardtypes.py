"""Card type parsing, for grouping a decklist the way a deckbuilder reads it.

A card can be several types at once -- an Artifact Creature, a Sorcery on one
face and a Land on the other. It is listed once, under the first of its types in
this order, and every other section it belongs to notes how many of its cards
are shown elsewhere.
"""

from __future__ import annotations

from colors import mana_key

# Two different orders, because they answer two different questions.
#
# PRECEDENCE decides which single section a multi-type card is listed in. Land
# sits high here so anything that taps for mana is counted as a land, whatever
# else it also happens to be.
PRECEDENCE = ["Creature", "Planeswalker", "Land", "Artifact", "Enchantment",
              "Sorcery", "Instant", "Battle", "Kindred", "Other"]
RANK = {t: i for i, t in enumerate(PRECEDENCE)}

# DISPLAY decides what order the sections appear on screen. Lands go last: they
# are the biggest and least interesting block, and burying the spells behind
# them is not how anyone reads a list.
DISPLAY = ["Creature", "Planeswalker", "Sorcery", "Instant", "Kindred",
           "Artifact", "Enchantment", "Battle", "Land", "Other"]

# Kindred was called Tribal until 2024; they are one section.
ALIASES = {"Tribal": "Kindred"}

# Supertypes are not card types -- "Legendary Artifact Creature" is an Artifact
# Creature that happens to be legendary.
SUPERTYPES = {"Legendary", "Basic", "Snow", "World", "Elite", "Host", "Ongoing"}


def types_of(type_line: str | None) -> list:
    """Every card type on the card, across both faces, in reading order.

    Anything the game has invented that is not in ORDER -- planes, schemes,
    sticker sheets, attractions -- lands in "Other" rather than inventing a
    section per curiosity.
    """
    out: list = []
    for face in (type_line or "").split("//"):
        # The em dash separates types from subtypes: "Creature — Kobold".
        for word in face.split("—")[0].split():
            word = ALIASES.get(word, word)
            if word in SUPERTYPES:
                continue
            name = word if word in RANK else "Other"
            if name not in out:
                out.append(name)
    return out or ["Other"]


def primary(type_line: str | None) -> str:
    """The one section a card is actually listed in."""
    return min(types_of(type_line), key=lambda t: RANK[t])


def order(entry):
    """Mana value, then mana symbols, then name -- how a decklist reads."""
    cmc = (entry[4] if len(entry) > 4 else 0) or 0
    colors = entry[2] if len(entry) > 2 else ""
    return (cmc, mana_key(colors), entry[0].lower())


def group(entries: list) -> list:
    """Card entries -> [(type, shown entries, elsewhere count)], in DISPLAY order.

    `elsewhere` is how many cards carry this type but are listed under an
    earlier one, which is what the (+n) in a section title reports.
    """
    shown: dict = {}
    elsewhere: dict = {}
    for e in entries:
        line = e[3] if len(e) > 3 else ""
        home = primary(line)
        shown.setdefault(home, []).append(e)
        for t in types_of(line):
            if t != home:
                elsewhere[t] = elsewhere.get(t, 0) + 1

    out = []
    for t in DISPLAY:
        # A section with nothing in it is not worth a heading, even when cards
        # of that type exist elsewhere -- one Kindred instant should not buy a
        # Kindred column that lists nothing.
        if t in shown:
            cards = sorted(shown.get(t, []), key=order)
            out.append((t, cards, elsewhere.get(t, 0)))
    return out
