"""Markets settled by structure (a party with no candidate on the ballot, an uncontested seat). READ-ONLY.

  python research/structural_audit.py            # ~120 Kalshi + 5 Gamma requests, 3 SIG reads (top of book)

SIG's market objects carry no rules or candidate text (checked 12 Oct: title, status, settlementDate only), so the
evidence is the outside venues:
  * no_market   Kalshi's event for the race has no market for that party AND Polymarket has none mapped
                (Kalshi lists every party with a candidate on the ballot, e.g. NE Senate: R + Osborn (I), no D)
  * near_zero   both venues price the party at <= 0.01 (a nominal candidate, or a top-two race without one)
Each candidate is printed with SIG's book so the gap is visible. NOTHING is written to config/structural.json
unless --write is given, and only the exchanges listed with --confirm (after checking the ballot by hand, e.g. the
state's certified candidate list or Ballotpedia): a wrong entry would make the bot buy the wrong side as "certain".
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import json
from collections import defaultdict

from sigbot.client import ROOT, SigClient
from sigbot.kalshi import Kalshi, event_ticker
from sigbot.markets import parse_title
from sigbot.polymarket import Polymarket

UNIVERSE = ROOT / "config" / "universe.json"
OUT = ROOT / "config" / "structural.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--confirm", default="", help="comma-separated exchange ids checked by hand (with --write)")
    a = ap.parse_args()
    u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    by_race = defaultdict(dict)
    rtype, title = {}, {}
    for m in u["markets"]:
        race, rt, party = parse_title(m["title"])
        ex = str(m["exchanges"][0]["id"])
        by_race[race][party] = ex
        rtype[race], title[ex] = rt, m["title"]
    k, poly = Kalshi(), Polymarket()
    pq = poly.quotes(sorted(set(u["polymarket"].values())))
    c = SigClient(log=False)
    ids = sorted(title)
    tops = {}
    for i in range(0, len(ids), 100):
        r = c.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=u["tournamentId"])
        for x in (r.get("data") if isinstance(r, dict) else r) or []:
            tops[str(x.get("exchangeId"))] = (x.get("bestBid"), x.get("bestAsk"))
    found = []
    for race, exs in sorted(by_race.items()):
        et = event_ticker(race, rtype[race])
        try:
            kq = k.party_quotes(et) if et else {}
        except Exception:  # noqa: BLE001
            kq = {}
        for party, ex in exs.items():
            kp = kq.get(party)
            kv = None if kp is None else (kp.get("mid") if kp.get("mid") is not None else kp.get("last"))
            pid = u["polymarket"].get(ex)
            pv = (pq.get(pid) or {}).get("mid") if pid else None
            why = None
            if et and kq and kp is None and pid is None:
                why = "no_market"
            elif kv is not None and kv <= 0.01 and (pv is None or pv <= 0.01):
                why = "near_zero"
            if why:
                found.append((ex, why, race, party, kv, pv, tops.get(ex)))
    print(f"{len(found)} candidates (check every one by hand before --confirm):")
    print("  exchange  why         party race                          kalshi   poly     SIG bid/ask")
    for ex, why, race, party, kv, pv, top in found:
        print(f"  {ex:>8}  {why:<11} {party:<5} {race:<29} {kv if kv is not None else '-':<8} "
              f"{pv if pv is not None else '-':<8} {top}")
    if a.write:
        ok = {x.strip() for x in a.confirm.split(",") if x.strip()}
        rows = {ex: {"yes": 0.0, "why": why, "race": race, "party": party}
                for ex, why, race, party, *_ in found if ex in ok}
        OUT.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"wrote {OUT.name}: {len(rows)} markets settled NO by structure")


if __name__ == "__main__":
    main()
