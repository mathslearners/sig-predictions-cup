"""Trade-tape study for the SIG Predictions Cup bot. READ-ONLY: places and cancels nothing.

  python research/tape_study.py --hours 24        # pulls every exchange's tape + own fills, values them at Kalshi fair

For every trade in the window: how far from Kalshi fair it printed (YES-normalized), how much edge the right side
of it earned (|price - fair| x size), whether it was part of a sweep (several prints on one exchange within a
second), and how much of that edge our own fills took. Reports by distance bucket, by exchange, by sweep size and
by our free cash at the time, so we can see what is left on the table and why.

Paces itself at --reads reads/min (default 8) to share the key with the live maker + sniper (~90 reads/min).
Raw tapes and fills are cached in data/tapes/ so re-runs only analyse.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from daily_report import at, kalshi_history, ts
from sigbot.client import ROOT, RateLimiter, SigClient
from sigbot.markets import TOURNAMENT_SLUG as T

CACHE = ROOT / "data" / "tapes"
BUCKETS = (0.0, 0.03, 0.06, 0.10, 0.15, 0.25, 1.01)


def bucket(d: float) -> str:
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        if lo <= d < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "?"


def cash_series() -> tuple[list, list]:
    """(timestamps, free cash) from the maker status lines on run-logs."""
    pts = []
    if subprocess.run(["git", "fetch", "-q", "origin", "run-logs"], cwd=ROOT).returncode:
        return [], []
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", "FETCH_HEAD", "maker_logs/"], cwd=ROOT,
                           capture_output=True, text=True).stdout.split()
    for n in names:
        m = re.search(r"maker_(\d{8}T\d{6}Z)\.out$", n)
        if not m:
            continue
        end = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        txt = subprocess.run(["git", "show", f"FETCH_HEAD:{n}"], cwd=ROOT, capture_output=True, text=True).stdout
        for line in txt.splitlines():
            mm = re.match(r"\[(\d\d):(\d\d):(\d\d)Z\] #\d+ cash ([\d,]+)", line)
            if not mm:
                continue
            t = end.replace(hour=int(mm.group(1)), minute=int(mm.group(2)), second=int(mm.group(3)))
            if t > end + timedelta(minutes=1):
                t -= timedelta(days=1)
            pts.append((t.timestamp(), float(mm.group(4).replace(",", ""))))
    pts.sort()
    return [p[0] for p in pts], [p[1] for p in pts]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--reads", type=int, default=8, help="reads/min (the live maker uses ~90 of 100)")
    ap.add_argument("--cached", action="store_true", help="analyse data/tapes/ only, no API calls")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=a.hours)
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    tid, kmap = u["tournamentId"], u["kalshi"]
    title = {e["id"]: m["title"] for m in u["markets"] for e in m["exchanges"]}
    CACHE.mkdir(parents=True, exist_ok=True)
    tag = f"{start:%Y%m%dT%H%MZ}_{now:%Y%m%dT%H%MZ}"

    if a.cached:
        f = sorted(CACHE.glob("tapes_*.json"))[-1]
        raw = json.loads(f.read_text(encoding="utf-8"))
        tapes, fills = raw["tapes"], raw["fills"]
        start, now = datetime.fromisoformat(raw["start"]), datetime.fromisoformat(raw["end"])
    else:
        c = SigClient()
        c.read_limiter = RateLimiter(a.reads)
        c.write_limiter = RateLimiter(1)
        tapes = {}
        for i, ex in enumerate(title):
            rows = []
            for t in c.paginate(f"/exchanges/{ex}/trades", limit=200, tournamentId=tid,
                                **{"from": start.isoformat()[:19] + "Z", "to": now.isoformat()[:19] + "Z"}):
                rows.append(t)
            tapes[ex] = rows
            if i % 20 == 0:
                print(f"  {i}/{len(title)} tapes, {sum(map(len, tapes.values()))} trades", flush=True)
        fills = []
        for f in c.paginate(f"/tournaments/{T}/portfolio/fills", limit=200):
            if f["filledAt"] < start.isoformat()[:19]:
                break
            fills.append(f)
        (CACHE / f"tapes_{tag}.json").write_text(json.dumps(
            {"start": start.isoformat(), "end": now.isoformat(), "tapes": tapes, "fills": fills}), encoding="utf-8")

    hist = kalshi_history({kmap[e] for e in tapes if e in kmap}, int(start.timestamp()) - 3600, int(now.timestamp()))
    cts, cvs = cash_series()

    # Our fills keyed by (exchange, second): edge we took on that exchange in that second.
    ours = defaultdict(float)
    our_sh = defaultdict(float)
    for f in fills:
        ex = str(f["exchangeId"])
        fy = at(hist.get(kmap.get(ex)), ts(f["filledAt"]))
        if fy is None:
            continue
        fs = fy if f["side"] == "yes" else 1 - fy
        e = (fs - f["price"]) * abs(f["quantity"])
        k = (ex, int(ts(f["filledAt"])))
        ours[k] += e
        our_sh[k] += abs(f["quantity"])

    by_b = defaultdict(lambda: [0, 0.0, 0.0, 0.0])            # trades, shares, edge, our edge
    by_ex = defaultdict(lambda: [0.0, 0.0, 0.0])               # edge >= 0.10, our edge there, shares
    by_cash = defaultdict(lambda: [0.0, 0.0])
    by_hour = defaultdict(lambda: [0.0, 0.0])
    sweeps = []
    dir_ = defaultdict(float)
    unpriced = 0
    for ex, rows in tapes.items():
        groups = defaultdict(list)
        for t in rows:
            if t.get("price") is None:
                continue
            groups[int(ts(t["createdAt"]))].append(t)
        for sec, g in groups.items():
            fy = at(hist.get(kmap.get(ex)), sec)
            if fy is None:
                unpriced += len(g)
                continue
            gedge = 0.0
            gsh = 0
            gmax = 0.0
            for t in g:
                d = abs(t["price"] - fy)
                e = d * t["size"]
                b = by_b[bucket(d)]
                b[0] += 1
                b[1] += t["size"]
                b[2] += e
                gedge += e if d >= 0.10 else 0
                gsh += t["size"]
                gmax = max(gmax, d)
                if d >= 0.10:
                    dir_["printed above fair (YES buyers overpaid)" if t["price"] > fy
                         else "printed below fair (YES sellers undersold)"] += e
            mine = ours.get((ex, sec), 0.0)
            # our fills in the same second count against the bucket of the group's deepest print
            by_b[bucket(gmax)][3] += mine
            if gedge:
                by_ex[ex][0] += gedge
                by_ex[ex][1] += max(0.0, mine)
                by_ex[ex][2] += gsh
                cash = at((cts, cvs), sec) if cts else None
                ck = "?" if cash is None else ("<10k" if cash < 10e3 else "10-30k" if cash < 30e3 else
                                                "30-60k" if cash < 60e3 else ">=60k")
                by_cash[ck][0] += gedge
                by_cash[ck][1] += max(0.0, mine)
                hk = datetime.fromtimestamp(sec, timezone.utc).strftime("%H")
                by_hour[hk][0] += gedge
                by_hour[hk][1] += max(0.0, mine)
                sweeps.append((gedge, ex, sec, len(g), gsh, gmax, mine))

    hours = (now - start).total_seconds() / 3600
    print(f"\nWindow {start:%m-%d %H:%MZ} -> {now:%m-%d %H:%MZ} ({hours:.1f} h), "
          f"{sum(map(len, tapes.values()))} trades, {unpriced} without Kalshi fair, our fills {len(fills)}")
    print("\nDistance from fair | trades | shares | edge to the right side | our edge (same second)")
    for k in sorted(by_b):
        n, sh, e, m = by_b[k]
        print(f"  {k:<10} {n:>7} {sh:>11,.0f} {e:>12,.0f} {m:>10,.0f}")
    tot = sum(v[0] for v in by_ex.values())
    got = sum(v[1] for v in by_ex.values())
    print(f"\nEdge in trades >= 0.10 off fair: {tot:,.0f} ({tot / hours:,.0f}/h); ours {got:,.0f} ({got / max(tot, 1):.1%})")
    for k, v in dir_.items():
        print(f"  {k}: {v:,.0f}")
    print("\nBy our free cash at the time (edge >= 0.10 | ours):")
    for k in ("<10k", "10-30k", "30-60k", ">=60k", "?"):
        if k in by_cash:
            print(f"  {k:<7} {by_cash[k][0]:>10,.0f} {by_cash[k][1]:>9,.0f}")
    print("\nBy UTC hour (edge >= 0.10 | ours):")
    for k in sorted(by_hour):
        print(f"  {k}h {by_hour[k][0]:>10,.0f} {by_hour[k][1]:>9,.0f}")
    print("\nTop exchanges (edge >= 0.10 | ours | shares):")
    for ex, v in sorted(by_ex.items(), key=lambda kv: -kv[1][0])[:25]:
        print(f"  {ex:>5} {v[0]:>9,.0f} {v[1]:>8,.0f} {v[2]:>9,.0f}  {title.get(ex, '')[:60]}")
    sz = sorted(s[4] for s in sweeps)
    if sz:
        print(f"\nOff-fair seconds: {len(sweeps)}, shares per second median {sz[len(sz) // 2]:,} "
              f"p90 {sz[int(len(sz) * .9)]:,} max {sz[-1]:,}; multi-print {sum(s[3] > 1 for s in sweeps)}")
    print("\nBiggest 20 (edge, exchange, time, prints, shares, deepest, ours):")
    for e, ex, sec, n, sh, d, m in sorted(sweeps, reverse=True)[:20]:
        print(f"  {e:>8,.0f} {ex:>5} {datetime.fromtimestamp(sec, timezone.utc):%m-%d %H:%M:%S} {n:>3} "
              f"{sh:>8,} {d:.3f} {m:>7,.0f}  {title.get(ex, '')[:45]}")


if __name__ == "__main__":
    main()
