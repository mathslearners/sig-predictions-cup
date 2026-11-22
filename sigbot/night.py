"""Election-night decision engine (pure logic; election.py supplies the live SIG venue, sim_election.py a fake one).

Every step (~2 s): outside prices -> race calls (sigbot/calls.py) -> fair per exchange -> takes, in priority order:
  C  call sweep   race called: buy the winner's YES and every loser's NO on every SIG level up to conf - margin
  L  lag snipe    SIG offers >= lag_edge (+ buffer) through a trusted outside fair (venues agree, or one venue + model)
  H  chamber      Senate/House control vs the outside price blended with the swing model's seat count
  P  pairs        two-party race: YES asks D + R < 1 - pair_edge, or NO asks D + R < 1 - pair_edge (pays 1 at
                  settlement whoever wins; the cash is locked until certification, hence the 0.03 default edge)
  B  bold         the gap-rule overlay (sigbot/bold.py) at the scheduled times and after each wave of calls
plus the recycle exit (X: offer called winners at exit_px when cash runs short) and risk-off (cancel our resting
orders on a state minutes before its polls close; nothing of ours rests on an open race after that but X exits).
Fully automatic: no human input is needed (the STOP file / SIGBOT_DISABLED stay as the only kill switch).

Venue interface (live: election.py SigVenue; sim: sim_election.FakeVenue):
  books() -> {ex: {"bids": [(p, q)], "asks": [(p, q)]}}    YES prices, best first
  account() -> (cash, {ex: {"side", "qty", "avg"}})
  open_orders() -> [{"id", "exchangeId", "side", "price", ...}]
  take(orders, expiry_s) -> [{"ex", "side", "filled", "cost"}]      orders: {ex, side, price (limit), qty, role}
  rest(orders, expiry_s) -> number placed
  cancel(order_ids) -> number cancelled;  cancel_all() -> bool
  third_balance() -> float | None                                  3rd-place balance on the all-time board
Outside interface: quotes() -> {ex: {"kalshi": p|None, "polymarket": p|None, "fair": {"p", "buffer"}|None}}
Desk interface (optional, election.py --desk-calls): calls() -> {race: {source: party|None}} (sigbot/desk.py)
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np

from sigbot.bold import (leader_targets, lots_from_books, night_gain_draws, optimise, outcome_table,
                         target_draws)
from sigbot.calls import NIGHT0, CallDetector, RaceState

TICK = 0.005
CLOSE = NIGHT0 + timedelta(hours=17)      # trading closes 4 Nov 17:00Z
SPEED_ROLES = ("C", "L", "H", "P")        # the roles whose realised edge the adaptive bold measures


def floor_tick(p: float) -> float:
    return round(math.floor(round(p / TICK, 6)) * TICK, 3)


def ceil_tick(p: float) -> float:
    return round(math.ceil(round(p / TICK, 6)) * TICK, 3)


class Params:
    """Defaults; election.py exposes each as a --flag."""
    call_margin_hi = 0.02        # conf >= 0.99: buy up to 0.97 for a 0.99 winner
    call_margin_lo = 0.05
    lag_edge = 0.04
    model_edge = 0.08            # races priced only by the model
    chamber_edge = 0.05
    pair_edge = 0.03             # NO+NO pairs lock cash for weeks: worth it only well under 1
    agree = 0.05                 # Kalshi and Polymarket within this = trusted outside fair
    model_agree = 0.07           # one venue + the model within this = trusted
    race_cap_call = 0.25         # share of bankroll per race on a call >= 0.99 (a 0.99 race still loses 1 in 100:
                                 # a 60% cap lost 440k on one race in a simulated night)
    race_cap = 0.10              # per race otherwise (both caps include the cost already held in the race)
    race_cap_sure = 0.25         # per race while BOTH venues hold the winner >= 0.99 (= race_cap_call: off; 26 Oct
                                 # test: 0.5, since far behind the bar a wrong call costs little P(top 3))
    pending_s = 30.0             # recent fills count toward the race cap this long (the account snapshot lags them)
    reserve = 1000.0
    lock_keep = 0.5              # L/P takes (cash locked until settlement) only spend cash above this share of the
                                 # bankroll, so call sweeps (C) and chamber trades (H) always find cash on the night
    lock_relax = (4.0, 10.0)     # ... falling linearly to 0 between these hours after 00:00Z 4 Nov: once most races
                                 # are called idle cash is worth nothing at settlement, so it goes to the open races
    structural_edge = 0.02       # markets settled by structure (no candidate): buy the certain side up to 0.98
    min_take = 50.0              # SUSQies: smaller opportunities are not worth a write
    take_expiry_s = 60
    retake_s = 15.0              # no second take on the same side at the same or a lower limit within this
    exit_px = 0.99               # recycle: offer a called winner at this
    recycle_below = 20000.0      # ... while free cash is under this and better uses are waiting, or whenever takes
    recycle_window_s = 300.0     # were blocked by cash within this many seconds
    exit_px_late = 0.995         # races called >= 1 h ago: late buyers pay up to 0.995
    exit_expiry_s = 900
    recycle_always = 0.0         # 1: offer every called winner at exit_px all night, not only while cash is short
    recycle_bid_min = 0.0        # > 0: while takes wait for cash, sell called winners into SIG bids >= this (0.975),
    recycle_take_gain = 0.05     # ... only when the waiting takes return >= this per SUSQie (the sale gives up 1-2c)
    risk_off_min = 5.0
    bold_times: tuple = ()       # datetimes for the scheduled bold decisions
    bold_every_min = 30.0        # re-plan after calls during the night
    bold_max = 1.0               # share of cash the overlay may spend (gap rule decides within it)
    bold_draws = 8000
    leader_gain = (0.0, 0.3)     # leaders' own night gain, uniform
    night_gain = (0.15, 0.25)    # mean, sd of the return on cash kept for the speed roles (the prior, see below)
    # Adaptive bold (31 Oct): the fixed prior above decided every plan. Simulated nights with deep stale books
    # (27 Oct) had the speed roles earn several x the cash, and the 22:30Z plan bet it away (median 1.88M -> 1.06M,
    # one night 350k). Now no plan before bold_min_calls race calls, and the return on kept cash is the measured
    # sweep edge per call x the calls still expected tonight / cash, blended with the prior by calls seen.
    bold_on = True
    bold_adaptive = 1.0          # 0 = the fixed night_gain and the scheduled plans as before
    bold_min_calls = 8           # no bold plan before this many race calls (the evidence of what sweeps earn)
    gain_prior_calls = 10.0      # the prior weighs as much as this many measured calls
    gain_rel_sd = 0.5            # sd of the measured projection, relative to it
    gain_rate_h = 2.0            # calls still expected = this window's call rate x hours to the close (<= uncalled)
    model_every_s = 60.0
    model_iters = 20             # I-projection sweeps per refresh (each ~0.1 s with 115 races)
    model_async = False          # live: refresh in a thread so a slow posterior never delays a take
    bold_async = False           # live: plan the bold overlay in a thread too (1-11 s per plan)


class Engine:
    def __init__(self, venue, outside, markets: dict, races: dict[str, RaceState], params: Params,
                 model=None, swing_races: dict | None = None, log=print, notify=lambda s: None,
                 senate_ex: tuple | None = None, house_ex: tuple | None = None, live: bool = True,
                 structural: dict | None = None, leaders: dict | None = None, detector_kw: dict | None = None,
                 desk=None):
        self.v, self.o, self.markets, self.p = venue, outside, markets, params
        self.det = CallDetector(races, **(detector_kw or {}))
        self.desk = desk                          # sigbot/desk.py DeskFeed (election.py --desk-calls) or None
        self.races = races
        self.model, self.swing_races = model, swing_races or {}
        self.log, self.notify, self.live = log, notify, live
        self.senate_ex, self.house_ex = senate_ex, house_ex
        self.ex_race = {str(ex): r.race for r in races.values() for ex in r.exchanges.values()}
        self.cash, self.pos = 0.0, {}
        self.model_p: dict[str, float] = {}       # race -> P(anti-R)
        self.senate_model: float | None = None
        self.model_at = None
        self.risk_off_done: set[str] = set()
        self.exit_ids: set = set()
        self.last_exit: dict[str, datetime] = {}
        self.bold_done: set = set()
        self.last_bold = None
        self.calls_since_bold = 0
        self.stats = defaultdict(float)
        self.blocked_cost = 0.0
        self.blocked_edge = 0.0
        self._ext: dict = {}
        self.recent: dict[tuple, tuple] = {}      # (ex, side) -> (time, limit) of the last take sent
        self.pending: list[tuple] = []            # (time, race, cost) of recent fills, for the race caps
        self.model_lock = threading.Lock()
        self._model_thread = None
        self._bold_thread = None
        self.structural = {str(k): float(v) for k, v in (structural or {}).items()}   # ex -> certain YES payoff
        self.last_blocked: datetime | None = None
        self.leaders = leaders or {}              # name -> {balance, exposures} (trusted leader_exposure.json only)
        self.now: datetime | None = None
        # The night so far, for the adaptive bold: realised edge at fill of the speed roles and the first call time
        # of each race. Carried across runs in the lease file (election.LeaseKeeper), since every run starts empty.
        self.ledger = {"edge": 0.0, "cost": 0.0, "fills": 0, "called": {}}
        self.gain_now: tuple[float, float] | None = None   # (mean, sd) the last bold plan used

    # ------------------------------------------------------------------ fair values
    def refresh_model(self, ext: dict, now: datetime, force: bool = False):
        if self.model is None or (not force and self.model_at and
                                  (now - self.model_at).total_seconds() < self.p.model_every_s):
            return
        self.model_at = now
        if not self.p.model_async:
            self._refresh_model(ext)
            return
        if self._model_thread is None or not self._model_thread.is_alive():
            self._model_thread = threading.Thread(target=self._refresh_model, args=(ext,), daemon=True, name="model")
            self._model_thread.start()

    def _refresh_model(self, ext: dict):
        """Swing posterior given the calls and the outside moves; publishes model_p / senate_model when done."""
        with self.model_lock:
            m = self.model
            m.clear()
            for name, r in list(self.races.items()):
                if name not in m.races:
                    continue
                if r.status == "CALLED":
                    m.call(name, r.winner != "R")
                    continue
                p = self.anti_r_outside(r, ext)
                if p is not None and abs(p - m.races[name].p) >= 0.03:
                    m.market(name, p)
            try:
                post = m.posterior(iters=self.p.model_iters, tol=2e-3)
            except Exception as e:  # noqa: BLE001 - keep the last posterior
                self.log({"kind": "model_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
                return
            sen = [n for n, r in m.races.items() if r.rtype == "Senate"]
            senate = m.seats(sen, 17) if sen else None
        self.model_p, self.senate_model = post["p"], senate

    @staticmethod
    def venue_p(q: dict | None):
        if not q:
            return None
        vals = [v for v in (q.get("kalshi"), q.get("polymarket")) if v is not None]
        return sum(vals) / len(vals) if vals else None

    def anti_r_outside(self, r: RaceState, ext: dict) -> float | None:
        if "R" in r.exchanges:
            p = self.venue_p(ext.get(str(r.exchanges["R"])))
            return None if p is None else 1 - p
        if "D" in r.exchanges:
            return self.venue_p(ext.get(str(r.exchanges["D"])))
        return None

    def model_fair(self, ex: str) -> float | None:
        race = self.ex_race.get(ex)
        sr = self.swing_races.get(race)
        if race not in self.model_p or sr is None:
            return None
        a = self.model_p[race]
        party = self.markets[ex]["party"]
        return {"R": 1 - a, "D": a * sr.share_d, "I": a * (1 - sr.share_d)}.get(party)

    def fair(self, ex: str, ext: dict) -> tuple[float, float, str] | None:
        """(fair YES, extra edge needed, role) or None when nothing trustworthy prices it."""
        if ex in self.structural:   # no candidate / uncontested (config/structural.json): settled by structure
            return self.structural[ex], self.p.structural_edge, "L"
        race = self.ex_race.get(ex)
        r = self.races.get(race)
        if r and r.status == "CALLED":
            won = self.markets[ex]["party"] == r.winner
            conf = r.conf
            margin = self.p.call_margin_hi if conf >= 0.99 else self.p.call_margin_lo
            return (conf if won else 1 - conf), margin, "C"
        q = ext.get(ex) or {}
        k, pm, f = q.get("kalshi"), q.get("polymarket"), q.get("fair")
        mf = self.model_fair(ex)
        chamber = r is not None and r.chamber
        if chamber:
            out = f["p"] if f else self.venue_p(q)
            mod = self.senate_model if (self.senate_ex and ex in self.senate_ex) else None
            if mod is not None and ex == self.senate_ex[1]:
                mod = 1 - mod
            if out is None and mod is None:
                return None
            if out is not None and mod is not None:
                # The venues lead on news, the model on arithmetic: blend, stand back by half the disagreement.
                return (out + mod) / 2, self.p.chamber_edge + abs(out - mod) / 2, "H"
            return (out if out is not None else mod), self.p.chamber_edge + (0 if out is not None else 0.03), "H"
        if f is None:
            if mf is not None:
                return mf, self.p.model_edge, "L"
            return None
        buf = f.get("buffer", 0.0)
        if k is not None and pm is not None:
            if abs(k - pm) <= self.p.agree:
                return f["p"], self.p.lag_edge + buf, "L"
            if mf is not None and min(abs(k - mf), abs(pm - mf)) <= self.p.model_agree:
                near = k if abs(k - mf) <= abs(pm - mf) else pm
                return near, self.p.lag_edge + abs(k - pm) / 2, "L"
            return None
        if mf is not None and abs(f["p"] - mf) > self.p.model_agree:
            return None
        return f["p"], self.p.lag_edge + buf + 0.01, "L"

    # ------------------------------------------------------------------ opportunities
    @staticmethod
    def offers(book: dict | None, side: str) -> list[tuple]:
        """Offers to buy `side` as (price of that side, qty), cheapest first."""
        if not book:
            return []
        if side == "yes":
            return list(book.get("asks") or [])
        return [(round(1 - p, 3), q) for p, q in book.get("bids") or []]

    def opportunities(self, books: dict, ext: dict) -> list[dict]:
        out = []
        for ex in self.markets:
            fv = self.fair(ex, ext)
            if fv is None:
                continue
            fy, need, role = fv
            for side in ("yes", "no"):
                fs = fy if side == "yes" else 1 - fy
                limit = floor_tick(fs - need)
                if limit < TICK:
                    continue
                qty = cost = edge = 0.0
                for px, q in self.offers(books.get(ex), side):
                    if px > limit + 1e-9:
                        break
                    qty += q
                    cost += q * px
                    edge += q * (fs - px)
                if cost >= self.p.min_take:
                    out.append({"ex": ex, "side": side, "price": limit, "qty": int(qty), "cost": cost,
                                "edge": edge, "role": role, "race": self.ex_race.get(ex), "fair_s": fs})
        out += self.pairs(books)
        return out

    def pairs(self, books: dict) -> list[dict]:
        out = []
        for r in self.races.values():
            if r.chamber or r.status == "CALLED" or set(r.exchanges) != {"D", "R"}:
                continue
            d, rr = str(r.exchanges["D"]), str(r.exchanges["R"])
            for side in ("yes", "no"):
                od, orr = self.offers(books.get(d), side), self.offers(books.get(rr), side)
                if not od or not orr:
                    continue
                (pd, qd), (pr, qr) = od[0], orr[0]
                if pd + pr < 1 - self.p.pair_edge:
                    q = int(min(qd, qr))
                    if q * (pd + pr) >= self.p.min_take:
                        legs = [{"ex": d, "side": side, "price": pd}, {"ex": rr, "side": side, "price": pr}]
                        out.append({"ex": d, "side": side, "price": pd, "qty": q, "cost": q * (pd + pr),
                                    "edge": q * (1 - pd - pr), "role": "P", "race": r.race, "fair_s": None,
                                    "legs": legs})
        return out

    def budget(self, opps: list[dict], now: datetime | None = None) -> list[dict]:
        bank = self.cash + sum(h["qty"] * h.get("avg", 0.5) for h in self.pos.values())
        free = self.cash - self.p.reserve
        prio = {"C": 0, "P": 1, "H": 2, "L": 3}
        opps = sorted(opps, key=lambda o: (prio.get(o["role"], 9), -o["edge"] / max(o["cost"], 1)))
        # The per-race caps count what the race already holds (cost), plus fills the account snapshot may not show
        # yet. Per step alone, repeated sweeps of a deep stale book put the whole bankroll into one race in a minute.
        race_used = defaultdict(float)
        for ex, h in self.pos.items():
            race_used[self.ex_race.get(str(ex))] += h["qty"] * h.get("avg", 0.5)
        if now is not None:
            self.pending = [(t, r, c) for t, r, c in self.pending if (now - t).total_seconds() < self.p.pending_s]
            for _, r, c in self.pending:
                race_used[r] += c
        out = []
        self.blocked_cost = self.blocked_edge = 0.0
        lock_floor = self.lock_keep_now() * bank
        for o in opps:
            last = self.recent.get((o["ex"], o["side"]))
            if last and now is not None and (now - last[0]).total_seconds() < self.p.retake_s and o["price"] <= last[1]:
                continue      # just sent: the book may not show our fill yet (a write takes 5-20 s under load)
            r = self.races.get(o["race"])
            if r is not None and r.status == "CALLED" and r.conf >= 0.99:
                cap = max(self.p.race_cap_call, self.p.race_cap_sure) if self.both_sure(r) else self.p.race_cap_call
            else:
                cap = self.p.race_cap
            room = min(free, cap * bank - race_used[o["race"]])
            if o["role"] in ("L", "P"):
                room = min(room, free + self.p.reserve - lock_floor)
            if room < self.p.min_take:
                self.blocked_cost += o["cost"]
                self.blocked_edge += o["edge"]
                continue
            if o["cost"] > room:
                frac = room / o["cost"]
                self.blocked_cost += o["cost"] - room
                self.blocked_edge += o["edge"] * (1 - frac)
                o = {**o, "qty": int(o["qty"] * frac), "cost": room, "edge": o["edge"] * frac}
            if o["qty"] < 1:
                continue
            free -= o["cost"]
            race_used[o["race"]] += o["cost"]
            for leg in o.get("legs") or [None]:     # a pair is sized as one and sent as two equal legs
                w = o if leg is None else {**o, **leg, "cost": o["qty"] * leg["price"], "edge": o["edge"] / 2}
                w.pop("legs", None)
                out.append(w)
                if now is not None:
                    self.recent[(w["ex"], w["side"])] = (now, w["price"])
        return out

    def both_sure(self, r: RaceState) -> bool:
        """Both venues quote the called winner >= 0.99 right now (race_cap_sure applies)."""
        ex = r.exchanges.get(r.winner) if r.winner else None
        q = self._ext.get(str(ex)) if ex is not None else None
        return bool(q) and all(v is not None and v >= 0.99 for v in (q.get("kalshi"), q.get("polymarket")))

    def lock_keep_now(self) -> float:
        if self.now is None:
            return self.p.lock_keep
        h = (self.now - NIGHT0).total_seconds() / 3600
        a, b = self.p.lock_relax
        return self.p.lock_keep * min(1.0, max(0.0, (b - h) / (b - a)))

    # ------------------------------------------------------------------ actions
    def risk_off(self, now: datetime):
        """Our resting orders on a state are cancelled risk_off_min before its FIRST polls close (results start then;
        stale orders are what the other bots pick off when a race moves or is called). Exits on called races are
        exempt."""
        due = {r.state for r in self.races.values() if not r.chamber and r.state not in self.risk_off_done
               and self.det.results_at(r) - timedelta(minutes=self.p.risk_off_min) <= now}
        if not due:
            return
        exs = {str(ex) for r in self.races.values() if r.state in due for ex in r.exchanges.values()}
        ids = [o["id"] for o in self.v.open_orders() if str(o.get("exchangeId")) in exs and o["id"] not in self.exit_ids]
        n = self.v.cancel(ids) if ids else 0
        self.risk_off_done |= due
        self.log({"kind": "risk_off", "states": sorted(due), "cancelled": n})

    def winners_held(self) -> list[tuple[str, dict]]:
        """(ex, holding) for every holding on the winning side of a race called at conf >= 0.99."""
        out = []
        for ex, h in self.pos.items():
            r = self.races.get(self.ex_race.get(ex))
            if r is None or r.status != "CALLED" or r.conf < 0.99:
                continue
            if (h["side"] == "yes") == (self.markets[ex]["party"] == r.winner):
                out.append((ex, h))
        return out

    def recycle_into_bids(self, books: dict, now: datetime):
        """Cash waits for takes worth >= recycle_take_gain: sell called winners into SIG's bids >= recycle_bid_min
        (buy the opposite side; the pair pays 1 at once), best bid first, only as much as the waiting takes need."""
        need, want = self.blocked_cost, []
        cands = []
        for ex, h in self.winners_held():
            opp = "no" if h["side"] == "yes" else "yes"
            for px, q in self.offers(books.get(ex), opp):
                if 1 - px >= self.p.recycle_bid_min - 1e-9:
                    cands.append((px, ex, opp, q, h))
        held_left = {ex: h["qty"] for ex, h in self.winners_held()}
        for px, ex, opp, q, h in sorted(cands, key=lambda c: c[0]):
            if need < self.p.min_take:
                break
            last = self.recent.get((ex, opp))
            if last and (now - last[0]).total_seconds() < self.p.retake_s:
                continue
            n = int(min(q, held_left[ex], need / max(1 - px, 1e-6)))
            if n < 1:
                continue
            held_left[ex] -= n
            need -= n * (1 - px)
            want.append({"ex": ex, "side": opp, "price": px, "qty": n, "cost": n * px, "edge": 0.0, "role": "X",
                         "race": self.ex_race.get(ex)})
        if not want:
            return
        # one order per exchange at its worst accepted level (a limit sweeps the better levels too)
        merged: dict[tuple, dict] = {}
        for w in want:
            m = merged.get((w["ex"], w["side"]))
            if m is None:
                merged[(w["ex"], w["side"])] = dict(w)
            else:
                m["qty"] += w["qty"]
                m["cost"] += w["cost"]
                m["price"] = max(m["price"], w["price"])
        orders = list(merged.values())
        for o in orders:
            self.recent[(o["ex"], o["side"])] = (now, o["price"])
        fills = self.v.take(orders, self.p.take_expiry_s) if self.live else []
        self.stats["recycle_sold"] += sum(o["qty"] for o in orders)
        self.log({"kind": "recycle_bids", "orders": [{k: o[k] for k in ("ex", "side", "price", "qty")} for o in orders],
                  "need": self.blocked_cost, "fills": fills})

    def recycle(self, now: datetime, books: dict | None = None):
        if self.blocked_cost > 0:
            self.last_blocked = now
        if (self.p.recycle_bid_min > 0 and books and self.blocked_cost >= self.p.min_take
                and self.blocked_edge >= self.p.recycle_take_gain * self.blocked_cost):
            self.recycle_into_bids(books, now)
        recent = self.last_blocked is not None and (now - self.last_blocked).total_seconds() <= self.p.recycle_window_s
        if not self.p.recycle_always and (not recent or (self.cash >= self.p.recycle_below and self.blocked_cost <= 0)):
            return
        want = []
        for ex, h in self.pos.items():
            r = self.races.get(self.ex_race.get(ex))
            if r is None or r.status != "CALLED" or r.conf < 0.99:
                continue
            won_yes = self.markets[ex]["party"] == r.winner
            if (h["side"] == "yes") != won_yes:
                continue      # we hold the losing side: nothing to offer
            last = self.last_exit.get(ex)
            if last and (now - last).total_seconds() < self.p.exit_expiry_s:
                continue
            late = r.called_at and (now - datetime.fromisoformat(r.called_at)).total_seconds() >= 3600
            px = self.p.exit_px_late if late else self.p.exit_px
            want.append({"ex": ex, "side": "no" if h["side"] == "yes" else "yes",
                         "price": max(TICK, floor_tick(1 - px + 1e-9)), "qty": int(h["qty"]), "role": "X"})
            self.last_exit[ex] = now
        if want:
            ids = self.v.rest(want, self.p.exit_expiry_s)
            if isinstance(ids, (list, set)):
                self.exit_ids |= set(ids)
            self.stats["recycle_offered"] += sum(w["qty"] for w in want)
            self.log({"kind": "recycle", "orders": len(want), "cash": self.cash})

    def bold(self, books: dict, now: datetime, why: str):
        """Live (bold_async): planned in a thread. A plan takes 1-11 s and lands right after a wave of calls, when
        the call sweeps must not wait for it."""
        if self.model is None:
            return
        if not self.p.bold_async:
            self.last_bold, self.calls_since_bold = now, 0   # also when the plan is skipped (no leaderboard)
            self._bold(books, now, why)
            return
        if self._bold_thread is not None and self._bold_thread.is_alive():
            self.log({"kind": "bold_skip", "why": f"previous plan still running ({why})"})
            return
        self.last_bold, self.calls_since_bold = now, 0
        self._bold_thread = threading.Thread(target=self._bold_safe, args=(books, now, why), daemon=True, name="bold")
        self._bold_thread.start()

    def _bold_safe(self, books: dict, now: datetime, why: str):
        try:
            self._bold(books, now, why)
        except Exception as e:  # noqa: BLE001 - a failed plan must not take the thread's error into the night
            self.log({"kind": "bold_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def _bold(self, books: dict, now: datetime, why: str):
        third = self.v.third_balance()
        if not third:
            self.log({"kind": "bold_skip", "why": "no leaderboard"})
            return
        rng = np.random.default_rng(int(now.timestamp()))
        n = self.p.bold_draws
        with self.model_lock:
            names, anti, nat = self.model.sample(n, rng)
        house_p = None
        if self.house_ex:
            house_p = self.venue_p((self._ext or {}).get(str(self.house_ex[0])))
        pay = outcome_table(self.swing_races, names, anti, nat, house_p, self.senate_ex, self.house_ex, rng)
        for name, r in self.races.items():   # called races pay as called
            if r.status == "CALLED":
                for party, ex in r.exchanges.items():
                    pay[str(ex)] = np.full(n, party == r.winner)
        T = None
        if self.leaders:   # trusted leader exposures (research/leader_exposure.py): beat the leaders in each outcome
            mids = {ex: (b["bids"][0][0] + b["asks"][0][0]) / 2 for ex, b in books.items()
                    if b.get("bids") and b.get("asks")}
            T = leader_targets(self.leaders, pay, mids, n, rng, 3, *self.p.leader_gain)
        if T is None:
            T = target_draws(third, n, rng, *self.p.leader_gain)
        gm, gs = self.night_gain_now(now)
        self.gain_now = (gm, gs)
        G = night_gain_draws(n, rng, gm, gs)
        lots = lots_from_books(books, [ex for ex in pay if ex in self.markets])
        res = optimise(self.cash - self.p.reserve, self.pos, lots, pay, T, G, max_frac=self.p.bold_max)
        self.stats["bold_spent"] += res["spent"]
        self.stats["bold_runs"] += 1
        self.log({"kind": "bold", "why": why, "third": third, "cash": self.cash, "gain": [round(gm, 4), round(gs, 4)],
                  "ledger": {k: v for k, v in self.ledger.items() if k != "called"},
                  "calls": len(self.ledger["called"]), **{k: v for k, v in res.items()}})
        self.notify(f"bold ({why}): P(top3) {res['p_before']:.3f} -> {res['p_after']:.3f}, spend {res['spent']:,.0f} "
                    f"on {len(res['orders'])} orders (3rd {third:,.0f}, night gain {gm:.2f} +- {gs:.2f})")
        if res["orders"]:
            fills = self.v.take([{**o, "role": "B"} for o in res["orders"]], self.p.take_expiry_s)
            self.cash -= sum(f.get("cost", 0) for f in fills or [])
            self.log({"kind": "bold_fills", "fills": fills})
        self.last_bold, self.calls_since_bold = now, 0

    # ------------------------------------------------------------------ adaptive bold: what the night earns
    def note_calls(self):
        for r in self.races.values():
            if r.status == "CALLED" and not r.chamber and r.called_at:
                self.ledger["called"].setdefault(r.race, r.called_at)

    def note_fills(self, opps: list[dict], fills: list[dict]):
        """Realised edge at fill of the speed roles: each fill gets its order's edge pro rata."""
        by = {(o["ex"], o["side"]): o for o in opps if o["role"] in SPEED_ROLES}
        for f in fills or []:
            o = by.get((f.get("ex"), f.get("side")))
            q = float(f.get("filled") or 0)
            if o is None or q <= 0 or not o["qty"]:
                continue
            self.ledger["edge"] += o["edge"] * min(1.0, q / o["qty"])
            self.ledger["cost"] += float(f.get("cost") or 0)
            self.ledger["fills"] += 1

    def ledger_state(self) -> dict:
        return {**self.ledger, "called": dict(self.ledger["called"])}

    def adopt_ledger(self, d: dict | None):
        """Merge the ledger another run left (lease file): its totals if larger (only the holder trades, so the
        larger one is the night so far), the earliest time of each call."""
        if not isinstance(d, dict):
            return
        if float(d.get("edge") or 0) > self.ledger["edge"]:
            for k in ("edge", "cost", "fills"):
                self.ledger[k] = type(self.ledger[k])(d.get(k) or 0)
        for race, t in (d.get("called") or {}).items():
            cur = self.ledger["called"].get(race)
            if cur is None or str(t) < cur:
                self.ledger["called"][race] = str(t)

    def night_gain_now(self, now: datetime) -> tuple[float, float]:
        """(mean, sd) of the return on the cash bold leaves for the speed roles. Measured: edge per call so far x
        the calls still expected tonight (the last gain_rate_h hours' call rate x hours to the close, at most the
        races still uncalled) / the cash kept; blended with the prior night_gain by calls seen."""
        pm, ps = self.p.night_gain
        n = len(self.ledger["called"])
        if not self.p.bold_adaptive or n == 0:
            return pm, ps
        cut = (now - timedelta(hours=self.p.gain_rate_h)).isoformat()
        recent = sum(1 for t in self.ledger["called"].values() if t >= cut)
        uncalled = sum(1 for r in self.races.values() if not r.chamber and r.status != "CALLED")
        hours = max(0.0, (CLOSE - now).total_seconds() / 3600)
        left = min(uncalled, recent / self.p.gain_rate_h * hours)
        kept = max(self.cash - self.p.reserve, 1000.0)
        est = float(np.clip(self.ledger["edge"] / n * left / kept, -0.5, 20.0))
        w = n / (n + self.p.gain_prior_calls)
        return w * est + (1 - w) * pm, w * max(0.05, self.p.gain_rel_sd * abs(est)) + (1 - w) * ps

    def due_bold(self, now: datetime) -> str | None:
        if not self.p.bold_on:
            return None
        if self.p.bold_adaptive:
            n = len(self.ledger["called"])
            if n < self.p.bold_min_calls:
                return None          # scheduled plans wait for the evidence too (they stay pending)
            if self.last_bold is None:
                self.bold_done.update(t for t in self.p.bold_times if t <= now)
                return f"first plan after {n} calls"
        for t in self.p.bold_times:
            if t <= now and t not in self.bold_done:
                self.bold_done.add(t)
                return f"scheduled {t:%H:%M}Z"
        if self.calls_since_bold and self.last_bold and \
                (now - self.last_bold).total_seconds() >= 60 * self.p.bold_every_min:
            return f"{self.calls_since_bold} calls since last"
        return None

    def read_error(self, what: str, e: Exception):
        self.stats[f"{what}_errors"] += 1
        self.log({"kind": "read_error", "what": what, "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def focus(self) -> list[str]:
        """Exchanges whose full depth matters most: the called races, latest call first (the stale books)."""
        called = sorted((r for r in self.races.values() if r.status == "CALLED"),
                        key=lambda r: r.called_at or "", reverse=True)
        return [str(ex) for r in called for ex in r.exchanges.values()]

    # ------------------------------------------------------------------ one step
    def step(self, now: datetime, refresh_account: bool = True) -> dict:
        self.now = now
        ext = self.o.quotes()
        self._ext = ext
        try:
            desk = self.desk.calls() if self.desk is not None else None
        except Exception as e:  # noqa: BLE001 - the desk signal is optional
            self.read_error("desk", e)
            desk = None
        events = self.det.update({ex: {"kalshi": q.get("kalshi"), "polymarket": q.get("polymarket")}
                                  for ex, q in ext.items()}, now, desk=desk)
        for e in events:
            self.log({**e, "at": now.isoformat()})
            self.notify(f"{e['kind']}: {e['race']} -> {e.get('winner')} ({e.get('conf', e.get('best'))})")
            if e["kind"] == "call":
                self.calls_since_bold += 1
        self.note_calls()
        self.refresh_model(ext, now, force=bool(events))
        # A failed or slow read must not cost the takes: SIG is at its slowest on the night. The last account
        # snapshot stands in, and risk-off retries the same states next step.
        if refresh_account:
            try:
                self.cash, self.pos = self.v.account()
            except Exception as e:  # noqa: BLE001
                self.read_error("account", e)
        try:
            self.risk_off(now)
        except Exception as e:  # noqa: BLE001
            self.read_error("risk_off", e)
        if hasattr(self.v, "focus"):   # REST fallback: full books for the called races (election.py SigVenue)
            self.v.focus = self.focus()
        try:
            books = self.v.books()
        except Exception as e:  # noqa: BLE001
            self.read_error("books", e)
            books = {}
        opps = self.budget(self.opportunities(books, ext), now)
        fills = []
        if opps:
            fills = self.v.take(opps, self.p.take_expiry_s) if self.live else []
            self.pending += [(now, self.ex_race.get(str(f["ex"])), f["cost"]) for f in fills or [] if f.get("cost")]
            self.note_fills(opps, fills)
            for o in opps:
                self.stats[f"edge_{o['role']}"] += o["edge"]
                self.stats[f"orders_{o['role']}"] += 1
            self.log({"kind": "takes", "orders": [{k: o[k] for k in ("ex", "side", "price", "qty", "cost", "edge",
                                                                       "role")} for o in opps], "fills": fills})
        self.recycle(now, books)
        why = self.due_bold(now)
        if why:
            self.bold(books, now, why)
        return {"events": events, "takes": opps, "fills": fills}
