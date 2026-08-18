"""Original printings and card images, from Scryfall.

Moxfield tells us which printing a deck uses, which is whatever the builder
happened to pick. For the printing a card *first* had, Scryfall answers in one
request and hands back the image URL with it -- Moxfield's own editions endpoint
lists every printing but carries no Scryfall id, so it would need a second
lookup per card to reach a picture.

Everything here is cached permanently: a card's first printing does not change.
"""

from __future__ import annotations

import getpass
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.scryfall.com"
CDN = "https://cards.scryfall.io"
UA = os.environ.get("SCRYFALL_UA", "moxfield-deck-search/0.1")
# Scryfall ask for 50-100ms between requests. This is well inside that, and the
# results are cached forever, so a session makes a handful of calls at most.
INTERVAL = 0.12


def default_cache_dir() -> str:
    """Where downloaded card images live.

    The system temp directory, asked for rather than hard-coded: `gettempdir`
    honours TMPDIR/TEMP/TMP and lands on /tmp on Unix and %TEMP% on Windows
    without either appearing here. Every file is re-downloadable from an id in
    `cards_detail`, so losing the lot to a reboot costs only bandwidth.

    The user name is in the directory name because /tmp is shared: without it
    the first user to run this owns a directory the next one cannot write to.
    """
    try:
        who = getpass.getuser()
    except Exception:                                   # no passwd entry
        who = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return os.path.join(tempfile.gettempdir(), f"moxfield-images-{who}")


class Scryfall:
    def __init__(self, store, cache_dir: str | None = None):
        self.store = store
        self.dir = cache_dir or default_cache_dir()
        self._last = 0.0

    def _get(self, url: str, binary: bool = False):
        gap = time.monotonic() - self._last
        if gap < INTERVAL:
            time.sleep(INTERVAL - gap)
        self._last = time.monotonic()
        req = urllib.request.Request(
            url, headers={"User-Agent": UA,
                          "Accept": "*/*" if binary else "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        return body if binary else json.loads(body)

    # ---- original printing -------------------------------------------------

    def original(self, unique_card_id: str, name: str) -> dict | None:
        """The card's first printing, cached into cards_detail.

        Returns the stored row's original columns, or None if Scryfall does not
        recognise the name -- which happens for tokens and a few oddities, and
        is not worth retrying in a loop.
        """
        row = self.store.card(unique_card_id)
        if row is None:
            return None
        if row["orig_scryfall_id"]:
            return dict(row)
        query = urllib.parse.urlencode({
            "q": f'!"{name}"', "unique": "prints",
            "order": "released", "dir": "asc"})
        try:
            data = self._get(f"{API}/cards/search?{query}")
        except urllib.error.HTTPError as e:
            if e.code == 404:                       # no such card by that name
                self.store.put_original(unique_card_id, ("", "", "", "", ""))
                return dict(self.store.card(unique_card_id))
            raise
        first = (data.get("data") or [None])[0]
        if not first:
            return dict(row)
        self.store.put_original(unique_card_id, (
            first.get("set") or "", first.get("set_name") or "",
            first.get("released_at") or "", first.get("id") or "",
            first.get("artist") or ""))
        return dict(self.store.card(unique_card_id))

    # ---- images ------------------------------------------------------------

    def image_path(self, scryfall_id: str, size: str = "normal",
                   face: str = "front") -> str | None:
        """A local path to the card image, downloading it once if need be.

        Returns None rather than raising: the image is decoration on a panel
        that already works without it, so nothing here should be able to break
        the view.
        """
        if not scryfall_id:
            return None
        os.makedirs(self.dir, exist_ok=True)
        suffix = "" if face == "front" else f"-{face}"
        path = os.path.join(self.dir, f"{scryfall_id}-{size}{suffix}.jpg")
        if os.path.exists(path):
            return path
        url = (f"{CDN}/{size}/{face}/{scryfall_id[0]}/{scryfall_id[1]}/"
               f"{scryfall_id}.jpg")
        try:
            data = self._get(url, binary=True)
        except Exception:
            return None
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)       # never leave a half-written file behind
        return path
