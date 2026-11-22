"""Estimate the leaders' net positions from the leaderboard, so the bold overlay can aim at beating THEM. READ-ONLY.

  python research/leader_exposure.py                 # last 48 h of run-logs maker logs + 1 SIG read (all-time board)
  python research/leader_exposure.py --hours 72 --top 12 --write

Every live maker run logs the 1-day leaderboard every ~15 s (`lb` rows: account, 1d pnl) and SIG's prints with the
book top (`tape` rows). An account's pnl moves with the marks of what it holds: on a 5-minute grid,
  d pnl(account) ~= sum over exchanges of (net YES shares held) x d mid(exchange)  (+ its trading, as noise)
so a ridge regression of each account's pnl steps on the mid steps of the most-traded exchanges estimates its net
exposures. Validation: the same fit on OUR account, whose positions the maker logs every cycle; it is only trusted
if our own big positions come back within --tolerance.

Output config/leader_exposure.json: {saved, validation, leaders: {name: {balance, exposures: {ex: net YES shares}}}}
(balance = 100k + all-time pnl). election.py's bold overlay then sets the target per simulated outcome to the 3rd
highest leader balance in THAT outcome (sigbot/bold.py: leader_targets) instead of one number.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import gzip
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from sigbot.client import ROOT, SigClient

OUT = ROOT / "config" / "leader_exposure.json"
ME = "steady-acorn-6517"
GRID = 300


def git(*args, binary=False):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=120)
    return r.stdout if binary else r.stdout.decode()


def ts(s: str) -> float:
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s).timestamp()


def load(hours: float):
    git("fetch", "-q", "origin", "run-logs")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    names = [n for n in git("ls-tree", "-r", "--name-only", "FETCH_HEAD", "maker_logs/").split()
             if n.endswith(".jsonl.gz") and (m := re.search(r"_(\d{8}T\d{6})Z", n))
             and datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc) >= since]
    pnl = defaultdict(list)        # account -> [(t, 1d pnl)]
    mids = defaultdict(list)       # ex -> [(t, mid)]
    mypos = []                     # [(t, {ex: net YES shares})]
    for n in names:
        for line in gzip.decompress(git("show", f"FETCH_HEAD:{n}", binary=True)).decode().splitlines():
            if '"kind": "lb"' in line:
                d = json.loads(line)
                t = ts(d["t1"])
                for r in d["rows"]:
                    pnl[r["u"]].append((t, float(r["pnl"])))
            elif '"kind": "tape"' in line:
                for at, ex, price, qty, _id, bid, ask, _seen in json.loads(line).get("trades", []):
                    mid = (bid + ask) / 2 if bid is not None and ask is not None and ask - bid <= 0.05 else price
                    mids[str(ex)].append((ts(at), mid))
            elif '"kind": "cycle"' in line:
                d = json.loads(line)
                mypos.append((ts(d["ts"]), {ex: (h["qty"] if h["side"] == "yes" else -h["qty"])
                                            for ex, h in (d.get("positions") or {}).items()}))
    return names, pnl, mids, mypos


def on_grid(series: list[tuple], grid: np.ndarray) -> np.ndarray:
    """Last value at or before each grid time (NaN before the first)."""
    s = sorted(series)
    t = np.array([x[0] for x in s])
    v = np.array([x[1] for x in s])
    i = np.searchsorted(t, grid, side="right") - 1
    out = np.where(i >= 0, v[np.clip(i, 0, None)], np.nan)
    return out


def fit(y: np.ndarray, X: np.ndarray, lam: float) -> np.ndarray:
    """Ridge on the rows where y and every regressor step are known. Day boundaries (1d pnl resets at 00Z) and
    reconnect gaps are dropped by the caller as NaN."""
    ok = ~np.isnan(y) & ~np.isnan(X).any(1)
    Xo, yo = X[ok], y[ok]
    if len(yo) < 30:
        return np.full(X.shape[1], np.nan)
    A = Xo.T @ Xo + lam * np.eye(X.shape[1])
    return np.linalg.solve(A, Xo.T @ yo)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--top", type=int, default=12, help="leaders by all-time pnl to model")
    ap.add_argument("--exchanges", type=int, default=30, help="regressors: most-printed exchanges")
    ap.add_argument("--lam", type=float, default=1e-3, help="ridge penalty (in mid-step units squared)")
    ap.add_argument("--tolerance", type=float, default=0.3)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    names, pnl, mids, mypos = load(a.hours)
    if not pnl or not mids:
        print(f"no leaderboard/tape rows in the last {a.hours:g} h ({len(names)} logs): the realtime feed must be live "
              "for the maker to log them (lbtrack only polls while it is)")
        return
    t0 = max(min(t for s in pnl.values() for t, _ in s), min(t for s in mids.values() for t, _ in s))
    t1 = max(t for s in pnl.values() for t, _ in s)
    grid = np.arange(t0 - t0 % GRID + GRID, t1, GRID)
    exs = sorted(mids, key=lambda e: -len(mids[e]))[:a.exchanges]
    M = np.column_stack([on_grid(mids[e], grid) for e in exs])
    dX = np.diff(M, axis=0)
    day = (grid // 86400)
    same_day = day[1:] == day[:-1]
    print(f"{len(names)} logs, {len(grid)} 5-min steps ({(t1 - t0) / 3600:.0f} h), {len(pnl)} accounts on the 1d board, "
          f"{len(exs)} exchanges as regressors")

    def exposures(acct):
        p = on_grid(pnl[acct], grid)
        dy = np.diff(p)
        dy[~same_day] = np.nan
        return dict(zip(exs, fit(dy, dX, a.lam)))

    # validation on our own account (positions known from the maker's cycle rows)
    val = {}
    if ME in pnl and mypos:
        est = exposures(ME)
        mid_t = (t0 + t1) / 2
        actual = min(mypos, key=lambda r: abs(r[0] - mid_t))[1]
        big = sorted(((abs(actual.get(e, 0)), e) for e in exs), reverse=True)[:5]
        errs = []
        print(f"\nvalidation on {ME} (positions at the middle of the window vs fitted):")
        for q, e in big:
            if q < 1000:
                continue
            got = est.get(e, float("nan"))
            err = abs(got - actual.get(e, 0)) / max(abs(actual.get(e, 0)), 1)
            errs.append(err)
            print(f"   exchange {e:>5}: actual {actual.get(e, 0):>10,.0f}  fitted {got:>10,.0f}  error {err:.0%}")
        val = {"median_error": float(np.median(errs)) if errs else None, "n": len(errs)}
        print(f"   median error {val['median_error'] if errs else 'n/a'} (trusted if <= {a.tolerance:.0%})")
    else:
        print(f"\nno rows for {ME}: validation impossible")

    board = SigClient(log=False).get("/leaderboards").get("leaderboard", [])
    top = sorted(board, key=lambda r: r.get("rank") or 1e9)[:a.top]
    leaders = {}
    print(f"\ntop {a.top} all-time and their largest fitted exposures (net YES shares):")
    for r in top:
        u = r.get("username")
        bal = 100000.0 + float(r.get("pnl") or 0)
        exp = exposures(u) if u in pnl else {}
        exp = {e: float(v) for e, v in exp.items() if np.isfinite(v) and abs(v) >= 1000}
        leaders[u] = {"balance": bal, "exposures": exp}
        tops = sorted(exp.items(), key=lambda kv: -abs(kv[1]))[:4]
        print(f"   {u:<22} balance {bal:>10,.0f}  " + ", ".join(f"{e}: {v:+,.0f}" for e, v in tops))
    trusted = bool(val.get("median_error") is not None and val["median_error"] <= a.tolerance)
    print(f"\nexposures trusted: {trusted}")
    if a.write:
        OUT.write_text(json.dumps({"saved": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                                   "trusted": trusted, "validation": val, "window_h": a.hours,
                                   "leaders": leaders}, indent=1), encoding="utf-8")
        print(f"wrote {OUT.name}")


if __name__ == "__main__":
    main()
