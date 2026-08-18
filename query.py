"""Scryfall-flavoured query language: text -> AST -> typed terms -> predicate.

  parse()     text -> AST of raw terms
  resolve()   raw terms -> typed terms (card names resolved against the API)
  evaluate()  AST x (row, body) -> bool

Planning lives in engine.py, which turns the same AST into set algebra over
server-enumerable id sets. `evaluate` is the local source of truth for anything
the algebra leaves as a residual.

A search row answers every non-card term, so only card terms ever need a body --
and only the ones the server cannot express exactly (copy counts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from api import QueryError, name_variants, norm

# ---------------------------------------------------------------- vocabulary

# Board scope -> (server param, server `board` value, board name in a deck body).
SCOPES = {
    "card": ("cardId", None, None), "cards": ("cardId", None, None),
    "ca": ("cardId", None, None),
    "main": ("cardId", "mainboard", "mainboard"),
    "mainboard": ("cardId", "mainboard", "mainboard"),
    "md": ("cardId", "mainboard", "mainboard"),
    "side": ("cardId", "sideboard", "sideboard"),
    "sideboard": ("cardId", "sideboard", "sideboard"),
    "sb": ("cardId", "sideboard", "sideboard"),
    "maybe": ("cardId", "maybeboard", "maybeboard"),
    "maybeboard": ("cardId", "maybeboard", "maybeboard"),
    "mb": ("cardId", "maybeboard", "maybeboard"),
    "cmdr": ("commanderCardId", None, "commanders"),
    "commander": ("commanderCardId", None, "commanders"),
    "general": ("commanderCardId", None, "commanders"),
    "partner": ("partnerCardId", None, "commanders"),
    "companion": ("companionCardId", None, "companions"),
    "signature": ("commanderSignatureSpellCardId", None, "signatureSpells"),
    "spell": ("commanderSignatureSpellCardId", None, "signatureSpells"),
}

FORMAT_KEYS = {"f", "fmt", "format"}
BRACKET_KEYS = {"bracket", "br"}
AUTHOR_KEYS = {"by", "author", "user"}
DECKNAME_KEYS = {"name", "deck", "title"}

# Row fields checkable locally. The first three are also *sortable* server-side,
# which lets engine.py enumerate them directly (see PREFIX_SORT).
NUMERIC = {"views": "views", "view": "views", "likes": "likes", "like": "likes"}
DATES = {"created": "created", "date": "created", "year": "created",
         "updated": "updated"}
TEXTUAL = {"hub": "hubs", "hubs": "hubs", "ci": "color_identity",
           "identity": "color_identity"}

# `likes`/`views`/`created` descending is a sorted prefix of the cell, so a
# `>` bound on one of them is a complete generator on its own.
PREFIX_SORT = {"views": "views", "likes": "likes", "created": "created"}

FORMATS = [
    "alchemy", "alpha40", "archon", "brawl", "brawlPrecons", "centurion",
    "commander", "commanderPrecons", "competitiveBrawl", "conquest", "dandan",
    "duelCommander", "duelCommanderRussian", "frontier", "gladiator",
    "highlanderAustralian", "highlanderCanadian", "highlanderEuropean",
    "highlanderGauntlet", "historic", "historicBrawl", "legacy", "leviathan",
    "modern", "none", "oathbreaker", "oldSchool", "pauper", "pauperEdh",
    "pendragon", "pennyDreadful", "pioneer", "precons", "predh", "premodern",
    "primordial", "secretLair", "standard", "timeless", "tinyLeaders",
    "valueVintage", "vintage",
]
# `tlr` is in the frontend enum but search answers 400 for it, so it is not a
# partition value. `f:tlr` from a user still resolves and is checked locally.
FORMAT_ALIASES = {
    "edh": "commander", "cedh": "commander", "tlr": "tlr",
    "pdh": "pauperEdh", "paupercommander": "pauperEdh", "pauperedh": "pauperEdh",
    "duel": "duelCommander", "dc": "duelCommander", "penny": "pennyDreadful",
    "casual": "none", "unknown": "none", "constructed": "none",
    "canlander": "highlanderCanadian", "auslander": "highlanderAustralian",
    "eurolander": "highlanderEuropean", "ob": "oathbreaker",
}


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve_format(v: str) -> str:
    k = _fold(v)
    if k in FORMAT_ALIASES:
        return FORMAT_ALIASES[k]
    for f in FORMATS:
        if _fold(f) == k:
            return f
    raise QueryError(
        f"unknown format {v!r}; try commander, modern, legacy, pauper, vintage, "
        f"none (full list in FINDINGS.md)")


# ------------------------------------------------------------------- nodes

@dataclass
class Raw:
    key: str | None
    op: str
    value: str


@dataclass
class And:
    children: list


@dataclass
class Or:
    children: list


@dataclass
class Not:
    child: object


@dataclass
class CardTerm:
    """A card that must be present. Exactly expressible server-side unless it
    carries a copy count, which only a deck body can settle."""
    name: str
    card_id: str
    param: str                # cardId | commanderCardId | ...
    board: str | None         # server-side board narrowing
    body_board: str | None    # board to check in a deck body; None = anywhere
    qty: int = 1

    def params(self) -> dict:
        p = {self.param: self.card_id}
        if self.board:
            p["board"] = self.board
        return p


@dataclass
class Refine:
    """Pushable into server params, and checkable on a row."""
    key: str                  # fmt | bracket | author | deckname
    op: str
    value: object


@dataclass
class Local:
    """Checkable on a row; some are enumerable as a sorted prefix."""
    key: str                  # views | likes | created | updated | hubs | ...
    op: str
    value: object


# ------------------------------------------------------------------- lexing

def tokenize(text: str) -> list:
    toks: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            toks.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and not text[i + 1].isspace():
            toks.append("-")
            i += 1
            continue
        m = re.match(r"(?i)or(?=\s|\(|$)", text[i:])
        if m:
            toks.append("or")
            i += m.end()
            continue

        key, op = None, ":"
        km = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*(!=|>=|<=|:|=|>|<)", text[i:])
        if km:
            key, op = km.group(1).lower(), km.group(2)
            i += km.end()

        if i < n and text[i] in "\"'":
            q, j, buf = text[i], i + 1, []
            while j < n and text[j] != q:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise QueryError(f"unterminated quote near {text[i:i+24]!r}")
            value, i = "".join(buf), j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            value, i = text[i:j], j
        if not value:
            raise QueryError(f"empty value for {key!r}" if key else "empty term")
        toks.append(Raw(key, op, value))
    return toks


class _Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def parse(self):
        node = self.parse_or()
        if self.peek() is not None:
            raise QueryError("unbalanced ')' in query")
        return node

    def parse_or(self):
        parts = [self.parse_and()]
        while self.peek() == "or":
            self.i += 1
            parts.append(self.parse_and())
        return parts[0] if len(parts) == 1 else Or(parts)

    def parse_and(self):
        parts = []
        while True:
            tok = self.peek()
            if tok is None or tok in (")", "or"):
                break
            parts.append(self.parse_unary())
        if not parts:
            raise QueryError("empty expression (check parentheses / 'or')")
        return parts[0] if len(parts) == 1 else And(parts)

    def parse_unary(self):
        if self.peek() == "-":
            self.i += 1
            return Not(self.parse_unary())
        return self.parse_atom()

    def parse_atom(self):
        tok = self.peek()
        if tok == "(":
            self.i += 1
            node = self.parse_or()
            if self.peek() != ")":
                raise QueryError("missing ')' in query")
            self.i += 1
            return node
        if isinstance(tok, Raw):
            self.i += 1
            return tok
        raise QueryError(f"unexpected {tok!r} in query")


def parse(text: str):
    toks = tokenize(text)
    if not toks:
        raise QueryError("empty query")
    return _Parser(toks).parse()


# ----------------------------------------------------------------- resolving

_QTY = re.compile(r"^\s*(\d+)\s*x\s+(.*)$", re.I)


def _int(v, what):
    try:
        return int(v)
    except ValueError:
        raise QueryError(f"{what} needs a number, got {v!r}")


def resolve(node, client):
    if isinstance(node, And):
        return And([resolve(c, client) for c in node.children])
    if isinstance(node, Or):
        return Or([resolve(c, client) for c in node.children])
    if isinstance(node, Not):
        return Not(resolve(node.child, client))
    if not isinstance(node, Raw):
        return node

    key, op, val = node.key, node.op, node.value
    if key is None or key in SCOPES:
        qty = 1
        m = _QTY.match(val)
        if m:
            qty, val = int(m.group(1)), m.group(2).strip()
        param, board, body_board = SCOPES.get(key or "card")
        cid, full = client.resolve_card(val)
        return CardTerm(full, cid, param, board, body_board, qty)

    if key in FORMAT_KEYS:
        return Refine("fmt", op, resolve_format(val))
    if key in BRACKET_KEYS:
        return Refine("bracket", op, _int(val, "bracket"))
    if key in AUTHOR_KEYS:
        return Refine("author", op, val)
    if key in DECKNAME_KEYS:
        return Refine("deckname", op, val)
    if key in NUMERIC:
        return Local(NUMERIC[key], op, _int(val, key))
    if key in DATES:
        return Local(DATES[key], op, val if len(val) > 4 else f"{val}-01-01")
    if key in TEXTUAL:
        return Local(TEXTUAL[key], op, val)

    raise QueryError(
        f"unknown filter {key!r}. Available: card, cmdr, partner, companion, "
        f"signature, main, side, maybe, f/format, bracket, by/author, name, "
        f"views, likes, created, updated, hub, ci")


# ---------------------------------------------------------------- evaluating

def compare(a, op, b) -> bool:
    if a is None:
        return False
    if op in (":", "="):
        return a == b
    return {"!=": a != b, ">": a > b, "<": a < b,
            ">=": a >= b, "<=": a <= b}.get(op, False)


def needs_body(node) -> bool:
    """Only a copy count needs the card list; everything else lives on the row
    or is settled exactly by the server."""
    if isinstance(node, (And, Or)):
        return any(needs_body(c) for c in node.children)
    if isinstance(node, Not):
        return needs_body(node.child)
    return isinstance(node, CardTerm) and node.qty > 1


def _counts(body: dict, board: str | None) -> dict:
    """normalized card name (each face) -> copies in scope."""
    out: dict = {}
    for b, cards in body.items():
        if board and b != board:
            continue
        for name, qty in cards:
            for v in name_variants(name):
                out[v] = out.get(v, 0) + qty
    return out


def evaluate(node, row, body=None) -> bool:
    if isinstance(node, And):
        return all(evaluate(c, row, body) for c in node.children)
    if isinstance(node, Or):
        return any(evaluate(c, row, body) for c in node.children)
    if isinstance(node, Not):
        return not evaluate(node.child, row, body)

    if isinstance(node, CardTerm):
        if body is None:
            return False
        return _counts(body, node.body_board).get(norm(node.name), 0) >= node.qty

    if isinstance(node, Refine):
        if node.key == "fmt":
            return compare(row["format"], node.op, node.value)
        if node.key == "bracket":
            return compare(row["bracket"], node.op, node.value)
        if node.key == "author":
            return compare((row["author"] or "").lower(), node.op, node.value.lower())
        hay, needle = (row["name"] or "").lower(), str(node.value).lower()
        return (needle not in hay) if node.op == "!=" else (needle in hay)

    if isinstance(node, Local):
        v = row[node.key]
        if node.key in ("hubs", "color_identity"):
            hit = str(node.value).lower() in (v or "").lower()
            return (not hit) if node.op == "!=" else hit
        if node.key in ("created", "updated") and isinstance(v, str):
            v = v[:len(node.value)]
        return compare(v, node.op, node.value)
    return False


# ------------------------------------------------------------------ display

def card_names(node, out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(node, (And, Or)):
        for c in node.children:
            card_names(c, out)
    elif isinstance(node, Not):
        card_names(node.child, out)
    elif isinstance(node, CardTerm):
        out[node.card_id] = node.name
    return out


def describe(node, indent: str = "") -> str:
    if isinstance(node, (And, Or)):
        head = "AND" if isinstance(node, And) else "OR"
        return f"{indent}{head}\n" + "".join(
            describe(c, indent + "  ") for c in node.children)
    if isinstance(node, Not):
        return f"{indent}NOT\n" + describe(node.child, indent + "  ")
    if isinstance(node, CardTerm):
        scope = node.body_board or "any board"
        q = f"{node.qty}x " if node.qty > 1 else ""
        return f"{indent}{q}{node.name}  [{scope}]\n"
    if isinstance(node, (Refine, Local)):
        return f"{indent}{node.key} {node.op} {node.value}\n"
    return f"{indent}?\n"
