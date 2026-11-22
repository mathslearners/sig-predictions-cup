"""How fast does SIG follow the outside markets, and how slow are SIG's writes? READ-ONLY (run-logs + Kalshi public).

  python research/lag_study.py                    # tapes of the last 4 days on run-logs
  python research/lag_study.py --days 6 --move 0.03

1. Reaction lag. The maker's realtime tape (run-logs maker_logs/*.jsonl.gz, `tape` rows: every SIG print with the
   book top just before it) gives SIG's mid per exchange and minute; Kalshi 1-minute candles give the outside mid.
   An outside move = Kalshi mid moves >= --move within 5 minutes and stays there 10 minutes. For each, from SIG's
   median mid over the 15-5 minutes before: minutes until SIG covers half / 90% of the move, or never within 60 min.
   This is the stale window election.py's lag snipes and call sweeps live on (sim_election.py --sig-lag-min).
2. Write latency proxy. The maker cycles every 20 s; the gap between consecutive cycle lines in the routine logs
   (maker_logs/*.out) minus 20 s is time spent in reads and order writes, by UTC hour. Exact per-batch latency is in
   each run's summary from now on (maker.py `batch_latency`).
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import gzip
import json
import re
import statistics
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from daily_report import kalshi_history
from sigbot.client import ROOT

UNIVERSE = ROOT / "config" / "universe.json"


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=120).stdout


def ts(s: str) -> float:
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s).timestamp()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=4)
    ap.add_argument("--move", type=float, default=0.03)
    a = ap.parse_args()
    git("fetch", "-q", "origin", "run-logs")
    since = datetime.now(timezone.utc) - timedelta(days=a.days)
    names = [n for n in git("ls-tree", "-r", "--name-only", "FETCH_HEAD", "maker_logs/").split()
             if (m := re.search(r"_(\d{8}T\d{6})Z", n)) and datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
             .replace(tzinfo=timezone.utc) >= since]

    # ---- 1. SIG mid per exchange-minute from the tapes
    sig = defaultdict(dict)                    # ex -> minute -> mid
    for n in [n for n in names if n.endswith(".jsonl.gz")]:
        raw = subprocess.run(["git", "show", f"FETCH_HEAD:{n}"], cwd=ROOT, capture_output=True, timeout=120).stdout
        for line in gzip.decompress(raw).decode().splitlines():
            if '"kind": "tape"' not in line:
                continue
            d = json.loads(line)
            for at, ex, price, qty, _id, bid, ask, _seen in d.get("trades", []):
                mid = (bid + ask) / 2 if bid is not None and ask is not None and ask - bid <= 0.05 else price
                sig[str(ex)][int(ts(at) // 60)] = mid
    u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    kmap = {ex: tk for ex, tk in u["kalshi"].items() if ex in sig and len(sig[ex]) >= 30}
    mins = [m for s in sig.values() for m in s]
    if not mins:
        print("no tape rows in range (the realtime feed was down?)")
        return
    t0, t1 = min(mins) * 60, max(mins) * 60
    print(f"tapes: {len(names)} files, {sum(len(s) for s in sig.values()):,} exchange-minutes on {len(sig)} exchanges, "
          f"{datetime.fromtimestamp(t0, timezone.utc):%d %b %H:%MZ} - {datetime.fromtimestamp(t1, timezone.utc):%d %b %H:%MZ}; "
          f"Kalshi history for {len(kmap)}", flush=True)
    hist = kalshi_history(set(kmap.values()), int(t0) - 1800, int(t1) + 3600)

    rows = []
    for ex, tk in kmap.items():
        if tk not in hist:
            continue
        km = {int(t // 60): p for t, p in zip(*hist[tk])}
        s = sig[ex]
        last_event = -10 ** 9
        for m in sorted(km):
            if m - last_event < 30 or (m - 5) not in km or (m + 10) not in km:
                continue
            move = km[m] - km[m - 5]
            if abs(move) < a.move or abs(km[m + 10] - km[m]) > 0.015:
                continue
            before = [s[x] for x in range(m - 15, m - 5) if x in s]
            if len(before) < 2:
                continue
            base = statistics.median(before)
            half = ninety = None
            for x in range(m - 5, m + 61):
                if x not in s:
                    continue
                got = (s[x] - base) / move
                if half is None and got >= 0.5:
                    half = x - (m - 5)
                if got >= 0.9:
                    ninety = x - (m - 5)
                    break
            prints = sum(1 for x in range(m - 5, m + 61) if x in s)
            rows.append({"ex": ex, "at": datetime.fromtimestamp(m * 60, timezone.utc).strftime("%d %H:%MZ"),
                         "move": round(move, 3), "half_min": half, "ninety_min": ninety, "minutes_with_prints": prints})
            last_event = m
    print(f"\n1. SIG reaction to Kalshi moves >= {a.move} in 5 min ({len(rows)} events):")
    if rows:
        for key in ("half_min", "ninety_min"):
            got = sorted(r[key] for r in rows if r[key] is not None)
            never = sum(1 for r in rows if r[key] is None)
            if got:
                print(f"   {key:>10}: median {statistics.median(got):.0f} min, p25 {got[len(got) // 4]}, "
                      f"p75 {got[3 * len(got) // 4]}, never within 60 min: {never}/{len(rows)}")
        for r in rows[:15]:
            print("   ", r)

    # ---- 2. cycle gaps by hour (write latency proxy)
    gaps = defaultdict(list)
    for n in [n for n in names if n.endswith(".out")]:
        prev = None
        for line in git("show", f"FETCH_HEAD:{n}").splitlines():
            m = re.match(r"\[(\d\d):(\d\d):(\d\d)Z\] #\d+ cash", line)
            if not m:
                continue
            sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if prev is not None and 0 < sec - prev < 1800:
                gaps[int(m.group(1))].append(sec - prev - 20)
            prev = sec
    print("\n2. maker cycle overrun (s beyond the 20 s cycle = reads + writes), by UTC hour:")
    for h in sorted(gaps):
        g = sorted(gaps[h])
        print(f"   {h:02d}Z  n {len(g):4d}  median {statistics.median(g):5.1f}  p90 {g[int(len(g) * 0.9)]:5.1f}  "
              f"max {g[-1]:5.0f}")


if __name__ == "__main__":
    main()
