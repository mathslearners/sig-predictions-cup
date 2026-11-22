"""Election-night tuning grid (OFFLINE, sends nothing): each variant of the night bot's settings against several
assumptions about SIG's books on the night, on the same simulated nights (paired seeds), in parallel.

  python research/sim_grid.py --nights 8 --holds data/election/positions_20261026.json --races-cache races.json --out grid.json

Variants (26 Oct; all off in the live defaults):
  base   today's settings: holds kept into the night, bold at 22:30Z
  sell   maker.py --night-sell-all: every hold to cash before the night (~4.8c/share given up)
  caps   --race-cap-sure 0.5: half the bankroll per race while both venues hold the winner >= 0.99
  recyc  --recycle-always 1 --recycle-bid-min 0.975: called winners sold back into bids as soon as takes wait
  late   --bold-at 01:00Z: the bold overlay plans after the first results instead of before them
  loose  --race-cap 0.15 --lock-keep 0.25: more per uncalled race, less cash kept back from L/P takes
  all4 / all5   sell+caps+recyc+late (+loose)
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import json
import time
from multiprocessing import Pool

import numpy as np

import sim_election as se

COMMON = {"start_cash": 50000.0, "third": 965000.0, "called_mid": 0.985, "bold_at": "-90", "leader_gain_hi": 0.3}
VARIANTS = {
    "base": {},
    "sell": {"liquidate": True},
    "caps": {"race_cap_sure": 0.5},
    "recyc": {"recycle_always": True, "recycle_bid_min": 0.975},
    "late": {"bold_at": "60"},
    "loose": {"race_cap": 0.15, "lock_keep": 0.25},
}
VARIANTS["all4"] = {k: v for n in ("sell", "caps", "recyc", "late") for k, v in VARIANTS[n].items()}
VARIANTS["all5"] = {**VARIANTS["all4"], **VARIANTS["loose"]}
SCENARIOS = {   # how much stale liquidity the night really has
    "deep": {"depth": 20000, "rivals": 0.7, "refill": 1.0, "winner_buys": 2000},
    "deep-contested": {"depth": 20000, "rivals": 0.9, "refill": 0.2, "winner_buys": 1000},
    "thin": {"depth": 3000, "rivals": 0.7, "refill": 1.0, "winner_buys": 2000},
    "thin-contested": {"depth": 3000, "rivals": 0.9, "refill": 0.2, "winner_buys": 500},
}
_races = None


def job(args):
    variant, scenario, seed, base = args
    global _races
    if _races is None:
        _races, _ = se.load_races(base["races_cache"])
    a = se.build_parser().parse_args([])
    for k, v in {**base, **COMMON, **SCENARIOS[scenario], **VARIANTS[variant]}.items():
        setattr(a, k, v)
    t = time.time()
    r = se.night(_races, a, seed, bold=True)
    return {"variant": variant, "scenario": scenario, "seed": seed, "final": r["final"], "secs": time.time() - t,
            "edge": {k: v for k, v in r["edge"].items() if k.startswith(("edge_", "bold_", "recycle"))},
            "stats": {k: v for k, v in r["stats"].items() if k.startswith(("realised", "liquidated"))}}


def summary(rows, bars=(965000, 1100000)) -> str:
    out = ["| scenario | variant | nights | mean | median | p90 | max | "
           + " | ".join(f"P(>= {b / 1e3:.0f}k)" for b in bars) + " | vs base (paired mean) |",
           "|---|---|---:|---:|---:|---:|---:|" + "---:|" * len(bars) + "---:|"]
    for sc in SCENARIOS:
        base = {r["seed"]: r["final"] for r in rows if r["scenario"] == sc and r["variant"] == "base"}
        for v in VARIANTS:
            rs = sorted((r for r in rows if r["scenario"] == sc and r["variant"] == v), key=lambda r: r["seed"])
            if not rs:
                continue
            f = np.array([r["final"] for r in rs])
            d = [r["final"] - base[r["seed"]] for r in rs if r["seed"] in base]
            out.append(f"| {sc} | {v} | {len(f)} | {f.mean():,.0f} | {np.median(f):,.0f} | "
                       f"{np.percentile(f, 90):,.0f} | {f.max():,.0f} | "
                       + " | ".join(f"{(f >= b).mean():.2f}" for b in bars)
                       + f" | {np.mean(d) if d else float('nan'):+,.0f} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nights", type=int, default=8)
    ap.add_argument("--seed0", type=int, default=0)
    ap.add_argument("--holds", required=True)
    ap.add_argument("--races-cache", required=True)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--procs", type=int, default=10)
    ap.add_argument("--out", default="sim_grid.json")
    ap.add_argument("--summary", action="store_true", help="print the table for --out and exit")
    a = ap.parse_args()
    rows = []
    try:
        rows = json.load(open(a.out, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if a.summary:
        print(summary(rows))
        return
    base = {"holds": a.holds, "races_cache": a.races_cache}
    jobs = [(v, s, seed, base) for seed in range(a.seed0, a.seed0 + a.nights)
            for s in a.scenarios.split(",") for v in a.variants.split(",")]
    done = {(r["variant"], r["scenario"], r["seed"]) for r in rows}
    jobs = [j for j in jobs if j[:3] not in done]
    print(f"{len(jobs)} nights to run ({len(done)} already in {a.out})", flush=True)
    with Pool(a.procs) as pool:
        for i, r in enumerate(pool.imap_unordered(job, jobs), 1):
            rows.append(r)
            json.dump(rows, open(a.out, "w", encoding="utf-8"))
            print(f"[{i}/{len(jobs)}] {r['scenario']:15} {r['variant']:6} seed {r['seed']}: {r['final']:,.0f} "
                  f"({r['secs']:.0f} s)", flush=True)
    print(summary(rows))


if __name__ == "__main__":
    main()
