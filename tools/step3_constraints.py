"""Step 3: cross-market constraint violations, ranked by edge after crossing the spread.

Two sources:
  1. Engine: GET /relationships/constraints (raw output saved as-is).
  2. Own check: party markets within one race are ~mutually exclusive & exhaustive, so
       buy-all  edge = 1 - sum(best asks)   (buy YES on every party; one should pay 1)
       sell-all edge = sum(best bids) - 1   (sell YES on every party / buy NO on each)
     Caveats: sell-all loses if two party markets both resolve YES (fusion-ticket rule);
     buy-all loses if an unlisted candidate wins.

Books come from the latest step-2 snapshot unless --fresh is given.
Output: data/constraints/engine_<UTC>.json and data/constraints/ranked_<UTC>.csv
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots
import argparse
import csv
import glob
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

from sigbot.client import ROOT, SigClient
from sigbot.markets import book_stats, fetch_markets

NUM = ["best_bid", "bid_size", "best_ask", "ask_size", "mid", "spread"]
FIELDS = ["source", "kind", "name", "edge_after_spread", "edge_at_mid", "max_size", "profit_at_size",
          "legs", "risk_note", "relationship_id"]


def load_books(fresh: bool) -> tuple[str, dict[str, dict]]:
    if fresh:
        subprocess.run([sys.executable, str(ROOT / "tools/step2_snapshot.py")], check=True)
    path = sorted(glob.glob(str(ROOT / "data" / "snapshots" / "markets_*.csv")))[-1]
    books = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for k in NUM:
                r[k] = float(r[k]) if r[k] != "" else None
            books[r["exchange_id"]] = r
    return path, books


def race_constraints(books: dict[str, dict]) -> list[dict]:
    races = defaultdict(list)
    for r in books.values():
        races[r["race"]].append(r)
    out = []
    for race, rs in races.items():
        rs.sort(key=lambda r: r["party"])
        mids = [r["mid"] for r in rs]
        at_mid = sum(mids) - 1 if None not in mids else None
        asks, bids = [r["best_ask"] for r in rs], [r["best_bid"] for r in rs]
        if None not in asks:
            size = min(r["ask_size"] for r in rs)
            edge = 1 - sum(asks)
            out.append({"source": "own", "kind": "buy_all_parties", "name": race,
                        "edge_after_spread": round(edge, 4),
                        "edge_at_mid": round(-at_mid, 4) if at_mid is not None else None,
                        "max_size": size, "profit_at_size": round(edge * size, 2),
                        "legs": " + ".join(f"BUY {r['party']} YES @{r['best_ask']}" for r in rs),
                        "risk_note": "loses if an unlisted candidate wins"})
        if None not in bids:
            size = min(r["bid_size"] for r in rs)
            edge = sum(bids) - 1
            out.append({"source": "own", "kind": "sell_all_parties", "name": race,
                        "edge_after_spread": round(edge, 4),
                        "edge_at_mid": round(at_mid, 4) if at_mid is not None else None,
                        "max_size": size, "profit_at_size": round(edge * size, 2),
                        "legs": " + ".join(f"SELL {r['party']} YES @{r['best_bid']}" for r in rs),
                        "risk_note": "loses if 2 parties resolve YES (fusion ticket)"})
    return out


def engine_constraints(c: SigClient, tid: str, books: dict[str, dict]) -> tuple[dict, list[dict]]:
    raw = c.get("/relationships/constraints", tournamentId=tid, minViolation=0)
    out = []
    for ev in raw.get("data", []):
        if ev.get("evaluationStatus") != "violated":
            continue
        # Edge after spread ~= engine violation minus the cost of crossing from the engine's reference
        # price to the executable touch on each suggested leg. UNVERIFIED: currentPrice is YES-denominated.
        cross, sizes, legs = 0.0, [], []
        for t in ev.get("suggestedCorrectiveTrades", []):
            b = books.get(t["exchangeId"])
            if b is None:
                b = book_stats(c.get(f"/exchanges/{t['exchangeId']}/orderbook", tournamentId=tid, depth=200))
            buys_yes = (t["action"] == "buy") == (t["outcomeSide"].lower() == "yes")
            px, sz = (b["best_ask"], b["ask_size"]) if buys_yes else (b["best_bid"], b["bid_size"])
            if px is None:
                cross, sizes = None, []
                break
            ref = t.get("currentPrice") if t.get("currentPrice") is not None else b.get("mid") or px
            cross += abs(px - ref)
            sizes.append(sz)
            legs.append(f"{t['action'].upper()} {t['outcomeSide'].upper()} ex{t['exchangeId']} @{px}")
        edge = None if cross is None else ev["violationAmount"] - cross
        size = min(sizes) if sizes else None
        out.append({"source": "engine", "kind": ev["type"],
                    "name": (ev.get("constraint") or {}).get("description") or ev.get("reason"),
                    "edge_after_spread": round(edge, 4) if edge is not None else None,
                    "edge_at_mid": ev["violationAmount"], "max_size": size,
                    "profit_at_size": round(edge * size, 2) if edge is not None and size else None,
                    "legs": " + ".join(legs) or "(no executable legs)",
                    "risk_note": ev.get("reason"), "relationship_id": ev["relationshipId"]})
    return raw, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="re-run step 2 to refresh books first")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    c = SigClient()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap, books = load_books(args.fresh)
    tourn, _ = fetch_markets(c)

    raw, eng = engine_constraints(c, tourn.id, books)
    own = race_constraints(books)

    out_dir = ROOT / "data" / "constraints"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"engine_{ts}.json").write_text(json.dumps(raw, indent=2))
    ranked = sorted(eng + own, key=lambda r: -(r["edge_after_spread"] if r["edge_after_spread"] is not None else -9))
    with (out_dir / f"ranked_{ts}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(ranked)

    print(f"Books from: {snap}")
    print(f"Engine: {len(raw.get('data', []))} evaluations, violationsCount={raw.get('violationsCount')}, "
          f"computedAt={raw.get('computedAt')}")
    print(f"Own race checks: {len(own)} executable baskets over "
          f"{len({r['name'] for r in own})} races")
    pos = [r for r in ranked if (r["edge_after_spread"] or 0) > 0]
    print(f"Positive edge after crossing the spread: {len(pos)}")
    print(f"\nTop {args.top} by edge after spread:")
    print(f"  {'src':<6} {'kind':<17} {'name':<24} {'edge':>7} {'@mid':>7} {'size':>6} {'profit':>8}")
    for r in ranked[:args.top]:
        mid = f"{r['edge_at_mid']:+.3f}" if r["edge_at_mid"] is not None else "   n/a"
        print(f"  {r['source']:<6} {r['kind']:<17} {str(r['name'])[:24]:<24} {r['edge_after_spread']:+.3f} "
              f"{mid:>7} {r['max_size'] or 0:>6.0f} {r['profit_at_size'] or 0:>+8.1f}")
    print(f"\nSaved {(out_dir / f'engine_{ts}.json').relative_to(ROOT)} and "
          f"{(out_dir / f'ranked_{ts}.csv').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
