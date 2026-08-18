# Design

The goal is to iterate, for sure, every deck matching a query. The API makes
that hard in one specific way: every count and every result window saturates at
10,000, there is no cursor, and there is no range filter to slice with. So
completeness has to be *constructed*, and it has to be *auditable*.

## Two tiers, because they cost 100x apart

A search row carries every field of a deck except its card list, and arrives 100
to a request. A deck body carries the cards and costs one request. So:

- **rows** are what we enumerate and iterate — cheap enough that a complete
  row-level index of the whole corpus (15–25M decks) is a ~6 day crawl at the
  measured 0.4 req/s;
- **bodies** are fetched lazily, for the one predicate rows and the server
  cannot settle between them: copy counts.

`store.py` holds both, plus the user list and the ledger.

## The ledger

`cells` records every server-side region we have finished with: its params, how
it was closed (`exact` / `unverified`), the evidence, and when. `cell_decks`
records what was in it.

This makes completeness a durable object rather than a log line. Crawls resume,
overlapping queries pay nothing twice, and an answer's provenance comes from
rows in a table instead of hope.

## The ladder

`engine.py` closes a region by descending this, cheapest first:

1. **count < 10,000** — drain it. Exact.
2. **asc + desc on `created`** — the two halves are disjoint, so a saturated
   region reaches 20k. They meet in the middle exactly when the region fits;
   testing whether they *overlap* is what makes this an exact test rather than a
   threshold.
3. **other sort keys** — each `sortType` is an independent 10k window onto the
   same region, not a different view of one. Measured: 45,057 distinct decks
   across three keys where `created` alone reached 19,999.
4. **`fmt`** — validated by summing children against the parent.
5. **`minBracket`/`maxBracket`** — same, and refused where it does not partition
   (outside Commander it returns nothing).
6. **`authorUserNames` batches** — the terminal axis.

Anything that survives all six is reported `unverified`, with the numbers.

The overlap test is checked *before* the sweep, not after: the window scan has
already fetched what it needs, and it is **exact** where a sweep is only a
sampled cover. A region whose `created` halves meet is proven complete, so the
sweep is skipped entirely — which on one measured cell would have saved ~250
requests that found nothing.

### Why author is the terminal axis

Every deck has exactly one creator, so author is a true partition, in every
format — unlike the command zone, which only exists in some, and which
double-counts partner pairs. `authorUserNames` accepts comma-separated batches
that return the union exactly, so ~120 users cost one request rather than 120.
And a single user is always far below the window, which means a user's deck list
is **exact** — that is what turns a cover into a proof.

The authors come from the region's own windows: paging them is not overhead
(those decks belong in the answer) and every row names its creator, so we only
ask about users who actually occur. The cover is then measured against a random,
two-ordering sample of the parent, because a count comparison cannot work — a
sweep takes hours and the corpus moves underneath it.

A sweep that returns decks we already had is the axis *corroborating* the
windows; only a sweep that returns nothing at all means the axis does not apply
here. Conflating those two is how a working cover gets thrown away.

This reduces "did I get every deck?" to "do I know every user?", which is a far
smaller and slower-moving question, and one the tail crawler answers directly:
if the row index is complete, the authors in it *are* the users with public
decks, by construction.

## Set algebra, not candidate-and-verify

`solve()` turns the AST into operations on id sets: **AND intersects, OR unions,
NOT subtracts**. Server params that compose (`fmt`, `bracket`, author, deck name,
and the command-zone card slots, which stack with `cardId`) are pushed down into
each enumeration first.

The cost model is one line: intersecting costs one request per 100 decks,
verifying by body costs one per deck — with the caveat that a *saturated* cell
costs a subdivision rather than a window, so it must not be priced at the 100
pages its capped count implies. Getting that wrong makes the planner enumerate
10,000+ decks twice to avoid fetching 152 deck bodies. So the planner takes the cheapest term as
a seed, then intersects each further term only while `pages(term) < |candidates|`
and leaves the rest as a local residual. On real queries the intersection wins by
one to two orders of magnitude, and negation stops being unsearchable.

## Sorted prefixes instead of range filters

There is no date or numeric range parameter, but `sortType` gives a *sorted
prefix*, so a lower bound is a complete generator: page `likes` descending and
stop at the first row past the bound. That makes `f:edh likes>1000` a first-class
query rather than one that has to be refused. It cannot reach a window in the
middle of history — you still cannot seek past 10k rows.

## Keeping it current

The unfiltered newest-10,000 window is ~18 hours deep (~13,300 new public decks
a day), so `tail` polling more often than that sees every deck created from now
on, for ~130 requests/day. It stops after two consecutive pages of entirely
known decks — two, because the index is replicated with differing freshness and
page 1 can go backwards between calls.

`sweep` walks the other direction: for every known user we have not enumerated,
fetch their complete deck list. Together they close the loop — the tail
discovers users, the sweep makes each user exact.

## What is deliberately not done

- **No parallelism.** The limit is a server-side quota, and a request costs
  0.14s of network against a 2.5s pacing interval, so a worker pool buys
  nothing. Every real speedup here is structural.
- **No colour-identity pruning** of any commander-shaped axis. Rulebreaker
  commanders are named exceptions to the colour rule, Moxfield hosts illegal
  decks anyway, and the check that would catch a bad prune is only available
  when the parent count is exact — which is exactly when the axis is not needed.
- **No claim of a complete corpus.** The row index is complete going forward and
  measurably incomplete backwards, and every search reports its own status
  rather than the corpus's.
