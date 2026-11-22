"""Simulated election nights for the night engine (sigbot/night.py). OFFLINE: no SIG calls, sends nothing.

  python sim_election.py --nights 20                     # priors from today's Kalshi/Polymarket (2 public reads)
  python sim_election.py --nights 20 --races-cache f.json   # reuse saved priors (written on first use)
  python sim_election.py --nights 20 --no-bold --third 850000 --start-cash 220000

Each night: truth drawn from the swing model (national swing --true-sn, state and race noise); races are called as
in sigbot/swing.rehearse (at the state's poll close if the margin is > 8 pts, +1 h if 4-8, +3 h if 2-4, +6 h
closer). The outside venues show the swing posterior given the calls so far (every --checkpoint-min), jumping to
0.995 at a race's call. SIG's book follows the outside price --sig-lag-min late, with long shots --premium dear,
--depth shares per level; before the engine acts each tick, rival bots take --rivals of every level >= 0.03
through the current outside price. Players buy called winners at up to 0.995 (--winner-buys shares per tick),
which fills our recycle exits. Positions settle on the truth at 17:00Z.

Reported per night: final balance, takes and edge per role, and with a target (--third) P(final >= the bar) with
and without the bold overlay.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import numpy as np

from sigbot.calls import NIGHT0, RaceState
from sigbot.night import Engine, Params, ceil_tick, floor_tick
from sigbot.swing import POLL_CLOSE_H, Race, SwingMixture, SwingModel

TICK = 0.005


def load_races(cache: str | None) -> tuple[list[Race], dict]:
    if cache:
        try:
            d = json.load(open(cache, encoding="utf-8"))
            return [Race(**r) for r in d["races"]], d["chamber"]
        except FileNotFoundError:
            pass
    from swing import prices
    races, chamber = prices()
    if cache:
        json.dump({"races": [asdict(r) for r in races], "chamber": chamber}, open(cache, "w", encoding="utf-8"))
    return races, chamber


class World:
    """One simulated night: truth, outside prices, SIG books, our account."""

    def __init__(self, races: list[Race], a, rng: np.random.Generator, senate_ex=("842", "843")):
        self.a, self.rng = a, rng
        self.races = {r.name: r for r in races if r.exchanges}
        truth = SwingModel(races, sn=a.true_sn)
        N = rng.normal(0, a.true_sn)
        S = {st: rng.normal(0, 3.0) for st in sorted({r.state for r in races})}
        self.margin = {n: truth.mu[n] + N + S[r.state] + rng.normal(0, truth.sri[n]) for n, r in truth.races.items()}
        self.call_at = {}
        for n, r in truth.races.items():
            m = abs(self.margin[n])
            h = POLL_CLOSE_H.get(r.state, 2.0) + (0 if m > 8 else 1 if m > 4 else 3 if m > 2 else 6)
            self.call_at[n] = NIGHT0 + timedelta(hours=h, minutes=float(rng.uniform(0, 20)))
        self.is_d = {n: rng.random() < r.share_d for n, r in self.races.items()}
        sen = [n for n, r in truth.races.items() if r.rtype == "Senate"]
        d_seats = sum(1 for n in sen if self.margin[n] > 0 and (self.is_d.get(n, True)))
        self.senate_d = d_seats >= 17
        self.senate_ex = senate_ex
        self.info = SwingModel(races, sn=a.true_sn)     # what the outside venues know
        self.outside: dict[str, float] = {}
        self.hist: list[tuple] = []                      # (time, {ex: p}) for SIG's lag
        self.books: dict[str, dict] = {}
        self.cash = a.start_cash
        self.pos: dict[str, float] = defaultdict(float)   # ex -> YES shares (negative = NO)
        self.cost: dict[str, float] = defaultdict(float)
        self.exits: list[dict] = []
        self.stats = defaultdict(float)
        self.used: dict[tuple, float] = defaultdict(float)   # (ex, "a"/"b", YES price) -> shares taken, see --refill
        self.hold_const = 0.0                               # holds the sim can't settle (exchange not modelled)

    def yes_pays(self, ex: str) -> bool:
        if ex in self.senate_ex:
            return self.senate_d if ex == self.senate_ex[0] else not self.senate_d
        for n, r in self.races.items():
            for party, e in r.exchanges.items():
                if str(e) == ex:
                    anti = self.margin[n] > 0
                    return {"R": not anti, "D": anti and self.is_d[n], "I": anti and not self.is_d[n]}[party]
        return False

    def update_outside(self, now: datetime, checkpoint: bool):
        if checkpoint:
            self.info.clear()
            for n in self.info.races:
                if self.call_at[n] <= now:
                    self.info.call(n, self.margin[n] > 0)
            post = self.info.posterior()["p"]
            sen = [n for n, r in self.info.races.items() if r.rtype == "Senate"]
            sen_p = self.info.seats(sen, 17)
            self.outside = {}
            for n, r in self.races.items():
                pa = post[n]
                for party, ex in r.exchanges.items():
                    self.outside[str(ex)] = {"R": 1 - pa, "D": pa * r.share_d, "I": pa * (1 - r.share_d)}[party]
            self.outside[self.senate_ex[0]], self.outside[self.senate_ex[1]] = sen_p, 1 - sen_p
        for n, r in self.races.items():      # a call shows on the venues at once
            if self.call_at[n] <= now:
                anti = self.margin[n] > 0
                for party, ex in r.exchanges.items():
                    won = {"R": not anti, "D": anti and self.is_d[n], "I": anti and not self.is_d[n]}[party]
                    self.outside[str(ex)] = 0.995 if won else 0.005
        self.hist.append((now, dict(self.outside)))

    def rebuild_books(self, now: datetime):
        """SIG players quote around the outside price of --sig-lag-min ago, long shots dear."""
        lagged = self.hist[0][1]
        for t, snap in self.hist:
            if t <= now - timedelta(minutes=self.a.sig_lag_min):
                lagged = snap
        # --refill: share of the depth taken (by rivals or us) that SIG's players put back each tick. 1 = every tick
        # rebuilds a full book (the original sim); lower = a stale level, once swept, stays mostly gone.
        if self.a.refill >= 1:
            self.used.clear()
        else:
            for k in list(self.used):
                self.used[k] *= 1 - self.a.refill
                if self.used[k] < 1:
                    del self.used[k]
        cm = self.a.called_mid
        for ex, p in lagged.items():
            if cm and p >= 0.99:         # a called winner: players pay up to --called-mid, not the favourite discount
                mid = cm
            elif cm and p <= 0.01:
                mid = 1 - cm
            else:
                mid = min(max(p + (self.a.premium if p < 0.15 else -self.a.premium if p > 0.85 else 0), 0.01), 0.99)
            bid, ask = floor_tick(mid - 0.005), ceil_tick(mid + 0.005)
            d = self.a.depth
            bids = [round(bid - i * TICK, 3) for i in range(6) if bid - i * TICK > 0]
            asks = [round(ask + i * TICK, 3) for i in range(6) if ask + i * TICK < 1]
            self.books[ex] = {"bids": [(x, max(0.0, d - self.used.get((ex, "b", x), 0.0))) for x in bids],
                              "asks": [(x, max(0.0, d - self.used.get((ex, "a", x), 0.0))) for x in asks]}
        # rival bots take mispriced levels first
        for ex, b in self.books.items():
            f = self.outside.get(ex)
            if f is None:
                continue
            for kind, key, hit in (("asks", "a", lambda x: x <= f - 0.03), ("bids", "b", lambda x: x >= f + 0.03)):
                new = []
                for x, q in b[kind]:
                    if hit(x):
                        self.used[(ex, key, x)] += q * self.a.rivals
                        q *= 1 - self.a.rivals
                    new.append((x, q))
                b[kind] = new

    def winner_buys(self):
        """Players buy called winners at up to 0.995: our exits (NO bids at 0.01 on a YES winner) fill."""
        left = []
        for o in self.exits:
            held = self.pos[o["ex"]] if o["side"] == "no" else -self.pos[o["ex"]]
            q = min(o["qty"], self.a.winner_buys, max(0.0, held))
            self.trade(o["ex"], o["side"], o["price"], q, "X")
            o["qty"] -= q
            if o["qty"] > 0:
                left.append(o)
        self.exits = left

    def trade(self, ex: str, side: str, px: float, q: float, role: str):
        if q <= 0:
            return
        sign = 1 if side == "yes" else -1
        held = self.pos[ex]
        self.cash -= q * px
        if held * sign < 0:              # netting: a pair pays 1 at once
            pairs = min(abs(held), q)
            self.cash += pairs
            avg = self.cost[ex] / abs(held) if held else 0
            self.cost[ex] -= avg * pairs
            self.stats[f"realised_{role}"] += pairs * (1 - px - avg)
            self.pos[ex] += sign * pairs
            q -= pairs
        self.pos[ex] += sign * q
        self.cost[ex] += q * px
        self.stats[f"shares_{role}"] += q

    def settle(self) -> float:
        total = self.cash + self.hold_const
        for ex, q in self.pos.items():
            y = self.yes_pays(ex)
            total += q if (q > 0 and y) else (-q if (q < 0 and not y) else 0)
        return total


class FakeVenue:
    def __init__(self, w: World, third: float | None):
        self.w, self._third = w, third

    def books(self):
        return self.w.books

    def account(self):
        pos = {}
        for ex, q in self.w.pos.items():
            if abs(q) >= 1:
                pos[ex] = {"side": "yes" if q > 0 else "no", "qty": abs(q), "avg": self.w.cost[ex] / abs(q)}
        return self.w.cash, pos

    def open_orders(self):
        return [{"id": i, "exchangeId": o["ex"]} for i, o in enumerate(self.w.exits)]

    def take(self, orders, expiry_s):
        out = []
        for o in orders:
            b = self.w.books.get(o["ex"])
            if not b:
                continue
            levels = b["asks"] if o["side"] == "yes" else [(round(1 - p, 3), q) for p, q in b["bids"]]
            want, filled, cost = o["qty"], 0.0, 0.0
            new = []
            for px, q in levels:
                if px <= o["price"] + 1e-9 and want > 0 and self.w.cash - cost > px:
                    t = min(q, want, (self.w.cash - cost) / px)
                    filled += t
                    cost += t * px
                    want -= t
                    self.w.trade(o["ex"], o["side"], px, t, o["role"])
                    self.w.used[(o["ex"], "a", px) if o["side"] == "yes" else (o["ex"], "b", round(1 - px, 3))] += t
                    q -= t
                new.append((px, q))
            if o["side"] == "yes":
                b["asks"] = [(p, q) for p, q in new if q > 0]
            else:
                b["bids"] = [(round(1 - p, 3), q) for p, q in new if q > 0]
            out.append({"ex": o["ex"], "side": o["side"], "filled": filled, "cost": cost, "role": o["role"]})
        return out

    def rest(self, orders, expiry_s):
        # A re-offer replaces the exchange's previous exit (live exits expire after exit_expiry_s), so exits never
        # stack up and oversell the position.
        new = {o["ex"] for o in orders}
        self.w.exits = [o for o in self.w.exits if o["ex"] not in new] + [dict(o) for o in orders]
        return []

    def cancel(self, ids):
        return 0

    def cancel_all(self):
        return True

    def third_balance(self):
        return self._third


class FakeOutside:
    def __init__(self, w: World, rng):
        self.w, self.rng = w, rng

    def quotes(self):
        out = {}
        for ex, p in self.w.outside.items():
            k = min(max(p + self.rng.normal(0, 0.003), 0.001), 0.999)
            pm = min(max(p + self.rng.normal(0, 0.005), 0.001), 0.999)
            out[ex] = {"kalshi": k, "polymarket": pm, "fair": {"p": (k + pm) / 2, "buffer": 0.005}}
        return out


def race_states(races: list[Race], senate_ex=("842", "843")) -> dict[str, RaceState]:
    out = {r.name: RaceState(r.name, r.state, {k: str(v) for k, v in r.exchanges.items()}) for r in races if r.exchanges}
    out["U.S. Senate"] = RaceState("U.S. Senate", None, {"D": senate_ex[0], "R": senate_ex[1]}, chamber=True)
    return out


def markets_of(states: dict[str, RaceState]) -> dict[str, dict]:
    return {str(ex): {"party": party, "race": r.race} for r in states.values() for party, ex in r.exchanges.items()}


def seed_holds(w: World, path: str) -> list[str]:
    """The account's holds (GET portfolio/positions, saved as JSON) at the start of the night. Exchanges the sim
    models settle on its truth; the rest count at their current market value."""
    known = {str(e) for r in w.races.values() for e in r.exchanges.values()} | set(w.senate_ex)
    out = []
    for h in json.load(open(path, encoding="utf-8")):
        if h.get("settled") or not h.get("quantity"):
            continue
        ex, q = str(h["exchangeId"]), float(h["quantity"])
        if ex in known:
            w.pos[ex] += q
            w.cost[ex] += float(h.get("costBasis") or 0)
            out.append(ex)
        else:
            w.hold_const += float(h.get("marketValue") or 0)
    return out


def liquidate(w: World, holds: list[str], cost: float):
    """--liquidate: every hold sold before the night at the outside fair less `cost` per share (maker.py
    --night-sell-all; busy-prep sales cost ~4.8c/share at fair on 25 Oct)."""
    for ex in holds:
        q = w.pos[ex]
        f = w.outside.get(ex)
        if not q or f is None:
            continue
        fs = f if q > 0 else 1 - f
        w.cash += abs(q) * max(0.0, fs - cost)
        w.stats["liquidated"] += abs(q)
        w.pos[ex], w.cost[ex] = 0.0, 0.0
    w.cash += w.hold_const * (1 - cost)
    w.hold_const = 0.0


def night(races, a, seed: int, bold: bool) -> dict:
    rng = np.random.default_rng(seed)
    w = World(races, a, rng)
    states = race_states(races)
    p = Params()
    p.model_every_s = a.checkpoint_min * 60
    p.race_cap, p.race_cap_call = a.race_cap, a.race_cap_call
    p.bold_every_min, p.bold_max = a.bold_every, a.bold_max
    p.leader_gain = (0.0, a.leader_gain_hi)
    p.race_cap_sure, p.lock_keep = a.race_cap_sure, a.lock_keep
    p.recycle_always, p.recycle_bid_min, p.recycle_take_gain = float(a.recycle_always), a.recycle_bid_min, a.recycle_take_gain
    t0 = NIGHT0 - timedelta(hours=2, minutes=20)
    p.bold_times = tuple(NIGHT0 + timedelta(minutes=float(m)) for m in a.bold_at.split(",")) if bold else ()
    p.bold_adaptive = 0.0 if a.bold_fixed else 1.0
    if not bold:
        p.bold_every_min, p.bold_on = 1e9, False
    model = SwingMixture(races)
    # The detector's 60 s / 300 s holds are shorter than a sim tick: calls land on the tick (holds = 0), otherwise
    # every call would be delayed by a whole tick, longer than SIG's lag itself.
    eng = Engine(FakeVenue(w, a.third), FakeOutside(w, rng), markets_of(states), states, p, model=model,
                 swing_races={r.name: r for r in races}, log=lambda e: None, senate_ex=("842", "843"),
                 detector_kw={"hold_s": 0, "early_hold_s": 0, "single_hold_s": 0})
    end = NIGHT0 + timedelta(hours=17)
    holds = seed_holds(w, a.holds) if a.holds else []
    now, k = t0, 0
    while now < end:
        w.update_outside(now, checkpoint=k % max(1, int(a.checkpoint_min / a.tick_min)) == 0)
        if k == 0 and a.liquidate:
            liquidate(w, holds, a.liquidate_cost)
        w.rebuild_books(now)
        w.winner_buys()
        eng.step(now)
        now += timedelta(minutes=a.tick_min)
        k += 1
    final = w.settle()
    return {"seed": seed, "final": final, "stats": dict(w.stats), "edge": dict(eng.stats),
            "calls": sum(1 for r in eng.races.values() if r.status == "CALLED"), "senate_d": w.senate_d}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nights", type=int, default=10)
    ap.add_argument("--races-cache")
    ap.add_argument("--start-cash", type=float, default=220000)
    ap.add_argument("--third", type=float, default=850000, help="3rd-place balance today (bold target)")
    ap.add_argument("--no-bold", action="store_true")
    ap.add_argument("--bold-fixed", action="store_true",
                    help="bold as before 31 Oct: the fixed night_gain prior and plans at --bold-at, not waiting for calls")
    ap.add_argument("--compare", action="store_true", help="each night with and without the bold overlay")
    ap.add_argument("--true-sn", type=float, default=3.0)
    ap.add_argument("--sig-lag-min", type=float, default=5)
    ap.add_argument("--premium", type=float, default=0.03)
    ap.add_argument("--depth", type=float, default=3000)
    ap.add_argument("--rivals", type=float, default=0.7, help="share of each mispriced level rivals take first")
    ap.add_argument("--winner-buys", type=float, default=2000)
    ap.add_argument("--tick-min", type=float, default=1.0)
    ap.add_argument("--checkpoint-min", type=float, default=10.0)
    p0 = Params()
    ap.add_argument("--race-cap", type=float, default=p0.race_cap)
    ap.add_argument("--race-cap-call", type=float, default=p0.race_cap_call)
    ap.add_argument("--bold-at", default="-30", help="bold decisions, minutes after 00:00Z 4 Nov (comma-separated; "
                                                       "the sim starts at -140)")
    ap.add_argument("--bold-every", type=float, default=p0.bold_every_min)
    ap.add_argument("--bold-max", type=float, default=p0.bold_max)
    ap.add_argument("--leader-gain-hi", type=float, default=p0.leader_gain[1])
    ap.add_argument("--seed0", type=int, default=0)
    ap.add_argument("--race-cap-sure", type=float, default=p0.race_cap_sure)
    ap.add_argument("--lock-keep", type=float, default=p0.lock_keep)
    ap.add_argument("--recycle-always", action="store_true")
    ap.add_argument("--recycle-bid-min", type=float, default=p0.recycle_bid_min)
    ap.add_argument("--recycle-take-gain", type=float, default=p0.recycle_take_gain)
    ap.add_argument("--refill", type=float, default=1.0,
                    help="share of swept depth SIG's players put back per tick (1 = full book every tick)")
    ap.add_argument("--called-mid", type=float, default=0.0,
                    help="SIG mid of a called winner once the lag passes (0 = the favourite discount, ~0.965)")
    ap.add_argument("--holds", help="positions JSON (GET portfolio/positions): held into the night")
    ap.add_argument("--liquidate", action="store_true", help="sell the holds before the night (maker --night-sell-all)")
    ap.add_argument("--liquidate-cost", type=float, default=0.048)
    return ap


def main():
    a = build_parser().parse_args()
    races, _ = load_races(a.races_cache)
    modes = [("edge only", False), ("with bold", True)] if a.compare else [("bold" if not a.no_bold else "edge only",
                                                                            not a.no_bold)]
    for label, bold in modes:
        res = [night(races, a, s, bold) for s in range(a.seed0, a.seed0 + a.nights)]
        f = np.array([r["final"] for r in res])
        edge = defaultdict(float)
        for r in res:
            for k, v in r["edge"].items():
                edge[k] += v / len(res)
        print(f"\n{label}: {a.nights} nights, start {a.start_cash:,.0f}, bar {a.third:,.0f}")
        print(f"  final balance: mean {f.mean():,.0f}  median {np.median(f):,.0f}  p10 {np.percentile(f, 10):,.0f}  "
              f"p90 {np.percentile(f, 90):,.0f}  max {f.max():,.0f}")
        print(f"  P(final >= bar) {np.mean(f >= a.third):.3f}   P(final >= start) {np.mean(f >= a.start_cash):.3f}")
        print("  edge at fill per night by role: " + ", ".join(f"{k} {v:,.0f}" for k, v in sorted(edge.items())))
        for r in res[:5]:
            print(f"   seed {r['seed']}: final {r['final']:,.0f}, {r['calls']} calls, Senate D {r['senate_d']}")


if __name__ == "__main__":
    main()
