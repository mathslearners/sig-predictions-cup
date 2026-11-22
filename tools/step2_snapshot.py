"""Step 2: snapshot every Midterm Elections market + orderbook to a timestamped CSV and print a summary.

Output: data/snapshots/markets_<UTC>.csv  and  data/raw/orderbooks_<UTC>.json
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from sigbot.client import ROOT, SigClient
from sigbot.markets import book_stats, fetch_markets, parse_title

SUM_TOLERANCE = 0.10  # flag races whose party mids sum further than this from 1

FIELDS = ["snapshot_utc", "market_id", "title", "race", "race_type", "party", "exchange_id", "status",
          "best_bid", "bid_size", "best_ask", "ask_size", "mid", "spread",
          "bid_levels", "ask_levels", "bid_depth", "ask_depth", "latest_price", "initial_price"]


def main():
    c = SigClient()
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")

    tourn, markets = fetch_markets(c)
    print(f"Tournament: {tourn.name} ({tourn.slug})  id={tourn.id}")
    print(f"Fetching {sum(len(m['exchanges']) for m in markets)} orderbooks...")

    rows, raw_books = [], {}
    for i, m in enumerate(markets, 1):
        race, race_type, party = parse_title(m["title"])
        for ex in m["exchanges"]:
            book = c.get(f"/exchanges/{ex['id']}/orderbook", tournamentId=tourn.id, depth=200)
            raw_books[ex["id"]] = book
            rows.append({
                "snapshot_utc": now.isoformat(timespec="seconds"),
                "market_id": m["id"], "title": m["title"],
                "race": race, "race_type": race_type, "party": party,
                "exchange_id": ex["id"], "status": m["status"],
                **book_stats(book),
                "latest_price": ex.get("latestPrice"), "initial_price": ex.get("initialPrice"),
            })
        if i % 50 == 0:
            print(f"  {i}/{len(markets)}")

    snap_dir, raw_dir = ROOT / "data" / "snapshots", ROOT / "data" / "raw"
    snap_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = snap_dir / f"markets_{ts}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["race_type"] or "", r["race"] or "", r["party"] or "")))
    (raw_dir / f"orderbooks_{ts}.json").write_text(json.dumps(raw_books))

    summarize(rows)
    print(f"\nSaved {out.relative_to(ROOT)}")


def summarize(rows):
    two = [r for r in rows if r["spread"] is not None]
    spreads = [r["spread"] for r in two]
    tob_bid = sum(r["bid_size"] or 0 for r in rows)
    tob_ask = sum(r["ask_size"] or 0 for r in rows)
    print("\n================ SUMMARY ================")
    print(f"Markets:                 {len(rows)}")
    print(f"  two-sided books:       {len(two)}")
    print(f"  bid only / ask only:   {sum(1 for r in rows if r['best_bid'] is not None and r['best_ask'] is None)}"
          f" / {sum(1 for r in rows if r['best_ask'] is not None and r['best_bid'] is None)}")
    print(f"  empty:                 {sum(1 for r in rows if r['best_bid'] is None and r['best_ask'] is None)}")
    if spreads:
        print(f"Median spread:           {statistics.median(spreads):.3f}"
              f"   (min {min(spreads):.3f}, max {max(spreads):.3f})")
    print(f"Top-of-book depth:       {tob_bid + tob_ask:,.0f} shares  (bids {tob_bid:,.0f} / asks {tob_ask:,.0f})")
    print(f"Full-book depth:         {sum(r['bid_depth'] + r['ask_depth'] for r in rows):,.0f} shares")

    races = defaultdict(list)
    for r in rows:
        races[r["race"]].append(r)
    flagged, incomplete = [], []
    for race, rs in races.items():
        mids = [r["mid"] for r in rs]
        if any(x is None for x in mids):
            incomplete.append(race)
            continue
        s = sum(mids)
        if abs(s - 1) > SUM_TOLERANCE:
            flagged.append((s, race, rs))
    print(f"\nRaces: {len(races)}  (complete two-sided on all parties: {len(races) - len(incomplete)})")
    print(f"Races with party mids summing > {SUM_TOLERANCE:.2f} from 1:")
    if not flagged:
        print("  none")
    for s, race, rs in sorted(flagged, key=lambda x: -abs(x[0] - 1)):
        parts = "  ".join(f"{r['party']} {r['best_bid']:.3f}/{r['best_ask']:.3f} (mid {r['mid']:.3f})"
                          for r in sorted(rs, key=lambda r: r["party"]))
        print(f"  {race:<24} sum={s:.3f}   {parts}")
    if incomplete:
        print(f"Races skipped (a party has no two-sided book): {len(incomplete)}")


if __name__ == "__main__":
    main()
