#!/usr/bin/env python3
"""Moxfield deck search: a query language over the public API, plus a local
corpus that makes the answers complete and repeat queries free.

  moxfield.py search 'cmdr:"Rograkh, Son of Rohgahh" card:"Cloudstone Curio"'
  moxfield.py explain 'f:edh card:Windfall -card:"Mana Crypt"'
  moxfield.py tail          # ingest the global new-deck stream
  moxfield.py sweep         # enumerate known users' decks
  moxfield.py tui           # interactive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import engine as E
import query as Q
from api import Client, QueryError
from store import Store

EXAMPLES = """\
examples:
  moxfield.py search 'card:"Sol Ring" card:"Rhystic Study"'
  moxfield.py search 'cmdr:"Rograkh, Son of Rohgahh" card:"Cloudstone Curio"'
  moxfield.py search 'f:edh (card:Windfall or card:"Wheel of Fortune") -card:"Mana Crypt"'
  moxfield.py search 'f:modern main:"4x Lightning Bolt" likes>10'
  moxfield.py search 'f:edh likes>1000'
"""


def default_db() -> str:
    """Where the corpus lives.

    Beside the working directory normally, which is what a developer wants. But
    a frozen exe is launched from wherever Explorer happens to be, so it gets a
    real per-user data directory instead of scattering databases around.
    """
    if not getattr(sys, "frozen", False):
        return "moxfield.sqlite"
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "share"))
    d = os.path.join(base, "moxfield")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "moxfield.sqlite")


def build(args, dry=False):
    store = Store(args.db)
    client = Client(store)
    return store, client, E.Engine(client, store, dry=dry)


def _report(plan, out=sys.stderr):
    print(f"\ncandidates: {len(plan.ids or [])}  [{plan.status}]", file=out)
    for n in plan.notes:
        print(f"  {n}", file=out)
    if plan.residual:
        print("checked locally:", file=out)
        for r in plan.residual:
            print("  " + Q.describe(r).strip(), file=out)
        if plan.needs_body:
            print(f"  deck bodies: {len(plan.ids or []):,} requests", file=out)
    if plan.ids is not None:
        print(f"estimated cost: {plan.cost()}", file=out)


def cmd_explain(args):
    _, client, eng = build(args, dry=True)
    ast = Q.resolve(Q.parse(" ".join(args.query)), client)
    print(Q.describe(ast).rstrip(), file=sys.stderr)
    _report(eng.solve(ast))
    return 0


CONFIRM_ABOVE = 2000    # candidates, before asking on an interactive terminal


def cmd_search(args):
    store, client, eng = build(args)
    t0 = time.time()
    ast = Q.resolve(Q.parse(" ".join(args.query)), client)
    print(Q.describe(ast).rstrip(), file=sys.stderr)

    # Price it first. Counts are memoised, so the real plan re-uses these and the
    # estimate is free -- and a query that will subdivide gets to say so before
    # it starts a crawl rather than after.
    eng.dry = True
    est = eng.solve(ast)
    eng.dry = False
    big = est.ids is not None and (len(est.ids) > CONFIRM_ABOVE
                                   or est.status == "subdivides")
    if big and not args.yes and sys.stdin.isatty():
        _report(est)
        if input(f"\nthis needs {est.cost()}. continue? [y/N] ").strip().lower() \
                not in ("y", "yes"):
            return 1

    plan = eng.solve(ast)
    print(f"candidates: {len(plan.ids or [])} [{plan.status}]", file=sys.stderr)
    n = 0
    for row in eng.iter_matches(ast, plan):
        n += 1
        if args.json:
            print(json.dumps(dict(row), default=str))
        else:
            print(f"https://moxfield.com/decks/{row['public_id']}  "
                  f"[{row['format']}] {row['name']}  (by {row['author']})", flush=True)
        if args.limit and n >= args.limit:
            break
    thr = f" ({client.throttled} throttled)" if client.throttled else ""
    print(f"\n{n} matches | {client.requests} requests{thr} | {time.time()-t0:.0f}s "
          f"| completeness: {plan.status}", file=sys.stderr)
    for note in plan.notes:
        print(f"  {note}", file=sys.stderr)
    return 0


def cmd_tail(args):
    _, client, eng = build(args)
    while True:
        r = eng.tail(args.pages)
        print(f"tail: {r['new']} new of {r['seen']} rows"
              f"{'' if r['caught_up'] else '  WARNING: never caught up'}")
        if not args.watch:
            return 0
        time.sleep(args.watch)


def cmd_sweep(args):
    _, client, eng = build(args)
    r = eng.sweep_users(args.limit)
    warn = (f"  ({r['partial_batches']} batches hit the window and were not "
            f"counted exactly)" if r["partial_batches"] else "")
    print(f"swept {r['users']} users, {r['decks']} decks, "
          f"{client.requests} requests{warn}")
    return 0


def cmd_tui(args):
    from tui import run
    run(args.db)
    return 0


def main(argv=None) -> int:
    # Deck names are user-supplied text from all over the world and 2.6% of them
    # are outside cp1252, which is what a redirected stdout gets on Windows.
    # Without this, piping the results to a file dies on a Portuguese deck name.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0], epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=default_db())
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("search", cmd_search), ("explain", cmd_explain)):
        p = sub.add_parser(name)
        p.add_argument("query", nargs="+")
        p.add_argument("--limit", type=int)
        p.add_argument("--json", action="store_true")
        p.add_argument("-y", "--yes", action="store_true",
                       help="skip the confirmation on a large crawl")
        p.set_defaults(fn=fn)

    p = sub.add_parser("tail", help="ingest the global new-deck stream")
    p.add_argument("--pages", type=int, default=100)
    p.add_argument("--watch", type=int, metavar="SECONDS")
    p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("sweep", help="enumerate known users' decks")
    p.add_argument("--limit", type=int, default=1000)
    p.set_defaults(fn=cmd_sweep)

    sub.add_parser("tui").set_defaults(fn=cmd_tui)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except QueryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
