# Moxfield deck search API — reverse-engineered notes

Everything below was verified empirically against `api2.moxfield.com` in July 2026,
by reading the frontend bundle (`assets.moxfield.net/assets/assets/moxfield.*.js`)
and probing endpoints.

## Access

- `api2.moxfield.com` returns **403** to the default `curl`/`urllib` agent. Any
  browser-like `User-Agent` gets through. Cloudflare fronts it, but there is no JS
  challenge on the API host.
- Moxfield's ToS prohibits scraping; they ask automated clients to email
  `support@moxfield.com` for an assigned `User-Agent`. That's the sanctioned path
  and the only one that won't get an IP or agent blocked.
- **Rate limit:** re-measured 2026-08-18 and **much tighter than it was in
  July**. `Retry-After` is now **20 seconds**, not 5, so a 429 undoes what 28
  well-paced requests would have achieved. 40 requests per leg:

  | asked | achieved | 429s |
  |---|---|---|
  | 1.4 req/s | 0.45 req/s | 3 |
  | 0.7 req/s | 0.34 req/s | 3 |
  | **0.4 req/s** | **0.41 req/s** | **0** |

  So 0.4 req/s runs completely clean and still delivers ~91% of what hammering
  at 1.4 delivers. It does not behave like a simple token bucket: halving the
  ask from 1.4 to 0.7 did not reduce the 429 count at all, which points at a
  rolling quota rather than an instantaneous rate. No bans observed.
- **Latency is not the constraint.** A request costs 0.14s on a fresh
  connection and 0.04s on a kept-alive one, so throughput is set entirely by
  pacing and backoff. Concurrency buys nothing here.

## Deck search

`GET /v2/decks/search` — the full parameter list, taken from the bundle's request
builder (`GET_PUBLIC_DECKS`):

```
pageNumber, pageSize, sortType, sortDirection, fmt, filter, hubName,
deckName, authorUserNames, cardId, board, commanderCardId, minBracket,
maxBracket, partnerCardId, commanderSignatureSpellCardId,
partnerSignatureSpellCardId, companionCardId
```

Sibling endpoints: `/v2/decks/search-sfw` (same, SFW-filtered), `/v2/decks/search-alt`
(returns empty without auth), `/v2/decks/mod-search`, `/v2/decks/personal/search`.

### Hard limits

| Limit | Value |
|---|---|
| `pageSize` max | **100** (larger values silently clamp) |
| Result window | **10,000** exactly (100 pages × 100) |
| Page 101+ | returns `{data: [], totalResults: 0}` — not an error |

`totalResults` saturates at `10000`, so you cannot read a true count above that,
and **there is no way to measure the size of the whole corpus** — every aggregate
view caps at 10k.

### Search rows are a complete metadata record

A row is not a stub. Every field of a deck except its card list is on it:

```
publicId name format bracket autoBracket createdAtUtc lastUpdatedAtUtc
viewCount likeCount commentCount bookmarkCount colorIdentity colors hubNames
mainboardCount sideboardCount maybeboardCount mainCardId createdByUser authors
isLegal visibility hasPrimer
```

That is the single most useful fact for cost. A row arrives **100 to a request**;
a deck body is **one request each**. So every filter except card containment and
copy counts can be answered from search paging at 1% of the cost of fetching
decks, and a complete row-level index of the whole corpus is a two-day crawl
where a complete body-level one is five months.

### `cardId` — the one real card filter

This is the crux of the "only one card" limitation: the request builder accepts a
**single** `cardId`. Repeated or comma-joined values are ignored (and silently fall
back to unfiltered).

Two traps:

1. `cardId` wants the **printing id** (`card.id` from `/v3/cards/named`), *not*
   `uniqueCardId`. The two are separate 5-char id spaces with the same shape, so
   passing a `uniqueCardId` silently matches **some unrelated card** — it returns a
   plausible-looking count and completely wrong decks. This is the single easiest
   way to get quietly bogus results.
2. An invalid `cardId` is **silently ignored** rather than erroring, so a typo
   returns the unfiltered 10k.

Good news: `cardId` matches at **card level, not printing level**. All 10 printings
of Kroxa returned identical totals (legacy=324, modern=5959), so one query per card
suffices — no need to fan out over printings.

`board=mainboard` narrows containment to one board (Kroxa: 3677 → 2999 all-boards
vs mainboard).

### `filter` is *not* a card filter

`filter` is the free-text box: it matches deck **name/description only**. Combining
`cardId=Thrasios&filter=Rhystic Study` returns 64 decks — decks whose *title*
mentions Rhystic Study, not decks containing it. Easy to mistake for a second card
constraint.

### Commander-zone filters are extra card constraints

`commanderCardId`, `partnerCardId`, `companionCardId`,
`commanderSignatureSpellCardId`, `partnerSignatureSpellCardId` each take a printing
id. Combined with `cardId` this gives you **two or more server-side card
constraints** when one of them is in the command zone:

```
cardId=Dockside & commanderCardId=Thrasios  ->  4087   (vs 10000 capped for either alone)
```

### Sorting — use `created`, not the default

`sortType`: `updated` (default), `created`, `views`, `likes` — and `bracket`,
which the frontend enum lists but the search API **answers 400 for
unconditionally**. Verified against six parameter combinations (bare, with
`commanderCardId`, with `fmt`, with `minBracket`, with `maxBracket`, with both):
every one is rejected. It is the same trap as `fmt=tlr`, and it is worse in
practice, because a crawl only reaches the extra orderings deep into a saturated
cell — one killed an hour-long run at the very end.

**The default `updated` sort is unstable** — decks are re-saved constantly, so
ordering shifts underneath you mid-crawl and deep pagination silently drops and
duplicates rows. Observed directly: repeated identical unsorted queries returned
different first results. Always crawl with `sortType=created`.

`sortDirection=ascending|descending` are **disjoint** (0 overlap on first pages),
so flipping direction doubles a partition's reachable set from 10k to **20k**.

### `authorUserNames` takes batches — the universal partition axis

`authorUserNames` accepts a **comma-separated list** and returns the union,
exactly. Measured on 87 users harvested from one page:

```
first 44 users   ->   982
last  43 users   ->  1081
all   87 users   ->  2063   (= 982 + 1081, exact)
all 87 + cardId=Sol Ring  ->  1464
```

This is the axis that finishes the job, and it is strictly better than the
commander sweep on every count:

| | `commanderCardId` | `authorUserNames` |
|---|---|---|
| partition or cover | cover (partner pairs double up) | partition (one creator) |
| formats | command-zone only | all |
| batching | ignored, 1 request per value | union, ~120 values per request |
| enumerable | 3,609 known commanders | authors are on every row |

A single user is always far below the 10k window, so a user's deck list is
*exact* — which is what turns the cover into a proof.

The binding constraint on batch size is **the result window, not the URL**. A
5,090-character URL carrying 500 names was accepted without complaint; but 250
active users already saturate:

| batch | decks |
|---|---|
| 120 users | 5,515 |
| 250 users | 10,000+ |
| 500 users | 10,000+ |

So batches want to be sized by expected deck count (~120 names is a safe
default), with any batch that fills the window split in half and retried.

### The global new-deck stream

Unfiltered `sortType=created&sortDirection=descending` is a firehose of every
public deck, and the 10,000-row window is **about 18 hours deep**:

```
page 1   newest   2026-08-18T01:04:28Z
page 100 10000th  2026-08-17T07:04:08Z
```

So Moxfield gains ~13,300 public decks a day, and polling that one query more
often than every 18 hours sees **every deck ever created, from now on** — about
130 requests a day. Stopping at the first page of entirely-known decks is the
overlap proof that the stream never ran away.

The index is **eventually consistent across replicas**: page 1 can go backwards
between consecutive calls (observed newest jumping 01:30 -> 01:35 -> 01:30
within three minutes), and it refreshes in bursts rather than continuously. A
tail must therefore treat a run of already-known pages, not a single one, as
proof it is at the front.

Extrapolated back over 2018-04-27 → present, the corpus is on the order of
15–25M decks. Its true size stays unmeasurable (every count saturates at 10k),
but that is the scale to plan for.

### Deck ids are random

`publicId` is a 22-character base64url GUID — 128 bits, no ordering to walk. Most
are v4. Since **2026-06-04** a minority are **v7**, whose embedded millisecond
timestamp matches `createdAtUtc` exactly; both kinds are still being issued. There
is no id-range parameter, so this is a curiosity rather than a lever.

### No user-discovery endpoints

`/v2/users/{name}`, `/v2/users/{name}/followers` and `/following` all 404;
`/v2/users/search` 400s. `/v2/decks/{user}` answers **405**, so the path exists
under some other method. Users are therefore discovered from deck rows, which is
sufficient: if the row index is complete, the set of authors in it *is* the set
of users with public decks, by construction.

### Bounds need no range parameter

There is no date or numeric range filter, but `sortType` gives a sorted prefix of
a region, so a lower bound is a complete generator on its own: page
`sortType=likes&sortDirection=descending` and stop at the first row at or below
the bound. The same works for `views` and `created`. This makes `likes>1000` and
`created>2025-01-01` searchable without any card term. What it cannot do is a
window in the *middle* of history, since you still cannot seek past 10k rows.

### There is no multi-card parameter, and no cursor

Fuzzed 36 candidate parameter names against the public endpoint (`cardIds`,
`cardId2`, `cardNames`, `allCardIds`, `requiredCardIds`, `excludeCardId`,
`searchAfter`, `search_after`, `cursor`, `scrollId`, `scroll`, `offset`, `skip`,
`from`, `createdAfter`, `dateFrom`, `colors`, `colorIdentity`, …). **Only `board`
was recognised** — every other name left the result count untouched, i.e. silently
ignored.

So there is no hidden multi-card search, **no cursor or `search_after` to escape
the 10k window**, and no date-range filter. Subdividing is the only way past the
cap.

The response schema carries `matchTypes` and `matchedCards` on every deck, always
empty. Neither string appears anywhere in the frontend bundle, so they belong to a
server-side search mode the public UI never invokes — presumably the one behind
`/v2/decks/search-alt`, which returns **401 with `WWW-Authenticate: Bearer`** and is
gated in the bundle behind a user permission flag.

### Card names vs ids

**Use `/v2/cards/lookup?name=X` to resolve names.** It is exact,
case-insensitive, face-aware (`Stomp` → `Bonecrusher Giant // Stomp`), returns the
real double-faced printing rather than a self-duplicated art variant, and 404s on
anything that isn't a card. It cannot silently guess: `Sol Rin` and `Bonecrusher`
both 404.

`/v3/cards/named?q=` is **fuzzy and unstable, and must never be used to pick a
card** — only to build "did you mean" suggestions. `q=Fire` returns
`Curse of the Fire Penguin` and a pile of Firecats; `Fire // Ice` was outside the
top 25 on one run and inside it on another, so ranking shifts between calls.
Taking the first result is how you end up silently searching for the wrong card.

Names must be matched against faces, not just the printed name. Decks store split
and double-faced cards under the combined name (`"Bonecrusher Giant // Stomp"`), so
a user typing either face matches nothing on a naive string compare. In a 74-deck
sample there were 213 distinct `//` names.

Moxfield also carries **self-duplicated printings**: alongside the real
`Delver of Secrets // Insectile Aberration` there is a
`Delver of Secrets // Delver of Secrets`, and plain `Sol Ring` coexists with
`Sol Ring // Sol Ring`. Resolution therefore needs to prefer an exact full-name
match, then the printing with genuinely distinct faces, before declaring ambiguity.

### Beating the 10k cap: partitioning

The complete `fmt` enum (from the bundle) is:

```
alchemy alpha40 archon brawl brawlPrecons centurion commander commanderPrecons
competitiveBrawl conquest dandan duelCommander duelCommanderRussian frontier
gladiator highlanderAustralian highlanderCanadian highlanderEuropean
highlanderGauntlet historic historicBrawl legacy leviathan modern none
oathbreaker oldSchool pauper pauperEdh pendragon pennyDreadful pioneer precons
predh premodern primordial secretLair standard timeless tinyLeaders tlr
valueVintage vintage            (plus: package, wishList)
```

Two traps here. **`none` is a real and large bucket** — 29% of one sampled card's
decks had no format set, so omitting it loses a big fraction. And `package` /
`wishList` are *not decks*: including them makes a partition overcount (measured
1303 against a true 1236, over by exactly the 67 packages). Excluding those two and
including `none`, format sums **exactly**: 1236 = 1236.

Verified partition axes:

1. **`fmt`** — complete, given the caveats above.
2. **`minBracket`/`maxBracket` set equal** — complete *within Commander*
   (836 = 30+286+283+228+9, exact). Outside Commander it returns nothing at all, so
   it must not be applied blindly.
3. **`sortDirection` flip** — free ×2 on any leaf, since asc/desc are disjoint.
4. `commanderCardId` — a near-perfect partition for Commander, but requires a
   commander list to enumerate.
5. `authorUserNames` — per-user counts are essentially always < 10k.

`fmt=tlr` is in the frontend's format enum but the search API answers **HTTP 400**
for it. Iterating the enum naively crashes a crawl part-way through.

**Everything about `totalResults` saturating at 10000 is a trap**, and it bit in
four separate places during testing. `10000` never means "ten thousand", it means
"at least ten thousand":

| Check | Wrong | Right |
|---|---|---|
| Validating a split | children sum `==` parent | `==` below the cap, `>=` at it |
| Deciding to subdivide | `total <= 10000` → drain | `total < 10000` → drain |
| Flipping sort direction | `total > 10000` | `total >= 10000` |
| Reporting incompleteness | `total > 20000` | unobservable — see below |

The second one is the costly one: `<=` means the subdivision never fires *at all*,
because the condition is true exactly when subdivision is needed. Measured on
Lightning Bolt, that is the difference between enumerating 9,994 decks and 138,675.

**Detecting an incomplete result needs the overlap test.** Descending walks the
newest 10k and ascending the oldest 10k, so the two halves meet in the middle iff
the corpus fits in 20k. Checking whether they **intersect** is exact. The obvious
alternatives both fail: a unique-count threshold under-fires, because a full drain
lands slightly under 20,000 (duplicate ids, decks deleted mid-crawl — measured
19,996 and 19,999 on real runs), and a pages-consumed test over-fires, because
both windows fill at any size past 10k including sizes the halves still fully
cover. At exactly 20,000 the halves tile with no overlap, which is genuinely
indistinguishable from 20,001 through this API — so the honest report there is
"unverified", not "truncated".

### The third axis: commander

Superseded by `authorUserNames` batching above, which partitions every format
rather than only the command-zone ones and costs a fraction as much. Kept here
because the measurements are real and the cover-validation argument still
applies to any axis of this shape.

`commanderCardId` is what actually finishes the job for Commander, which is the
bulk of Moxfield. Every Commander deck has a command zone, so sweeping the
commander list covers all of them.

It is a **cover, not a partition** — a partner pair puts its deck under both
commanders — so it must be validated on the size of the deduplicated union, never
on a sum of counts.

The full list is 3,609 commanders (21 requests) from Moxfield's own command-zone
query. Unlike `authorUserNames`, `commanderCardId` does **not** accept
comma-separated batches: multiple ids are silently ignored and fall back to
unfiltered, so it costs one request per commander.

**Colour-identity pruning is available but off by default.** Skipping commanders
that could not legally host the card cuts the sweep sharply — 3,609 → 1,208 for a
mono-coloured card (3x), 295 for two-colour (12x), 68 for five-colour (53x),
nothing for colourless — but the premise does not hold:

1. **Rulebreaker** (Mystery Booster: Commander Edition, announced August 2026)
   puts a named exception to the colour identity rule on eight commanders —
   off-colour legendary permanents, instants and sorceries, big creatures, or all
   artifacts and enchantments. For a mono-blue card, **6 of those 8 would be
   wrongly pruned**. They are findable via `keyword:rulebreaker` on
   `/v2/cards/search` and are flagged `exempt` in `commanders.json` so they are
   never pruned. Expect the list to grow; rebuild it when new sets land.
2. Moxfield hosts **illegal decks** regardless of any rule.
3. Most importantly, the shortfall check that would catch a bad prune only has
   something to compare against when the parent count is **exact** — and the
   commander axis only ever engages when the parent is **saturated**. The safety
   net is therefore vacuous in exactly the case that uses it.

So pruning is an unverifiable assumption where it matters, and has to be asked
for explicitly with `--prune-colors`.

### Every sort key is a separate 10k window

This is the single most useful thing in this file. `sortType` is not a different
view of one window — each key is an **independent** 10k window onto the same
cell. Measured on one cell (Rhystic Study, Commander, bracket 3, `totalResults`
reading 10000):

| Windows paged | Distinct decks |
|---|---|
| `created` desc+asc | 19,999 |
| + `views`, `likes` | **45,057** |

(`updated` is a fourth usable ordering; `bracket` is not, see above.)

So `created` ascending+descending is worth 20k, but five keys × two directions is
worth far more. Draining the extra orderings is the cheapest coverage available
and needs no commander list at all — which matters most for the formats that
have no command zone to partition on.

### `mainCardId` is only *a* commander

Search rows carry `mainCardId`, and for a command-zone format it names the
deck's commander — but for a **partner** pair it names only one of the two, and
not necessarily the one you queried.

Measured on `commanderCardId=Rograkh, fmt=commander, bracket=4`, a cell the
crawler enumerated at 17,858 decks: only **9,643** of them carry Rograkh as
`mainCardId`. The other 8,215 are the same decks under their partner's id. So a
commander harvest read off `mainCardId` misses ~46% of a partner commander's
decks, and the miss is invisible — the rows are there, just filed under a
different name.

This is a second reason the commander axis gave way to `authorUserNames`:
`createdByUser` on a row is unambiguous and complete, where `mainCardId` is
neither for the commanders most likely to need subdividing.

#### The original measurement

Search rows carry `mainCardId`, and for a command-zone format it is *a* deck
commander (verified 11/12 against actual command zones; the twelfth was a
Commander deck with an *empty* command zone, which no `commanderCardId` query can
ever reach — a hard floor on that axis). The partner caveat above was not caught
at the time.

That makes the commander sweep far cheaper: page the windows once, read the
commanders off the rows, and ask only about the ones that actually occur. On the
cell above that was **1,204 commanders instead of 3,609**. The harvest depth must
follow the cell size — a fixed 10 pages saw a third of a 3,094-deck cell, missed
every commander living in the rest, and produced a 90.7% cover that the sampler
correctly rejected.

### The ceiling is not where it looked

An earlier version of this file claimed a card in 40k+ Commander decks could not
be enumerated. **That is wrong.** With multi-key windows plus a harvested
commander sweep, one saturated cell yielded:

| | decks |
|---|---|
| `created` asc+desc alone | 19,999 |
| multi-key window scan | 45,057 |
| + commander sweep (1,204 harvested) | **227,767** |

227,767 decks out of a query whose `totalResults` reads 10000 — 11.4x the "hard
20k ceiling" — verified at 99.9% by direct sampling, in 4,679 requests. The
ceiling was an artefact of only ever paging `created`.

### Why format and bracket alone are not enough

Measured Commander bracket distributions for common staples:

| Card | b1 | b2 | b3 | b4 | b5 |
|---|---|---|---|---|---|
| Rhystic Study | 32 | 3,091 | 10,000+ | 10,000+ | 10,000+ |
| Force of Will | 10 | 566 | 10,000+ | 10,000+ | 10,000+ |
| Mystic Remora | 469 | 10,000+ | 10,000+ | 10,000+ | 10,000+ |
| Esper Sentinel | 500 | 10,000+ | 10,000+ | 10,000+ | 10,000+ |

Every staple saturates at bracket level, and asc+desc caps a saturated leaf at
20k. So a card played in 40k+ Commander decks **cannot be fully enumerated** with
`fmt` × `bracket` × `sortDirection` alone. A full crawl of Rhystic Study in
Commander reached 63,112 decks (99.98% of the 63,123 those axes can address) and
correctly reported three of the five brackets as unverified.

Going further needs a third axis — `commanderCardId` is the natural one, since it
partitions Commander cleanly, but it requires enumerating commanders (Moxfield's
own list comes from `/v2/cards/search` with the Scryfall query
`(t:Legendary and (t:Creature or o:"can be your commander") and -t:Battle)`).

There is **no date-range parameter**, which is the notable gap — otherwise time
windows would be the clean way to slice everything.

## Deck fetch

`GET /v3/decks/all/{publicId}` — one request per deck, no bulk endpoint exists.

- ~66 KB gzipped on the wire, ~550 KB decoded.
- `boards` is a dict of board name → `cards` dict **keyed by `uniqueCardId`**, each
  value `{card: {...}, quantity: ...}`.
- Boards present: `mainboard, sideboard, maybeboard, commanders, companions,
  signatureSpells, attractions, stickers, contraptions, planes, schemes, tokens`.
- Keeping only names + metadata slims a deck to **~2.7 KB** — a 200× reduction, and
  the difference between a feasible and infeasible local corpus.
- Deleted/private decks return 403/404; handle rather than abort.

`/v3/decks/all/alt/{id}` requires auth (401).

## Cost of a full rip

At 1.4 req/s with one request per deck:

Bodies, at one request each:

| Decks | Wall time | Transfer | Stored (slim) |
|---|---|---|---|
| 10k | 7 hours | 0.7 GB | 27 MB |
| 100k | 2.9 days | 6.5 GB | 270 MB |
| 1M | 29 days | 65 GB | 2.7 GB |
| 10M | 289 days | 650 GB | 27 GB |

Rows, at 100 per request, are two orders of magnitude cheaper — and carry every
field except the card list:

| Decks | Requests | Wall time |
|---|---|---|
| 1M | 10k | 7 hours |
| 10M | 100k | 2.9 days |
| 20M | 200k | 5.8 days |

Which is why the corpus is built out of rows and bodies are fetched on demand.
(All times at the measured 0.4 req/s. They were 3.5x rosier in this file when it
assumed 1.4.)

The corpus spans 2018-04-27 → present. Its true size is unmeasurable through the
API (see the 10k cap), but Moxfield is the dominant MTG deckbuilder, so the
realistic scale is millions to tens of millions of public decks — i.e. a full
single-threaded rip is a **months-long** job, and storage is *not* the binding
constraint; request throughput is.

## Card lookup

- `GET /v2/cards/lookup?name={name}` — **exact** lookup, the one to resolve names
  with. Case-insensitive and face-aware; 404s rather than guessing.
- `GET /v3/cards/named?q={name}&count={n}` — fuzzy name search. Returns `id`
  (printing, use this for `cardId`), `uniqueCardId`, `scryfall_id`, and full oracle
  data. Ranking is unreliable — suggestions only.
- `GET /v3/cards/editions/{uniqueCardId}` — all printings; entries keyed `cardId`.
- `GET /v2/cards/search?q={scryfall syntax}` — Scryfall-syntax card search.
- `GET /v2/cards/lookup?name=&set=&cn=` — exact lookup.

For bulk card metadata, use Scryfall's bulk data instead — it's free, sanctioned,
and maps to Moxfield via `scryfall_id`.
