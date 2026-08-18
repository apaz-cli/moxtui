"""Local corpus: deck rows, deck bodies, users, and the completeness ledger.

Two tiers, because they cost two orders of magnitude apart. A search *row*
carries every field of a deck except its card list and arrives 100 to a request;
a *body* carries the cards and costs one request each. So rows are the thing we
enumerate and iterate, and a body is fetched only when a query asks something a
row cannot answer.

The `cells` table is what makes completeness durable. Every server-side query
region we have closed is recorded with how it was closed, so a crawl is
resumable, a second query pays only for regions not already covered, and an
answer can state its own provenance instead of hoping.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS decks (
  public_id TEXT PRIMARY KEY,
  author TEXT, format TEXT, bracket INT,
  created TEXT, updated TEXT,
  views INT, likes INT,
  color_identity TEXT, main_card_id TEXT, name TEXT, hubs TEXT,
  main_ct INT, side_ct INT, maybe_ct INT,
  row_seen_at REAL,
  body_seen_at REAL          -- NULL = row only, cards never fetched
);
CREATE INDEX IF NOT EXISTS decks_author ON decks(author);
CREATE INDEX IF NOT EXISTS decks_created ON decks(created);

CREATE TABLE IF NOT EXISTS deck_cards (
  public_id TEXT, board TEXT, card_name TEXT, qty INT,
  colors TEXT,        -- the card's own colours, e.g. "UR"; "" is colourless
  type_line TEXT,
  cmc REAL,
  unique_card_id TEXT -- joins to `cards`
);
CREATE INDEX IF NOT EXISTS deck_cards_id ON deck_cards(public_id);
CREATE INDEX IF NOT EXISTS deck_cards_name ON deck_cards(card_name);

-- Card detail, harvested from every deck body we fetch. The payload carries all
-- of this already and we used to throw it away; keeping it here rather than on
-- deck_cards means one row per card instead of one per card per deck.
CREATE TABLE IF NOT EXISTS cards_detail (
  unique_card_id TEXT PRIMARY KEY,
  name TEXT, mana_cost TEXT, type_line TEXT, oracle_text TEXT,
  power TEXT, toughness TEXT, loyalty TEXT, flavor_text TEXT,
  set_code TEXT, set_name TEXT, released_at TEXT, rarity TEXT, artist TEXT,
  scryfall_id TEXT,
  -- `layout` decides whether there is a back to turn to at all: a transform or
  -- modal card has a second image, a split or adventure card puts both faces on
  -- one picture.
  layout TEXT,
  back_name TEXT, back_mana_cost TEXT, back_type_line TEXT, back_oracle_text TEXT,
  back_power TEXT, back_toughness TEXT, back_flavor_text TEXT,
  -- The earliest printing, filled in lazily: the deck's own printing is
  -- whatever the builder happened to pick.
  orig_set TEXT, orig_set_name TEXT, orig_released_at TEXT,
  orig_scryfall_id TEXT, orig_artist TEXT
);

CREATE TABLE IF NOT EXISTS users (
  user_name TEXT PRIMARY KEY, deck_count INT, last_swept REAL
);

-- One row per server-side query region we have finished with.
CREATE TABLE IF NOT EXISTS cells (
  params_hash TEXT PRIMARY KEY,
  params TEXT, status TEXT, evidence TEXT, decks_found INT, closed_at REAL
);

-- Which decks a closed cell contained, so a repeat query pays nothing.
CREATE TABLE IF NOT EXISTS cell_decks (
  params_hash TEXT, public_id TEXT
);
CREATE INDEX IF NOT EXISTS cell_decks_hash ON cell_decks(params_hash);

CREATE TABLE IF NOT EXISTS cards (
  query TEXT PRIMARY KEY, card_id TEXT, full_name TEXT, ci TEXT
);
"""

ROW_COLS = ("public_id author format bracket created updated views likes "
            "color_identity main_card_id name hubs main_ct side_ct maybe_ct").split()


def row_of(r: dict) -> tuple:
    """Search-result row -> our columns."""
    return (
        r.get("publicId"),
        (r.get("createdByUser") or {}).get("userName"),
        r.get("format"), r.get("bracket"),
        r.get("createdAtUtc"), r.get("lastUpdatedAtUtc"),
        r.get("viewCount"), r.get("likeCount"),
        "".join(r.get("colorIdentity") or []),
        r.get("mainCardId"), r.get("name"),
        ",".join(r.get("hubNames") or []),
        r.get("mainboardCount"), r.get("sideboardCount"), r.get("maybeboardCount"),
    )


class Store:
    def __init__(self, path: str = "moxfield.sqlite"):
        self.path = path
        self._local = threading.local()
        self._open: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.db.executescript(SCHEMA)
        # Card colours arrived after the first bodies were stored. They come free
        # in the payload we already fetch, so the fix is to drop what we have and
        # let it re-fetch rather than carry two shapes around.
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(deck_cards)")}
        detail = {r[1] for r in self.db.execute("PRAGMA table_info(cards_detail)")}
        if (not {"colors", "type_line", "cmc", "unique_card_id"} <= cols
                or not {"layout", "back_name"} <= detail):
            self.db.execute("DROP TABLE IF EXISTS deck_cards")
            self.db.execute("DROP TABLE IF EXISTS cards_detail")
            self.db.executescript(SCHEMA)
            self.db.execute("UPDATE decks SET body_seen_at = NULL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()

    # One connection per thread, opened on first use. The TUI reads card detail
    # on the UI thread while its art worker reads the same row from its own, and
    # a connection caches one prepared statement per SQL string: same query from
    # two threads is one statement being reset under a live fetch, which sqlite
    # reports as "bad parameter or other API misuse". A connection per thread is
    # a statement cache per thread. WAL belongs to the file rather than the
    # connection, so the constructor sets it once and every later connection
    # reads while one of them writes; the timeout is what serialises writers.
    @property
    def db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            # check_same_thread guards against the sharing this property makes
            # impossible anyway, and leaving it on would stop close() from
            # releasing a worker's connection -- which Windows needs, since it
            # will not delete a database file anyone still holds open.
            db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            db.row_factory = sqlite3.Row
            self._local.db = db
            with self._lock:
                self._open.append(db)
        return db

    def close(self) -> None:
        """Every thread's connection, not just this one's: a worker that has
        finished leaves its handle behind until the collector gets to it."""
        with self._lock:
            open_, self._open = self._open, []
        for db in open_:
            db.close()
        self._local.db = None

    # ---- rows --------------------------------------------------------------

    def put_rows(self, rows: list) -> set:
        """Upsert search rows, keeping any body we already have. Returns the ids
        that were not already known -- which is what the tail crawler's overlap
        check is made of."""
        now = time.time()
        vals = [row_of(r) + (now,) for r in rows]
        new = {v[0] for v in vals} - self.known_ids([v[0] for v in vals])
        self.db.executemany(
            f"INSERT INTO decks ({','.join(ROW_COLS)}, row_seen_at) "
            f"VALUES ({','.join('?' * (len(ROW_COLS) + 1))}) "
            f"ON CONFLICT(public_id) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in ROW_COLS[1:])
            + ", row_seen_at=excluded.row_seen_at",
            vals,
        )
        authors = {v[1] for v in vals if v[1]}
        self.db.executemany(
            "INSERT OR IGNORE INTO users (user_name) VALUES (?)",
            [(a,) for a in authors],
        )
        self.db.commit()
        return new

    def row(self, pid: str):
        return self.db.execute(
            "SELECT * FROM decks WHERE public_id = ?", (pid,)
        ).fetchone()

    def known_ids(self, pids: list) -> set:
        got = set()
        for i in range(0, len(pids), 900):
            chunk = pids[i:i + 900]
            got |= {r[0] for r in self.db.execute(
                f"SELECT public_id FROM decks WHERE public_id IN "
                f"({','.join('?' * len(chunk))})", chunk)}
        return got

    def rows(self, pids: list):
        out = {}
        for i in range(0, len(pids), 900):
            chunk = pids[i:i + 900]
            q = f"SELECT * FROM decks WHERE public_id IN ({','.join('?' * len(chunk))})"
            for r in self.db.execute(q, chunk):
                out[r["public_id"]] = r
        return out

    # ---- bodies ------------------------------------------------------------

    def put_body(self, pid: str, boards: dict) -> None:
        self.db.execute("DELETE FROM deck_cards WHERE public_id = ?", (pid,))
        self.db.executemany(
            "INSERT INTO deck_cards VALUES (?,?,?,?,?,?,?,?)",
            [(pid, b) + tuple(c) for b, cards in boards.items() for c in cards],
        )
        self.db.execute(
            "UPDATE decks SET body_seen_at = ? WHERE public_id = ?", (time.time(), pid)
        )
        self.db.commit()

    def body(self, pid: str) -> dict | None:
        r = self.db.execute(
            "SELECT body_seen_at FROM decks WHERE public_id = ?", (pid,)
        ).fetchone()
        if not r or r["body_seen_at"] is None:
            return None
        boards: dict = {}
        for c in self.db.execute(
            "SELECT board, card_name, qty, colors, type_line, cmc, unique_card_id "
            "FROM deck_cards WHERE public_id = ?", (pid,)
        ):
            boards.setdefault(c["board"], []).append(
                (c["card_name"], c["qty"], c["colors"], c["type_line"], c["cmc"],
                 c["unique_card_id"]))
        return boards

    def have_bodies(self, pids: list) -> set:
        got = set()
        for i in range(0, len(pids), 900):
            chunk = pids[i:i + 900]
            q = (f"SELECT public_id FROM decks WHERE body_seen_at IS NOT NULL "
                 f"AND public_id IN ({','.join('?' * len(chunk))})")
            got |= {r[0] for r in self.db.execute(q, chunk)}
        return got

    def put_cards(self, cards: list) -> None:
        """Upsert harvested card detail, without clobbering an original printing
        we already looked up."""
        self.db.executemany(
            "INSERT INTO cards_detail (unique_card_id, name, mana_cost, type_line,"
            " oracle_text, power, toughness, loyalty, flavor_text, set_code,"
            " set_name, released_at, rarity, artist, scryfall_id, layout,"
            " back_name, back_mana_cost, back_type_line, back_oracle_text,"
            " back_power, back_toughness, back_flavor_text) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(unique_card_id) DO NOTHING", cards)

    def card(self, unique_card_id: str):
        return self.db.execute(
            "SELECT * FROM cards_detail WHERE unique_card_id = ?",
            (unique_card_id,)).fetchone()

    def put_original(self, unique_card_id: str, orig: tuple) -> None:
        self.db.execute(
            "UPDATE cards_detail SET orig_set=?, orig_set_name=?, "
            "orig_released_at=?, orig_scryfall_id=?, orig_artist=? "
            "WHERE unique_card_id = ?", orig + (unique_card_id,))
        self.db.commit()

    # ---- users -------------------------------------------------------------

    def user_counts(self, names) -> dict:
        out = {}
        names = list(names)
        for i in range(0, len(names), 900):
            chunk = names[i:i + 900]
            q = (f"SELECT user_name, deck_count FROM users "
                 f"WHERE user_name IN ({','.join('?' * len(chunk))})")
            out.update({r[0]: r[1] for r in self.db.execute(q, chunk)})
        return out

    def set_user_count(self, name: str, n: int) -> None:
        self.db.execute(
            "INSERT INTO users (user_name, deck_count, last_swept) VALUES (?,?,?) "
            "ON CONFLICT(user_name) DO UPDATE SET deck_count=excluded.deck_count,"
            " last_swept=excluded.last_swept",
            (name, n, time.time()),
        )

    def unswept_users(self, limit: int) -> list:
        return [r[0] for r in self.db.execute(
            "SELECT user_name FROM users WHERE last_swept IS NULL LIMIT ?", (limit,)
        )]

    # ---- ledger ------------------------------------------------------------

    @staticmethod
    def cell_key(params: dict) -> str:
        return json.dumps(sorted(params.items()), separators=(",", ":"))

    def cell(self, params: dict):
        return self.db.execute(
            "SELECT * FROM cells WHERE params_hash = ?", (self.cell_key(params),)
        ).fetchone()

    def close_cell(self, params: dict, status: str, evidence: str, n: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cells VALUES (?,?,?,?,?,?)",
            (self.cell_key(params), json.dumps(params), status, evidence, n, time.time()),
        )
        self.db.commit()

    def cell_members(self, key: str) -> set:
        return {r[0] for r in self.db.execute(
            "SELECT public_id FROM cell_decks WHERE params_hash = ?", (key,))}

    def put_cell_members(self, key: str, pids) -> None:
        self.db.execute("DELETE FROM cell_decks WHERE params_hash = ?", (key,))
        self.db.executemany("INSERT INTO cell_decks VALUES (?,?)",
                            [(key, p) for p in pids])
        self.db.commit()

    def stats(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]
        return {
            "decks": one("SELECT count(*) FROM decks"),
            "bodies": one("SELECT count(*) FROM decks WHERE body_seen_at IS NOT NULL"),
            "users": one("SELECT count(*) FROM users"),
            "users_swept": one("SELECT count(*) FROM users WHERE last_swept IS NOT NULL"),
            "cells": one("SELECT count(*) FROM cells"),
            "unverified": one("SELECT count(*) FROM cells WHERE status='unverified'"),
            "oldest": one("SELECT min(created) FROM decks") or "-",
            "newest": one("SELECT max(created) FROM decks") or "-",
        }
