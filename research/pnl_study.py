"""Realized cash P&L of the bot's own round trips. READ-ONLY: no API calls (uses research/fill_model.py --pull's cache).

  python research/pnl_study.py --pull     # every own fill since the open -> data/tapes/pull/fills_all.json (8 reads/min)
  python research/pnl_study.py            # analyse: fills_all.json + the tapes from research/fill_model.py --pull + Kalshi minute history

SIG nets a buy of the opposite side against a holding, so each exchange's position is one signed Yes quantity. A fill
that grows it is an entry, at cost (Yes price, or 1 - No price per Yes-equivalent); a fill that shrinks it closes part
of it at (1 - the No price paid) for a Yes holding or (1 - the Yes price paid) for a No holding. Realized cash P&L per
closed share = exit price - average entry cost (in the held side's terms). Entries are tagged with their distance
under Kalshi fair and under SIG's own level (VWAP of the exchange's prints in the 15 min before the fill), and each
close with how long the closed shares were held (FIFO).
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import bisect
import json
from collections import defaultdict, deque
from datetime import datetime, timezone

import argparse

from daily_report import kalshi_history
from sigbot.client import ROOT, RateLimiter, SigClient
from sigbot.markets import TOURNAMENT_SLUG

D = ROOT / "data" / "tapes" / "pull"


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def bucket(x, edges, fmt="{:+.2f}"):
    if x is None:
        return "n/a"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= x < hi:
            return f"{fmt.format(lo)}..{fmt.format(hi)}"
    return f"<{fmt.format(edges[0])}" if x < edges[0] else f">={fmt.format(edges[-1])}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--reads", type=int, default=8)
    ap.add_argument("--hours", type=float, default=24, help="hour-by-hour table for the last this many hours")
    a = ap.parse_args()
    D.mkdir(parents=True, exist_ok=True)
    if a.pull:
        c = SigClient()
        c.read_limiter = RateLimiter(a.reads)
        c.write_limiter = RateLimiter(1)
        rows = list(c.paginate(f"/tournaments/{TOURNAMENT_SLUG}/portfolio/fills", limit=200))
        (D / "fills_all.json").write_text(json.dumps(rows), encoding="utf-8")
        print(f"{len(rows):,} fills saved")
        return
    fills = sorted(json.loads((D / "fills_all.json").read_text(encoding="utf-8")), key=lambda f: (f["filledAt"], f["id"]))
    start, end = ts(fills[0]["filledAt"]), ts(fills[-1]["filledAt"])
    man = {"start": fills[0]["filledAt"], "end": fills[-1]["filledAt"]}
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    kmap = u["kalshi"]
    title = {str(e["id"]): m["title"] for m in u["markets"] for e in m["exchanges"]}
    hist = kalshi_history({kmap[str(f["exchangeId"])] for f in fills if str(f["exchangeId"]) in kmap},
                          int(start) - 3600, int(end) + 60)

    def kfair(ex, t):
        s = hist.get(kmap.get(ex))
        if not s:
            return None
        i = bisect.bisect_right(s[0], t) - 1
        return s[1][i] if i >= 0 and t - s[0][i] <= 600 else None

    tapes = {}
    for f in (D / "ex").glob("*.json"):
        rows = sorted((ts(r["createdAt"]), float(r["price"]), float(r["size"])) for r in
                      json.loads(f.read_text(encoding="utf-8")) if r.get("price") is not None and r.get("size"))
        tapes[f.stem] = ([r[0] for r in rows], rows)

    def sig_yes(ex, t):
        """VWAP Yes price of the exchange's prints in the 15 min before t (SIG's own level)."""
        tp = tapes.get(ex)
        if not tp:
            return None
        i1 = bisect.bisect_left(tp[0], t - 0.5)
        i0 = bisect.bisect_left(tp[0], t - 900)
        rows = tp[1][i0:i1]
        sz = sum(r[2] for r in rows)
        return sum(r[1] * r[2] for r in rows) / sz if sz else None

    # FIFO lots per exchange: (signed side, qty, cost per share in held side's terms, t, tag of entry)
    lots = defaultdict(deque)
    closes = []      # dicts per closed chunk
    entries = []
    for f in fills:
        ex, t = str(f["exchangeId"]), ts(f["filledAt"])
        side, q, px = f["side"], abs(float(f["quantity"])), float(f["price"])
        L = lots[ex]
        held = L[0][0] if L else None
        if held is None or held == side:       # entry: grows the position on `side`
            fy, sy = kfair(ex, t), sig_yes(ex, t)
            fs = None if fy is None else (fy if side == "yes" else 1 - fy)
            ss = None if sy is None else (sy if side == "yes" else 1 - sy)
            tag = {"under_kalshi": None if fs is None else fs - px, "under_sig": None if ss is None else ss - px,
                   "t": t, "ex": ex}
            L.append([side, q, px, t, tag])
            entries.append({**tag, "qty": q, "cost": q * px})
            continue
        exit_px = 1 - px                        # buying the other side at px sells the held side at 1 - px
        while q > 1e-9 and L:
            lot = L[0]
            take = min(q, lot[1])
            closes.append({"ex": ex, "t": t, "qty": take, "pnl": take * (exit_px - lot[2]), "per": exit_px - lot[2],
                           "held_min": (t - lot[3]) / 60, "entry": lot[4], "entry_px": lot[2]})
            lot[1] -= take
            q -= take
            if lot[1] <= 1e-9:
                L.popleft()
        if q > 1e-9:                            # flipped through to the other side: the rest is a new entry
            fy, sy = kfair(ex, t), sig_yes(ex, t)
            fs = None if fy is None else (fy if side == "yes" else 1 - fy)
            ss = None if sy is None else (sy if side == "yes" else 1 - sy)
            tag = {"under_kalshi": None if fs is None else fs - px, "under_sig": None if ss is None else ss - px,
                   "t": t, "ex": ex}
            L.append([side, q, px, t, tag])
            entries.append({**tag, "qty": q, "cost": q * px})

    hours = (end - start) / 3600
    tot = sum(c["pnl"] for c in closes)
    print(f"Window {man['start'][:16]}Z -> {man['end'][:16]}Z ({hours:.0f} h): {len(fills):,} own fills, "
          f"{len(entries):,} entries ({sum(e['cost'] for e in entries):,.0f} SUSQies), {len(closes):,} closed chunks")
    print(f"Realized cash P&L on round trips opened and closed in the window: {tot:+,.0f} ({tot / hours:+,.0f}/h); "
          f"winners {sum(c['pnl'] for c in closes if c['pnl'] > 0):+,.0f}, losers {sum(c['pnl'] for c in closes if c['pnl'] < 0):+,.0f}")
    open_val = 0.0
    open_cost = 0.0
    for ex, L in lots.items():
        for side, q, px, t0, tag in L:
            fy = kfair(ex, end)
            if fy is not None:
                open_val += q * ((fy if side == "yes" else 1 - fy) - px)
            open_cost += q * px
    print(f"Still open from window entries: cost {open_cost:,.0f}, worth {open_val:+,.0f} vs cost at Kalshi fair")

    def table(name, key, edges=None, fmt="{:+.2f}"):
        agg = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
        for c in closes:
            k = key(c)
            k = bucket(k, edges, fmt) if edges else k
            a = agg[k]
            a[0] += 1
            a[1] += c["qty"]
            a[2] += c["pnl"]
            a[3] += c["qty"] * c["entry_px"]
        print(f"\nBy {name}: closes | shares | realized P&L | per share | return on cost")
        for k in sorted(agg):
            n, sh, p, cost = agg[k]
            print(f"  {k:<14} {n:>6,} {sh:>10,.0f} {p:>+10,.0f} {p / sh:>+8.4f} {p / cost:>+8.2%}")

    table("entry distance under Kalshi fair", lambda c: c["entry"]["under_kalshi"],
          (-0.05, 0.0, 0.02, 0.04, 0.06, 0.10, 0.15, 0.25))
    table("entry distance under SIG's 15-min VWAP", lambda c: c["entry"]["under_sig"],
          (-0.05, -0.02, 0.0, 0.02, 0.04, 0.06, 0.10, 0.20))
    table("holding time (min)", lambda c: c["held_min"], (0, 2, 5, 15, 30, 60, 120, 360, 1440), "{:.0f}")
    table("UTC day of the close", lambda c: datetime.fromtimestamp(c["t"], timezone.utc).strftime("%m-%d"))
    recent = [c for c in closes if c["t"] >= end - a.hours * 3600]
    by_h = defaultdict(float)
    for c in recent:
        by_h[datetime.fromtimestamp(c["t"], timezone.utc).strftime("%m-%d %H")] += c["pnl"]
    print(f"\nRealized P&L by UTC hour, last {a.hours:g} h:")
    for k in sorted(by_h):
        print(f"  {k}h {by_h[k]:>+9,.0f}")
    worst = defaultdict(float)
    for c in closes:
        worst[c["ex"]] += c["pnl"]
    print("\nExchanges by realized P&L (worst 10 | best 10):")
    ranked = sorted(worst.items(), key=lambda kv: kv[1])
    for ex, p in ranked[:10]:
        print(f"  {p:>+9,.0f}  {ex:>5}  {title.get(ex, '')[:60]}")
    print("  ...")
    for ex, p in ranked[-10:]:
        print(f"  {p:>+9,.0f}  {ex:>5}  {title.get(ex, '')[:60]}")


if __name__ == "__main__":
    main()
