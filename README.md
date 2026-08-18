# Moxfield deck search

Moxfield's deck search accepts exactly **one** card constraint and never returns
more than 10,000 results. This gives you a real query language over the same API
— boolean logic, negation, board scoping, copy counts — and, unlike the site,
enumerates *every* deck that matches, saying so explicitly when it cannot.

See [DESIGN.md](DESIGN.md) for how completeness is established and
[FINDINGS.md](FINDINGS.md) for the reverse-engineered API.

## Usage

```bash
python3 moxfield.py search 'cmdr:"Rograkh, Son of Rohgahh" card:"Cloudstone Curio"'
python3 moxfield.py search 'f:edh (card:Windfall or card:"Wheel of Fortune") -card:"Mana Crypt"'
python3 moxfield.py search 'f:modern main:"4x Lightning Bolt" likes>10'
python3 moxfield.py explain 'f:edh likes>1000'      # plan and cost, fetches nothing
python3 moxfield.py tail --watch 3600               # ingest the new-deck stream
python3 moxfield.py sweep                           # enumerate known users' decks
python3 moxfield.py tui                             # interactive
```

Results stream as they are confirmed, so `| head` and `--limit` are cheap.

In the TUI, running a search abandons whichever one was still going. Textual
cannot kill a thread, so the old crawl stands down at its next request rather
than being killed mid-flight, and stops putting rows in a table that now belongs
to a different query. Cancellation is per thread, so the deck-body fetch behind
the card panel is untouched.

## Query language

Terms are ANDed by juxtaposition. `or` joins, `-` negates, parentheses group.

```
cmdr:"Rograkh, Son of Rohgahh" (card:Windfall or card:"Wheel of Fortune") -f:none
```

### Cards

| Term | Matches |
|---|---|
| `card:"Sol Ring"`, `"Sol Ring"`, `Windfall` | anywhere in the deck |
| `main:` `side:` `maybe:` | that board only |
| `cmdr:` / `commander:` | the command zone |
| `partner:` `companion:` `signature:` | those slots |
| `card:"4x Lightning Bolt"` | at least 4 copies |

A quoted name is the card; the key is optional. Split, double-faced and adventure
cards can be named by either face — `Stomp`, `Bonecrusher Giant`, and
`Bonecrusher Giant // Stomp` are the same card. Names are matched exactly, and
anything ambiguous is reported rather than guessed.

Two command-zone terms use both slots, which is exact for partner pairs:
`cmdr:"Rograkh, Son of Rohgahh" cmdr:"Silas Renn, Seeker Adept"`.

### Filters

| Term | Notes |
|---|---|
| `f:` `format:` `fmt:` | `f:edh` = `format:commander`; also `pdh`, `duel`, `penny`, `casual` |
| `bracket:4` `bracket>=4` | Commander only |
| `by:` `author:` `user:` | deck author |
| `name:` `deck:` `title:` | substring of the deck title |
| `views>1000` `likes>=50` | |
| `created>2024` `updated>2024-06-01` | year or full date |
| `hub:budget` `ci:UB` | deck hub / colour identity |

Operators: `:` `=` `!=` `>` `<` `>=` `<=`.

### What can start a search

A query needs something that can generate candidates: a positive **card** or
**command-zone** term outside an `or` branch, or a **lower bound** on `likes`,
`views` or `created` (which the API can serve as a sorted prefix). `f:edh`
alone cannot — it would match millions — and a negated term never can.

## Cost

Three things make queries cheap, and `explain` shows all of them:

- **Rows, not decks.** A search row carries every field except the card list and
  arrives 100 to a request, so filters like `likes>10` cost nothing. A deck body
  is one request each and is fetched only for copy counts.
- **Set algebra.** AND intersects, OR unions, NOT subtracts. `card:A card:B` at
  5k and 3k decks is 80 page requests, not 3,000 deck fetches; `-card:X` is a
  subtraction rather than a filter needing every candidate's cards.
- **The ledger.** Every region already enumerated is recorded with its members,
  so overlapping queries are nearly free and an interrupted crawl resumes.

```
$ python3 moxfield.py explain 'cmdr:"Rograkh, Son of Rohgahh" card:"Cloudstone Curio"'
AND
  Rograkh, Son of Rohgahh  [commanders]
  Cloudstone Curio  [any board]

candidates: 1732  [exact]
  commanderCardId=YMXJd, cardId=6JO99 -> 1732
estimated cost: 20 requests (~0 min at 1.4 req/s)
```

`search` prices the query the same way before it starts, and asks first if the
answer is large or will need subdividing (`-y` skips).

AND and OR pull in opposite directions: AND can be driven by whichever term is
cheapest, so adding terms makes a query *faster*, while OR must cover every
branch.

## Completeness

Every search reports `exact` or `unverified`, and the reason. A region under the
10,000-result window is drained directly; above it, the crawler subdivides by
format, then Commander bracket, then **author batches** — `authorUserNames` takes
comma-separated lists, one deck has one creator, and a single user is always far
below the window, so a user's deck list is exact. That last axis is what makes
the answer a proof rather than an estimate, and it works in formats with no
command zone.

Where a result still cannot be confirmed, it says so instead of implying
otherwise:

```
completeness: unverified
  exhausted every window at 19999 decks
```

`tail` keeps the corpus current: the newest-10,000 window is ~18 hours deep, so
polling it more often than that sees every deck created from now on, for about
130 requests a day.

## The corpus

`moxfield.sqlite` holds deck rows, the card lists of decks actually fetched, the
user list, and the ledger. It grows into a local dataset you can query directly:

```sql
SELECT author, count(*) FROM decks WHERE format='commander' GROUP BY 1 ORDER BY 2 DESC;
```

`datasette moxfield.sqlite` is a good browser over it.

## TUI

`python3 moxfield.py tui` (needs `pip install textual`).

Query bar on top, results streaming into the table on the left, and a log on the
right carrying the plan, its cost, and each region the crawl enters with how it
closed.

Along the bottom is a progress bar with a request budget, taken from the same
dry plan `explain` prints. It runs in two phases with different denominators:

```
enumerating · 412 of ~1,240 requests · 38,100 decks seen · 412 requests
checking · 812 of 1,732 decks · 40 matches · 1,244 requests
```

A query that will subdivide can only be given a *lower* bound, so it shows `≥`
and the bar grows rather than pinning at 100% and implying the run is over.

Clicking a column header sorts by it: **deck** and **author** alphabetically,
**likes** and **views** highest first, **created** newest first, and **color**
by number of colours, WUBRG-ordered within each count (`C W U B R G WU WG UG BR
RG …`). Clicking again reverses it, and a third click restores the order results
arrived in.

`ctrl+c` and `esc` quit from every screen. `q` quits the results screen and
backs out of a deck. (`q` types a letter while the query bar or the builder's
form has focus, which is why the footer's visible entry names the other keys
too.)

| key | |
|---|---|
| `enter`, `→`, `v` | open the decklist, full screen |
| `q`, `←`, `⌫` | back, from the deck view |
| `b` | build a query from a form, with a live preview of the query it writes |
| click / `enter` | copy the deck's link |
| `o` | open the deck in a browser |

The deck view is a full screen rather than a popup: a 100-card singleton list
wants every row the terminal has. The **mainboard** is broken into card types laid side by side across the width:
Creature, Planeswalker, Sorcery, Instant, Kindred, Artifact, Enchantment,
Battle, Land, then Other for planes, schemes and sticker sheets. Every other
board — command zone, sideboard, maybeboard — is just its cards under the board
name, since headings there are scaffolding around nothing.

Two orders are at work, because they answer different questions. Sections are
*shown* in the order above, with lands last so they do not bury the spells. But
a multi-type card is *counted* under Creature, Planeswalker, Land, Artifact,
Enchantment, Sorcery, Instant, Battle, Kindred — land high, so anything that
taps for mana lands in the count you actually take. Urza's Saga and Darksteel
Citadel are lands shown at the end, not an enchantment and an artifact hidden in
the middle.

A card is listed once, and every other section it belongs to says how many of
its cards are elsewhere: `Artifact (15) (+2)` means 15 artifacts listed plus two
more that are Artifacts but appear under Creature and Land. Within a section, cards sort by
mana value, then by mana symbols in WUBRG order (multicolour after mono,
colourless last), then by name.

Names are coloured by the card's own colour — mono takes its colour, two or more
take gold, artifacts and lands stay neutral — and quantities are left plain.

### The card panel

Arrow keys or `hjkl` move a cursor around the list, and clicking a card selects
it; either way a reserved panel shows that card in full — mana cost, type line,
oracle text, power/toughness or loyalty, flavour text, and the printing.

The panel goes **beside** the list on a wide terminal and **below** it on a
narrow or tall one, whichever leaves the decklist more room, and disappears
entirely rather than squeezing the list below readable. A half-block card image
is about 40 columns by 28 rows — wider than tall in cells, since a cell is
roughly twice as tall as it is wide — so the card's own shape does not argue for
one side or the other; the terminal's does.

Card detail costs nothing to collect: mana cost, oracle text, type line, rarity,
artist and set all arrive in the deck payloads already being fetched, so they
are harvested into a `cards_detail` table as decks are read. One row per card
rather than one per card per deck.

Downloaded images are cached in the system temp directory (`/tmp` on Unix,
`%TEMP%` on Windows — asked of Python rather than hard-coded), under a
per-user name since `/tmp` is shared. Losing them to a reboot costs only
bandwidth: every file is re-downloadable from an id in `cards_detail`.

The panel also shows the card's **original** printing and its picture. The
printing a deck uses is whichever one the builder picked, so the first printing
comes from one Scryfall query per card — which returns the image URL with it,
where Moxfield's own editions endpoint carries no Scryfall id and would need a
second lookup. Both are cached permanently, the lookup in `cards_detail` and the
picture in the temp directory, and both are fetched lazily when the cursor settles on
a card rather than per keypress.

**Clicking the card turns it over.** A transform or modal double-faced card
shows its other side — name, cost, type line, oracle text and picture — and
clicking again turns it back. Split, adventure and flip cards do not turn:
they have two faces but only one picture, so there is nothing on the other
side. The heading says which side you are looking at.

Pictures need `pip install textual-image`, which uses the terminal's graphics
protocol (Kitty, Sixel) where there is one and Unicode half-blocks where there
is not. When there is no picture to show — no library, no network, a card with
no image, or a file that will not decode — the picture is not merely blank, it
is removed, and the text takes the whole panel rather than leaving a reserved
hole.

`MOXFIELD_CARD_IMAGES` chooses the renderer: `0` for none, `auto` (the default),
or `tgp`/`kitty`, `sixel`, `halfcell`, `unicode` to force one. Forcing exists
because auto-detection is unreliable by construction — the library asks the
terminal over stdin and gives up after 100ms, so any multiplexer that does not
answer reads as "no support", and a terminal advertising a protocol it does not
honour reads as "supported".

Under **herdr** it picks Kitty graphics without asking. herdr reads Kitty
placements out of the pane and repaints them to the host terminal, so the
protocol works there — it simply never replies to the query, and detection would
otherwise fall back to half-blocks. `MOXFIELD_CARD_IMAGES=halfcell` overrides
that if the passthrough misbehaves. `o` and `c` open and copy from there too, and the deck's URL is printed in full
and marked up as a terminal hyperlink (OSC 8), so the terminal can open or copy
it without the app's help.

`←` only leaves when there is nothing to its left, so it backs out of the deck
you just opened without interfering with moving around the list.

`v`, `b` and `o` are single keys, so they only fire when the results table has
focus — while you are typing in the query bar they are just letters.

The builder is a form over the same language rather than a replacement for it —
the preview line is the exact query that will run, so filling it in is also a way
to learn the syntax.

## Windows

Nothing here is POSIX-specific — the code is standard library plus Textual, with
no `fcntl`, `termios`, `pty` or `signal` use — and Textual ships Windows 10 and
11 classifiers and is tested on both. So it runs on Windows as-is with
`pip install textual`.

Two things that are genuinely Windows-specific:

- **Use Windows Terminal**, not the legacy `conhost` console. The TUI renders in
  either, but only Windows Terminal gives truecolor (the mana pips need it) and
  OSC 52, which is how click-to-copy reaches the clipboard.
- **Output encoding.** Deck names are worldwide user text and 2.6% of them are
  outside cp1252 — `【VƐÐĤ】 KRARK ROGRAKH`, `Josh and Ti’s Bizarre Adventure`.
  A redirected stdout on Windows uses the ANSI codepage, so `moxfield.py search
  > out.txt` would die on one Portuguese deck name. `main()` reconfigures stdout
  and stderr to UTF-8 to prevent it.

## Building an exe

`moxfield.spec` is a PyInstaller spec. **PyInstaller cannot cross-compile**, so a
Windows `.exe` has to be built on Windows — either on a Windows machine:

```
pip install textual pyinstaller
pyinstaller --clean --noconfirm moxfield.spec
```

or, better, by CI: `.github/workflows/windows.yml` builds on `windows-latest`,
runs the tests, smoke-tests the exe and uploads it as an artifact. Push a `v*`
tag or trigger it by hand.

The spec collects Textual explicitly — it ships `.tcss` stylesheets and resolves
widgets lazily, so a plain dependency scan misses both — and names `tui`/`builder`
as hidden imports, since they are imported inside a function to keep the TUI
optional. Verified by building and running: CLI, TUI and all. About 16 MB.

### Which layout to send

`ONEFILE` at the top of the spec picks between two:

| | one file | one folder |
|---|---|---|
| what you send | a single `moxfield.exe` | a zipped folder |
| startup | unpacks to temp each run | immediate |
| antivirus | frequently false-flagged | much less so |

A single exe is the nicer thing to hand someone, so it is the default — but if
the recipient's antivirus eats it, switch `ONEFILE = False` and send the zip.
Either way expect a **SmartScreen** warning on an unsigned binary ("More info" →
"Run anyway"); the only real fix is a code-signing certificate.

Tell them to open Windows Terminal and run `moxfield.exe tui`.

### Where the data goes

A frozen exe is launched from wherever Explorer happens to be, so it does not
put the database in the working directory the way the script does. It uses
`%LOCALAPPDATA%\moxfield\moxfield.sqlite` instead. `--db` overrides either.

## Limits

- Rate limited. `MOXFIELD_RATE` (default 0.4) tunes it; 429s are retried with
  backoff. 0.4 req/s was measured to run with **zero** 429s while still
  achieving ~91% of the throughput of asking for 1.4 — a 429 costs
  `Retry-After: 20`, so overshooting is slower, not faster. Latency is 0.14s a
  request, so the limit is pacing, not the network: parallelism buys nothing.
- Moxfield's ToS prohibits scraping and they ask automated clients to request a
  dedicated `User-Agent` from `support@moxfield.com`. Set `MOXFIELD_UA` once you
  have one, and leave the rate low. Large crawls are the point at which that
  request stops being optional.
