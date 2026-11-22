"""Calibrate the election-night call detector (sigbot/calls.py) on the 2024 night. READ-ONLY: Kalshi public API only.

  python research/calibrate_calls.py                 # Senate + Governor + all House events, ~500 Kalshi requests, a few min
  python research/calibrate_calls.py --no-house      # Senate + Governor only (quick)

For every 2024 race Kalshi listed (SENATE{ST}-24, GOVPARTY{ST}-24, HOUSE{ST}{N}-24, from /historical/markets with the
`result` of each party market) it pulls 1-minute candles from 5 Nov 2024 22:00Z to 9 Nov 00:00Z and asks, per
threshold and hold: when did one party's price first hold >= threshold for `hold` consecutive minutes, how long
after the state's last poll close, and did that party win? Price per minute = mid of the closing bid/ask, or the bid
alone when the ask is gone (a called race often has no ask). Also: after such a "call", did the price ever fall back
under 0.90 (the detector's retraction floor)?

The night's detector calls a race when Kalshi AND Polymarket both hold >= 0.97 for 30 s (or one >= 0.99 with the
other >= 0.90), so these Kalshi-only numbers are a conservative bound on its false-call rate.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from sigbot.client import ROOT
from sigbot.kalshi import STATES
from sigbot.swing import POLL_CLOSE_H

BASE = "https://api.elections.kalshi.com/trade-api/v2"
NIGHT0_2024 = datetime(2024, 11, 6, 0, 0, tzinfo=timezone.utc)      # 19:00 EST, 5 Nov 2024
WINDOW = (datetime(2024, 11, 5, 22, 0, tzinfo=timezone.utc), datetime(2024, 11, 9, 0, 0, tzinfo=timezone.utc))
# House seats per state after the 2020 census (at-large states use district 1 on Kalshi, e.g. HOUSEAK1-24)
SEATS = {"AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5, "DE": 1, "FL": 28, "GA": 14, "HI": 2,
         "ID": 2, "IL": 17, "IN": 9, "IA": 4, "KS": 4, "KY": 6, "LA": 6, "ME": 2, "MD": 8, "MA": 9, "MI": 13, "MN": 8,
         "MS": 4, "MO": 8, "MT": 2, "NE": 3, "NV": 4, "NH": 2, "NJ": 12, "NM": 3, "NY": 26, "NC": 14, "ND": 1, "OH": 15,
         "OK": 5, "OR": 6, "PA": 17, "RI": 2, "SC": 7, "SD": 1, "TN": 9, "TX": 38, "UT": 4, "VT": 1, "VA": 11, "WA": 10,
         "WV": 2, "WI": 8, "WY": 1}
THRESHOLDS = (0.95, 0.97, 0.98, 0.99)
HOLDS = (1, 2, 3)          # consecutive minutes


class K:
    def __init__(self):
        self.s = requests.Session()
        self.n = 0

    def get(self, path, **params):
        for attempt in range(6):
            r = self.s.get(f"{BASE}{path}", params=params, timeout=20)
            self.n += 1
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        return None


def events(house: bool) -> list[tuple[str, str, str]]:
    """(event ticker, state, race type)."""
    out = [(f"SENATE{st}-24", st, "Senate") for st in STATES.values()]
    out += [(f"SENATE{st}S-24", st, "Senate") for st in ("NE", "OH", "CA")]   # 2024 specials
    out += [(f"GOVPARTY{st}-24", st, "Governor") for st in STATES.values()]
    if house:
        out += [(f"HOUSE{st}{d}-24", st, "House") for st, n in SEATS.items() for d in range(1, n + 1)]
    return out


def series_of(ticker: str) -> str:
    return ticker.split("-")[0]


def minute_prices(k: K, ticker: str) -> list[tuple[int, float, float]]:
    """[(unix minute end, price, low bid)] over WINDOW."""
    out = []
    t0, t1 = int(WINDOW[0].timestamp()), int(WINDOW[1].timestamp())
    step = 3 * 24 * 3600
    for a in range(t0, t1, step):
        d = k.get(f"/historical/markets/{ticker}/candlesticks", start_ts=a, end_ts=min(t1, a + step),
                  period_interval=1)
        for c in (d or {}).get("candlesticks", []):
            bid = (c.get("yes_bid") or {}).get("close")
            ask = (c.get("yes_ask") or {}).get("close")
            low = (c.get("yes_bid") or {}).get("low")
            b = float(bid) if bid not in (None, "") else None
            s_ = float(ask) if ask not in (None, "") else None
            if b is None and s_ is None:
                continue
            if b is not None and s_ is not None and s_ < 1:
                p = (b + s_) / 2
            elif b is not None:
                p = b
            else:
                p = s_
            out.append((int(c["end_period_ts"]), p, float(low) if low not in (None, "") else (b or p)))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-house", action="store_true")
    a = ap.parse_args()
    k = K()
    races = []
    for ev, st, rt in events(not a.no_house):
        d = k.get("/historical/markets", event_ticker=ev)
        ms = [m for m in (d or {}).get("markets", []) if m.get("result") in ("yes", "no")]
        if len(ms) < 2 or sum(m["result"] == "yes" for m in ms) != 1:
            continue
        races.append((ev, st, rt, ms))
    print(f"{len(races)} races with a settled winner ({k.n} requests so far)", flush=True)

    rows = []
    for i, (ev, st, rt, ms) in enumerate(races):
        close = NIGHT0_2024 + timedelta(hours=POLL_CLOSE_H.get(st, 2.0))
        series = {m["ticker"]: minute_prices(k, m["ticker"]) for m in ms}
        winner = next(m["ticker"] for m in ms if m["result"] == "yes")
        pre = {t: max([p for ts, p, _ in s if ts <= close.timestamp()] or [None], key=lambda x: x or 0)
               for t, s in series.items()}
        for th in THRESHOLDS:
            for hold in HOLDS:
                first = None
                for t, s in series.items():
                    run = 0
                    for ts, p, _ in s:
                        if ts < close.timestamp():
                            continue
                        run = run + 1 if p >= th else 0
                        if run >= hold:
                            if first is None or ts < first[0]:
                                first = (ts, t)
                            break
                if first is None:
                    rows.append({"race": ev, "type": rt, "th": th, "hold": hold, "called": 0})
                    continue
                ts, t = first
                dipped = any(p < 0.90 or low < 0.90 for tt, p, low in series[t] if tt > ts)
                rows.append({"race": ev, "type": rt, "th": th, "hold": hold, "called": 1, "right": int(t == winner),
                             "min_after_close": round((ts - close.timestamp()) / 60, 1), "dipped": int(dipped),
                             "pre_close_p": pre.get(t)})
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(races)} races ({k.n} requests)", flush=True)

    out = ROOT / "data" / "calibrate_calls.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k_ for r in rows for k_ in r}))
        w.writeheader()
        w.writerows(rows)

    print(f"\n2024 night, Kalshi only, {len(races)} races (calls counted from each state's last poll close):")
    print("  type      thresh hold | called  wrong  wrong%   dipped<0.90 | median min after close (contested*)")
    for rt in ("all", "Senate", "Governor", "House"):
        for th in THRESHOLDS:
            for hold in HOLDS:
                rs = [r for r in rows if r["th"] == th and r["hold"] == hold and r["called"]
                      and (rt == "all" or r["type"] == rt)]
                if not rs:
                    continue
                wrong = sum(1 - r["right"] for r in rs)
                dip = sum(r["dipped"] for r in rs)
                cont = sorted(r["min_after_close"] for r in rs if (r.get("pre_close_p") or 0) < 0.95)
                med = cont[len(cont) // 2] if cont else float("nan")
                print(f"  {rt:<9} {th:5.2f} {hold:>4}m | {len(rs):6d} {wrong:6d} {wrong / len(rs):7.2%} "
                      f"{dip:8d} ({dip / len(rs):5.1%}) | {med:7.1f} (n={len(cont)})")
    print(f"\n* contested = the eventual caller was under 0.95 at poll close. CSV: {out}  ({k.n} Kalshi requests)")
    wrong_list = [r for r in rows if r["called"] and not r["right"] and r["th"] >= 0.97 and r["hold"] == 1]
    for r in wrong_list[:20]:
        print("   wrong:", json.dumps(r))


if __name__ == "__main__":
    main()
