"""Live self-test of every order path the election bot uses, with 1-share orders. Costs about one spread (~0.01).

  python tools/election_selftest.py            # dry: prints what it would send
  python tools/election_selftest.py --live     # sends: run inside a maker run, never at :29-:31 of an even UTC hour

Uses election.py's SigVenue exactly as the night does (batch writes with expiry, index-mapped results, cancel):
  a  take 1 YES at the best ask                     (C/L/H path)        -> filled 1
  b  take 1 NO at 1 - best bid on the same exchange (netting)           -> filled 1, pair paid back at once
  c  rest 1 YES far under the market                (X path)            -> listed in GET /orders?status=open
  d  cancel it                                                           -> gone from the open orders
  e  rest 1 YES with a 60 s expiry                                       -> gone after ~75 s
  f  take a pair: 1 YES on each party of the race   (P path)            -> both filled
The live maker may cancel c/e itself in its next cycle (it cancels orders it doesn't want), which still shows the
order rested and went away, so c/e pass when the order was placed and is gone by the check.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import json
import time
from types import SimpleNamespace

from election import SigVenue
from sigbot.client import ROOT

UNIVERSE = ROOT / "config" / "universe.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--d", help="D exchange of the test race (default: a tight two-party race we have no orders or "
                                "position on: a buy on an exchange where our resting buys commit the cash is a no-op)")
    ap.add_argument("--r", help="R exchange of the test race")
    a = ap.parse_args()
    u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    log = []
    args = SimpleNamespace(live=a.live, paper=False, feed=False, writers=1, rest_tops_every=0, fallback_depth=1)
    v = SigVenue(args, u["tournamentId"], [], log.append)
    v.trading = True
    if not a.d:
        from sigbot.markets import parse_title
        by_race = {}
        for m in u["markets"]:
            race, rt, party = parse_title(m["title"])
            by_race.setdefault(race, {})[party] = str(m["exchanges"][0]["id"])
        cash, pos = v.account()
        committed = {}
        for o in v.open_orders():
            if o.get("action", "buy") == "buy":
                ex = str(o.get("exchangeId"))
                committed[ex] = committed.get(ex, 0.0) + float(o.get("remainingQuantity") or o.get("quantity") or 0) * \
                    float(o.get("price") or 0)
        busy = {ex for ex, c in committed.items() if c > 0.3 * cash}
        pairs = [(e["D"], e["R"]) for race, e in by_race.items() if set(e) == {"D", "R"}
                 and not ({e["D"], e["R"]} & (busy | set(pos)))]
        v.ids = [x for p in pairs for x in p]
        tops = v.books()
        tight = [(tops[d]["asks"][0][0] - tops[d]["bids"][0][0], d, r) for d, r in pairs
                 if all(tops.get(x, {}).get("bids") and tops.get(x, {}).get("asks") for x in (d, r))]
        _, a.d, a.r = min(tight)
        print(f"test race: {a.d}/{a.r} (no orders or positions of ours; {len(pairs)} such races)")
    v.ids = [a.d, a.r]
    tops = v.books()
    (bd, ad), (br, ar) = [((tops[x]["bids"] or [(None,)])[0][0], (tops[x]["asks"] or [(None,)])[0][0]) for x in (a.d, a.r)]
    print(f"book {a.d}: {bd}/{ad}   {a.r}: {br}/{ar}")
    cash0, _ = v.account() if a.live else (None, None)
    plan = [("a take YES", [{"ex": a.d, "side": "yes", "price": ad, "qty": 1, "role": "T"}], "take"),
            ("b take NO (nets a)", [{"ex": a.d, "side": "no", "price": round(1 - bd, 3), "qty": 1, "role": "T"}], "take"),
            ("f pair YES+YES", [{"ex": a.d, "side": "yes", "price": ad, "qty": 1, "role": "P"},
                                {"ex": a.r, "side": "yes", "price": ar, "qty": 1, "role": "P"}], "take")]
    ok = {}
    if not a.live:
        for name, orders, _ in plan:
            print("would send", name, orders)
        print("would rest 1 YES at 0.05 on", a.d, "then cancel it, and rest one with a 60 s expiry")
        return
    for name, orders, _ in plan:
        t0 = time.monotonic()
        res = v.take(orders, 60)
        ms = int((time.monotonic() - t0) * 1000)
        filled = [r["filled"] for r in res]
        ok[name] = len(res) == len(orders) and all(r["ok"] for r in res) and all(f >= 1 for f in filled)
        print(f"{name:<20} {'PASS' if ok[name] else 'FAIL'}  filled {filled}  {ms} ms  "
              f"{'no-op (cash committed by resting buys) ' if any(r.get('noop') for r in res) else ''}"
              f"{[r.get('error') for r in res if r.get('error')]}")
    # c + d: rest far away, see it, cancel it
    ids = v.rest([{"ex": a.d, "side": "yes", "price": 0.05, "qty": 1, "role": "X"}], 300)
    listed = bool(ids) and any(o["id"] in ids for o in v.open_orders())
    print(f"{'c rest + listed':<20} {'PASS' if listed else 'FAIL'}  ids {ids}")
    n = v.cancel(ids)
    time.sleep(3)
    gone = bool(ids) and not any(o["id"] in ids for o in v.open_orders())
    print(f"{'d cancel':<20} {'PASS' if gone else 'FAIL'}  cancelled {n}")
    ok["c"], ok["d"] = listed, gone
    # e: expiry
    ids = v.rest([{"ex": a.d, "side": "yes", "price": 0.05, "qty": 1, "role": "X"}], 60)
    print(f"{'e rest 60 s':<20} placed {ids}; waiting 75 s for the expiry")
    time.sleep(75)
    gone = bool(ids) and not any(o["id"] in ids for o in v.open_orders())
    print(f"{'e expired':<20} {'PASS' if gone else 'FAIL'}")
    ok["e"] = gone
    cash1, pos = v.account()
    print(f"cash {cash0:,.3f} -> {cash1:,.3f} ({cash1 - cash0:+.3f}); "
          f"{'ALL PASS' if all(ok.values()) else 'FAILED: ' + ', '.join(k for k, x in ok.items() if not x)}")
    for e in log:
        print("  log:", e)


if __name__ == "__main__":
    main()
