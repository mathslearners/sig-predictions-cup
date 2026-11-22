"""Election-night swing model CLI (sigbot/swing.py). READ-ONLY: Kalshi + Polymarket prices only, no SIG calls.

  python swing.py                          # priors from current prices, sn calibrated to Kalshi's Senate-control price
  python swing.py --save-baseline          # freeze today's prices as the pre-results prior (config/swing_baseline.json)
  python swing.py --live                   # election night: baseline prior + config/swing_calls.csv + market moves
  python swing.py --rehearse 300           # simulated election nights: how much the calls tell about open races
  python swing.py --rehearse 300 --true-sn 5   # ... when the real national swing is wider than the model assumes

Races are modelled on the anti-Republican side: P = 1 - P(R wins) (Nebraska/Montana Senate and Rhode Island Governor
have an Independent; share_d splits an anti-R win between D and I for Senate control). The Ohio special Senate race
isn't listed on SIG but counts for control, so it comes from Kalshi (SENATEOHS-26).

config/swing_calls.csv (election night, one row per race; race names as printed by this script):
  race,winner,margin,frac
  Georgia Senate,D,,            <- called for the Democrat (any non-R winner: D or I)
  Ohio Special Senate,R,,       <- called for the Republican
  Virginia Senate,,6.5,0.40     <- uncalled; anti-R margin +6.5 pts with 40% of the expected vote counted
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone

from sigbot.client import ROOT
from sigbot.kalshi import Kalshi
from sigbot.markets import parse_title
from sigbot.outside import outside_fair
from sigbot.polymarket import Polymarket
from sigbot.races import STATES
from sigbot.swing import POLL_CLOSE_H, Race, SwingMixture, SwingModel, calibrate_sn, rehearse

UNIVERSE = ROOT / "config" / "universe.json"
BASELINE = ROOT / "config" / "swing_baseline.json"
CALLS = ROOT / "config" / "swing_calls.csv"
SENATE_D_HOLDOVERS = 34      # D/I-caucus seats not up in 2026 (47 - 13 Class 2 seats up)
SENATE_CONTROL = 51          # the Vice President (R) breaks a 50-50 tie
EXTRA = {"Ohio Special Senate": ("OH", "Senate", "SENATEOHS-26")}
CODE = {v: k for k, v in STATES.items()}


def prices() -> tuple[list[Race], dict]:
    """Races with P(anti-R) from current Kalshi/Polymarket prices, plus {"senate": P(D control), "house": ...}."""
    u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    kmap, pmap = u["kalshi"], u["polymarket"]
    k, poly = Kalshi(), Polymarket()
    kq = k.quotes(sorted(set(kmap.values())))
    pq = poly.quotes(sorted(set(pmap.values())))

    def fair(ex):
        kraw, praw = kq.get(kmap.get(ex)), pq.get(pmap.get(ex))
        f = outside_fair(kraw, praw)
        if f:
            return f["p"]
        for q in (kraw, praw):   # completeness matters for Senate control: accept a wide quote over nothing
            if q and q.get("mid") is not None:
                return q["mid"]
        return kraw.get("last") if kraw else None

    by_race = defaultdict(dict)
    rtypes = {}
    chamber = {}
    for m in u["markets"]:
        race, rt, party = parse_title(m["title"])
        ex = str(m["exchanges"][0]["id"])
        if rt == "Chamber":
            if party == "D":
                chamber["senate" if "Senate" in race else "house"] = fair(ex)
            continue
        by_race[race][party] = ex
        rtypes[race] = rt
    races, skipped = [], []
    for race, exs in by_race.items():
        p = {party: fair(ex) for party, ex in exs.items()}
        if p.get("R") is not None:
            anti = 1 - p["R"]
        elif p.get("D") is not None and "I" not in p:
            anti = p["D"]
        else:
            skipped.append(race)
            continue
        share_d = 1.0
        if "I" in p and p.get("D") is not None and p.get("I") is not None and p["D"] + p["I"] > 0:
            share_d = p["D"] / (p["D"] + p["I"])
        st = race.split("-")[0] if rtypes[race] == "House" else STATES.get(race.rsplit(" ", 1)[0])
        races.append(Race(race, st, rtypes[race], min(max(anti, 0.0), 1.0), round(share_d, 4), exs))
    for name, (st, rt, ticker) in EXTRA.items():
        q = k.party_quotes(ticker)
        r = q.get("R") or {}
        pr = r.get("mid") if r.get("mid") is not None else r.get("last")
        if pr is not None:
            races.append(Race(name, st, rt, 1 - pr))
        else:
            skipped.append(name)
    if skipped:
        print(f"no price for: {', '.join(skipped)}")
    return races, chamber


def senate(races: list[Race]) -> tuple[list[str], int]:
    names = [r.name for r in races if r.rtype == "Senate"]
    return names, SENATE_CONTROL - SENATE_D_HOLDOVERS


def load_baseline() -> tuple[list[Race], dict, dict]:
    b = json.loads(BASELINE.read_text(encoding="utf-8"))
    return [Race(**r) for r in b["races"]], b["chamber"], b


def read_calls(model: SwingModel) -> int:
    n = 0
    if not CALLS.exists():
        return 0
    with CALLS.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            race = (row.get("race") or "").strip()
            if race not in model.races:
                print(f"swing_calls.csv: unknown race {race!r}")
                continue
            w = (row.get("winner") or "").strip().upper()
            if w:
                model.call(race, w != "R")
                n += 1
            elif (row.get("margin") or "").strip() and (row.get("frac") or "").strip():
                model.count(race, float(row["margin"]), float(row["frac"]))
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sn", type=float, help="national swing sd in points (default: calibrated to Senate control)")
    ap.add_argument("--ss", type=float, default=3.0, help="state effect sd in points")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--move", type=float, default=0.03, help="--live: outside price moves >= this become constraints")
    ap.add_argument("--flag", type=float, default=0.05, help="--live: show races where model and outside differ by this")
    ap.add_argument("--rehearse", type=int, default=0)
    ap.add_argument("--true-sn", type=float)
    ap.add_argument("--fixed", action="store_true", help="one national sd (--sn / calibrated) instead of the mixture")
    a = ap.parse_args()

    if a.live:
        races, chamber0, b = load_baseline()
        print(f"baseline {b['saved']} (sn {b['sn']}), {len(races)} races")
        sn = a.sn or b["sn"]
    else:
        races, chamber0 = prices()
        sen_names, need = senate(races)
        target = chamber0.get("senate")
        if a.sn:
            sn = a.sn
        elif target is not None:
            sn, rows = calibrate_sn(races, sen_names, need, target, ss=a.ss)
            print(f"Senate control (D needs {need} of {len(sen_names)}): Kalshi/Poly {target:.3f}; model prior by "
                  f"national sd: " + ", ".join(f"{s:g}->{p:.3f}" for s, p in rows) + f"  => sn {sn:g}")
        else:
            sn = 3.0
            print("no Senate-control price; sn 3.0")
    sen_names, need = senate(races)
    model = SwingModel(races, sn=sn, ss=a.ss) if a.fixed else SwingMixture(races, ss=a.ss)

    if a.save_baseline:
        BASELINE.write_text(json.dumps({"saved": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "sn": sn,
                                        "ss": a.ss, "chamber": chamber0, "races": [asdict(r) for r in races]},
                                       indent=1), encoding="utf-8")
        print(f"saved {BASELINE.name}: {len(races)} races, sn {sn:g}")

    if a.rehearse:
        print(f"\nRehearsal: {a.rehearse} simulated nights, model {f'sn {sn:g}' if a.fixed else 'mixture'}, "
              f"truth sn {a.true_sn or sn:g}, ss {a.ss:g}. "
              "Brier on races not yet called (lower is better).")
        print("  UTC 4 Nov | open races | prices frozen | swing model | improvement | Senate control Brier prior -> model")
        for r in rehearse(races, a.rehearse, model, a.true_sn or sn, a.ss, senate=(sen_names, need)):
            h = r["hours"]
            print(f"  {int(h):02d}:{int(h % 1 * 60):02d}Z     | {r['open']:10.1f} | {r['brier_prior']:13.4f} | "
                  f"{r['brier_model']:11.4f} | {1 - r['brier_model'] / r['brier_prior']:10.1%} | "
                  f"{r['senate_prior']:.3f} -> {r['senate_model']:.3f}")
        return

    if a.live:
        now, chamber = prices()
        cur = {r.name: r.p for r in now}
        n_calls = read_calls(model)
        moved = [n for n, p in cur.items() if n in model.races and n not in model.obs and abs(p - model.races[n].p) >= a.move]
        for n in moved:
            model.market(n, cur[n])
        post = model.posterior()
        mN, sN = post["N"]
        print(f"{n_calls} calls/counts, {len(moved)} market moves >= {a.move}; national swing {mN:+.1f} +/- {sN:.1f} pts "
              f"(anti-R); market fit error {post['fit']:.4f}; swing-size weights {post.get('w', sn)}")
        print(f"Senate control: model {model.seats(sen_names, need):.3f}  outside now {chamber.get('senate')}  "
              f"baseline {chamber0.get('senate')}")
        rows = sorted(((post["p"][n] - cur.get(n, model.races[n].p), n) for n in model.races if n not in model.obs),
                      key=lambda x: -abs(x[0]))
        print(f"\nUncalled races where the model and the outside price differ by >= {a.flag} (P anti-R):")
        print("  race                         closes  baseline  outside   model    diff  exchanges")
        for d, n in rows:
            if abs(d) < a.flag:
                break
            r = model.races[n]
            close = POLL_CLOSE_H.get(r.state)
            print(f"  {n:<28} {'' if close is None else f'{int(close):02d}:{int(close % 1 * 60):02d}Z':>6} "
                  f"{r.p:9.3f} {cur.get(n, float('nan')):8.3f} {post['p'][n]:7.3f} {d:+7.3f}  {r.exchanges}")
        return

    post = model.posterior()
    print(f"\n{len(races)} races, national sd {f'{sn:g}' if a.fixed else 'mixture 1.5-6'}, state sd {a.ss:g}; prior Senate control {model.seats(sen_names, need):.3f}")
    print("What one early call does (P anti-R before -> after a surprise in the first states to close):")
    early = sorted((r for r in races if POLL_CLOSE_H.get(r.state, 9) <= 0.5 and 0.15 < r.p < 0.85),
                   key=lambda r: abs(r.p - 0.5))
    for r in early[:3]:
        for won in (True, False):
            model.clear()
            model.call(r.name, won)
            post = model.posterior()
            p = post["p"]
            shift = sorted(((p[n] - model.races[n].p, n) for n in p if n != r.name), key=lambda x: -abs(x[0]))[:5]
            print(f"  {r.name} ({r.p:.2f}) -> {'anti-R' if won else 'R'}: Senate {model.seats(sen_names, need):.3f} "
                  f"(weights {post.get('w', sn)}); "
                  + ", ".join(f"{n} {d:+.3f}" for d, n in shift))


if __name__ == "__main__":
    main()
