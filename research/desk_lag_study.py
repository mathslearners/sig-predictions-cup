"""How fresh are news-desk projections vs Kalshi? 2024 night replay for the --desk-calls rule. READ-ONLY, public data.

  python research/desk_lag_study.py              # Senate + Governor (~150 Kalshi requests)
  python research/desk_lag_study.py --house      # + every House race (~1,300 requests, several minutes)

Desk time = NYT's `winner_accepted_at` (AP's call as it landed in static01.nyt.com/.../results-{office}.json; NBC's
files carry no call time). Kalshi = 1-minute candles of the winner's market (calibrate_calls.minute_prices). Per race:
  venue  first minute the winner held >= 0.97 for 2 minutes after the state's last poll close (~ the detector's
         both-venues 60 s rule, Kalshi only)
  desk   first minute at/after the desk call with the winner >= 0.95 on Kalshi (the --desk-calls rule)
  gain   venue - desk (minutes; > 0 = the desk rule calls first)
Writes data/desk_lag_study.csv.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import csv
import statistics
from datetime import datetime, timedelta

import requests

from calibrate_calls import NIGHT0_2024, K, events, minute_prices
from sigbot.client import ROOT
from sigbot.swing import POLL_CLOSE_H

NYT = "https://static01.nyt.com/elections-assets/pages/data/2024-11-05/results-{}.json"


def desk_times() -> dict[tuple, tuple[float, str]]:
    """(state, office, district|0, special) -> (unix call time, winner party)."""
    out = {}
    for office in ("senate", "governor", "house"):
        d = requests.get(NYT.format(office), timeout=30, headers={"Accept-Encoding": "gzip"}).json()
        for r in d["races"]:
            o = r.get("outcome") or {}
            at, won = o.get("winner_accepted_at") or [], o.get("won") or []
            if len(won) != 1 or not at:
                continue
            st = r["reporting_units"][0]["state_postal"]
            dist = int(r.get("seat_number") or 0) if r["office_id"] == "H" else 0
            party = (((r.get("candidate_metadata") or {}).get(won[0]) or {}).get("party") or {}).get("nyt_id")
            special = "special" in (r.get("type") or "").lower()
            out[(st, r["office_id"], dist, special)] = (datetime.fromisoformat(at[0]).timestamp(), party)
    return out


def key_of(ev: str, st: str, rt: str) -> tuple:
    if rt == "House":
        return st, "H", int(ev.split("-")[0][len("HOUSE") + len(st):]), False
    return st, "S" if rt == "Senate" else "G", 0, ev.startswith(f"SENATE{st}S-")


def first(series, start: float, th: float, hold: int = 1) -> float | None:
    run = 0
    for ts, p, _ in series:
        if ts < start:
            continue
        run = run + 1 if p >= th else 0
        if run >= hold:
            return ts
    return None


def price_at(series, t: float) -> float | None:
    before = [p for ts, p, _ in series if ts <= t + 60]
    return before[-1] if before else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--house", action="store_true")
    a = ap.parse_args()
    desk = desk_times()
    k = K()
    rows = []
    for ev, st, rt in events(a.house):
        dk = desk.get(key_of(ev, st, rt))
        if dk is None:
            continue
        d = k.get("/historical/markets", event_ticker=ev)
        ms = [m for m in (d or {}).get("markets", []) if m.get("result") in ("yes", "no")]
        win = [m for m in ms if m["result"] == "yes"]
        if len(win) != 1:
            continue
        s = minute_prices(k, win[0]["ticker"])
        if not s:
            continue
        close = (NIGHT0_2024 + timedelta(hours=POLL_CLOSE_H.get(st, 2.0))).timestamp()
        t_desk = dk[0]
        pre = price_at(s, close - 60)
        venue = first(s, close, 0.97, 2)
        drule = first(s, max(t_desk - 60, close), 0.95)
        rows.append({"race": ev, "type": rt, "pre_close_p": pre, "desk_min": round((t_desk - close) / 60, 1),
                     "kalshi_at_desk": price_at(s, t_desk),
                     "venue_min": None if venue is None else round((venue - close) / 60, 1),
                     "desk_rule_min": None if drule is None else round((max(drule, t_desk) - close) / 60, 1)})
        r = rows[-1]
        r["gain_min"] = (None if r["venue_min"] is None or r["desk_rule_min"] is None
                         else round(r["venue_min"] - r["desk_rule_min"], 1))
    out = ROOT / "data" / "desk_lag_study.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    def summary(name, rs):
        g = [r["gain_min"] for r in rs if r["gain_min"] is not None]
        kd = [r["kalshi_at_desk"] for r in rs if r["kalshi_at_desk"] is not None]
        if not g:
            print(f"  {name:<22} n={len(rs)}: no comparable calls")
            return
        print(f"  {name:<22} n={len(rs):3d} comparable {len(g):3d} | desk first {sum(x > 0.5 for x in g):3d}, "
              f"Kalshi first {sum(x < -0.5 for x in g):3d}, tie {sum(abs(x) <= 0.5 for x in g):3d} | gain median "
              f"{statistics.median(g):6.1f} min, mean {statistics.mean(g):6.1f} | Kalshi at the desk call: median "
              f"{statistics.median(kd):.3f}, >= 0.95 in {sum(x >= 0.95 for x in kd)}/{len(kd)}")

    print(f"\n2024 night, {len(rows)} races with an AP call time (NYT) and Kalshi candles ({k.n} Kalshi requests):")
    for rt in ("Senate", "Governor", "House"):
        rs = [r for r in rows if r["type"] == rt]
        if rs:
            summary(rt, rs)
            summary(rt + " contested", [r for r in rs if (r["pre_close_p"] or 0) < 0.95])
    print(f"  contested = the winner was under 0.95 on Kalshi at its state's last poll close. CSV: {out}")
    for r in sorted((r for r in rows if r["gain_min"] is not None), key=lambda r: -r["gain_min"])[:12]:
        print("   ", r)


if __name__ == "__main__":
    main()
