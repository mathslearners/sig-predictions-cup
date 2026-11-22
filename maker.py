"""Resting-liquidity maker for the SIG Predictions Cup (replaces run_once.py as the scheduled bot).

  python maker.py                       # DRY RUN: one cycle, prints the order diff, sends nothing
  python maker.py --live --minutes 110  # run cycles for 110 min (stops early at --stop-at), then exit

Why: naive players send large market orders into the books and sweep them far through fair value
(e.g. 80k shares of MN Senate D sold at 0.055 vs fair 0.895). Resting orders reserve no cash on SIG
(checked live 2 Oct), so a wide ladder costs nothing until it fills.

Each cycle:
  1. Fair values = Kalshi mid (targeted fetch) or config/fair_overrides.csv (refreshed every --fair-every s).
  2. Read top of book, my positions, my open orders, my balance.
  3. Desired orders per exchange, all of them BUYS (SIG nets opposite buys against holdings):
       dips     buy side s at SIG's own 30-min level for s minus each --dip-levels distance (since 5 Oct; --no-dip
                restores the Kalshi-anchored ladder below and the sweep catchers)
       ladder   buy side s at fair_s - delta for each --levels delta, sized in SUSQies (--notional)
       unwind   for a held side s, buy the opposite side so the pair closes at fair_s - --unwind-edge
                (never below avg cost + --min-gain); netting returns the cash at once
  4. Diff against what is resting: keep orders whose price is still inside the level's window (they
     keep their queue priority), cancel the rest, place what is missing (batches of 50).
Safety: every order carries expirationDate (+--expiry-min) as a dead-man switch; stale fair values,
a drawdown of --max-dd, the kill switch, the pause file or the window
end -> cancel everything and stop. No new ladder buys below --reserve cash.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sigbot.books import BookCache
from sigbot.client import READS_PER_MIN, ROOT, WRITES_PER_MIN, RateLimiter, SigAPIError, SigClient, new_idempotency_key
from sigbot.fairvalue import override_quotes
from sigbot.kalshi import Kalshi, event_ticker
from sigbot.lbtrack import LeaderPoller, Tape
from sigbot.markets import TOURNAMENT_SLUG, fetch_markets, parse_title
from sigbot.outside import outside_fair
from sigbot.polymarket import Polymarket
from sigbot.races import key_from_text
from sigbot.sniper import Sniper

OPEN_UTC = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
CLOSE_UTC = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
# Election night needs its own logic: results move prices faster than Kalshi-anchored quotes can follow.
NO_LADDER_AFTER = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
# election.py takes over at 21:40Z (routine at :40 of odd hours on 3-4 Nov); the maker must be gone by then.
ELECTION_HANDOVER = datetime(2026, 11, 3, 21, 37, tzinfo=timezone.utc)
# Pre-night: arrive at election night with as much free cash as possible (ELECTION_NIGHT.md). From PRE_NIGHT no new
# value/spike buys and busy_prep sells the holds in races called LATE (contested, or polls closing >= 03:00Z, or
# chamber control) at SIG's bid; safe early seats are kept, since the night bot turns them into cash at 0.99 when
# they are called at their poll close (selling them now at SIG's bid would give up ~10%). No new dips from
# NO_DIPS_AFTER.
PRE_NIGHT = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
NO_DIPS_AFTER = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
# --night-sell-all (26 Oct test, off by default): from here busy_prep sells EVERY hold at SIG's bid, early safe seats
# too, so the night bot starts with ~350k instead of ~200k of cash for call sweeps (stale books hold ~60x our cash).
NIGHT_SELL_FROM = datetime(2026, 11, 3, 19, 0, tzinfo=timezone.utc)
LATE_CLOSE_H = 3.0
TICK, MIN_PX, MAX_PX = 0.005, 0.005, 0.995
MAX_SANITY_GAP = 0.25
MIN_FAIR = 40           # fewer fresh Kalshi values than this = Kalshi is broken (others carry over <=15 min)
# Kalshi down (22 Oct 07:19-07:34Z: 503s for 15 min stopped the maker and cancelled every order): exchanges Kalshi
# normally prices run on Polymarket shifted by their last blend-vs-Polymarket offset, only if that offset was small
# and recent, standing further back.
FALLBACK_MAX_OFFSET = 0.03
FALLBACK_MAX_AGE = 6 * 3600
FALLBACK_BUFFER = 0.01
BATCH = 25              # 50-order batches took >20 s server-side on 2 Oct
DEAD_STATUSES = {"closed", "settled", "resolved", "halted", "cancelled", "canceled", "suspended"}
OUT_DIR = ROOT / "data" / "maker"
UNIVERSE = ROOT / "config" / "universe.json"   # cached market list + Kalshi map (python maker.py --remap)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tick_floor(p: float) -> float:
    return round(math.floor(round(p / TICK, 6)) * TICK, 3)


def tick_ceil(p: float) -> float:
    return round(math.ceil(round(p / TICK, 6)) * TICK, 3)


def other(side: str) -> str:
    return "no" if side == "yes" else "yes"


def next_slot_end(now: datetime) -> datetime:
    """The scheduled routine fires at ~:30:45 of every even UTC hour and its maker starts trading ~:31:30; any run
    stops by the next even-hour :29 (sniper joined by ~:29:30) so two makers never overlap, even when a run is
    started by hand. Was :27 with --minutes 108 until 4 Oct, which left ~13 min of every 2 h without sniping."""
    t = now.replace(minute=29, second=0, microsecond=0)
    while t.hour % 2 or t <= now:
        t += timedelta(hours=1)
    return t


def latency_stats(rows: list[tuple]) -> dict | None:
    """p50/p90/max ms of POST /orders/batch over the run, plus the raw (hour, ms, orders) rows."""
    if not rows:
        return None
    ms = sorted(r[1] for r in rows)
    return {"n": len(ms), "p50": ms[len(ms) // 2], "p90": ms[int(len(ms) * 0.9)], "max": ms[-1],
            "rows": rows[-500:]}


def parse_levels(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


class Maker:
    def __init__(self, args):
        self.a = args
        self.c = SigClient()
        self.k = Kalshi()
        self.kticker: dict[str, str] = {}
        self.poly = Polymarket()
        self.pticker: dict[str, str] = {}      # exchange id -> Polymarket market id (config/universe.json)
        self._sig_ema: dict = {}
        self._mid_ema: dict[str, float] = {}   # SIG Yes mid per exchange, smoothed across cycles (premium anchor)
        self._mine: set = set()   # order ids this process placed (--coexist cancels only these)
        self._open_by_ex = defaultdict(list)
        self.tid = None
        self.markets: dict[str, dict] = {}       # exchange id -> {"title", "key", "party", "market", "race"}
        self.pairs: dict[str, tuple] = {}        # two-party race -> (D exchange, R exchange)
        self.fair: dict[str, dict] = {}          # exchange id -> {"p", "buffer", "sources"}
        self.fair_at: datetime | None = None
        self.poly_offset: dict[str, tuple] = {}  # exchange -> (blend p - Polymarket mid, when, blend buffer)
        self.kalshi_down: str | None = None      # last Kalshi error while the fair runs on the Polymarket fallback
        self.start_value = None
        self.run_ts = utcnow().strftime("%Y%m%dT%H%M%SZ")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = OUT_DIR / f"maker_{self.run_ts}.jsonl"
        self.totals = defaultdict(float)
        self.last_cash: float | None = None
        self.last_cash_at: datetime | None = None   # when last_cash was read (the sniper's basket budget resets on it)
        self.sniper: Sniper | None = None
        self.books = BookCache()                 # full books from the realtime feed (maker owns the feed)
        self.feed = None
        self._event_lock = threading.Lock()
        hot = ROOT / "config" / "hot_markets.json"
        self.hot: set[str] = set() if args.no_hot or not hot.exists() else \
            set(json.loads(hot.read_text(encoding="utf-8"))["exchanges"])
        busy = ROOT / "config" / "busy_markets.json"
        self.busy: set[str] = set(json.loads(busy.read_text(encoding="utf-8"))["exchanges"]) if busy.exists() else set()
        self.busy_source = f"{busy.name} (static)"
        self.feed_trades: dict[str, list] = defaultdict(lambda: [0, 0.0])   # exchange -> [trades, shares] seen on the feed
        self._trade_ids: set = set()
        self._trade_lock = threading.Lock()
        self.feed_since: datetime | None = None
        self.tape = Tape()                       # leader tracking: prints + book top before them (sigbot/lbtrack.py)
        self.lbpoll: LeaderPoller | None = None
        self._lb_pushes = 0
        self._mid_hist: dict[str, deque] = defaultdict(deque)   # exchange -> (unix time, SIG Yes mid) for the dip anchor
        self._prep_ex: set = set()              # holds busy_prep chose to sell this run (see prep_allowance)
        self._start_pos: dict | None = None
        self._noop: dict[tuple, dict] = {}      # (ex, side, role) -> last no-op placement (see noop_blocked)
        self._v_noop: dict[tuple, datetime] = {}   # (ex, side) -> last value buy answered as a no-op (see rotation)
        self._quoting: set = set()              # exchanges quoted at the touch this cycle (quote_set)
        self.batch_ms: list[tuple] = []         # (UTC hour, ms, orders) per POST /orders/batch

    # ------------------------------------------------------------------ inputs
    def load_markets(self, deadline: datetime):
        """Market list and Kalshi map. Live /markets first; config/universe.json is the fallback (the 237 markets
        are fixed, and on 2 Oct a slow SIG server timed /markets out 7 times in a row and killed a run)."""
        while True:
            if UNIVERSE.exists() and not self.a.remap:   # the cache skips /markets, the slowest SIG endpoint
                u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
                self.tid, mk = u["tournamentId"], u["markets"]
                break
            try:
                t, mk = fetch_markets(self.c)
                self.tid = t.id
                break
            except Exception as e:  # noqa: BLE001
                if UNIVERSE.exists():
                    u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
                    print(f"/markets failed ({type(e).__name__}); using {UNIVERSE.name} from {u['saved']}", flush=True)
                    self.tid, mk = u["tournamentId"], u["markets"]
                    break
                if utcnow() + timedelta(minutes=5) >= deadline:
                    raise
                print(f"/markets failed ({type(e).__name__}); retrying in 60 s", flush=True)
                time.sleep(60)
        self.markets = {}
        races = defaultdict(list)
        for m in mk:
            if str(m.get("status", "")).lower() in DEAD_STATUSES:
                continue
            race, race_type, party = parse_title(m["title"])
            key = key_from_text(race) if race else None
            for ex in m.get("exchanges", []):
                self.markets[str(ex["id"])] = {"title": m["title"], "key": key, "party": party,
                                               "market": str(m["id"]), "race": race}
                races[(race, race_type)].append(str(ex["id"]))
        # Two-party races (114): NO on both parties pays >= 1 whoever wins. The 3 races with an Independent market
        # are left out (an endorsed independent could count for two parties under the fusion-ticket rule).
        self.pairs = {}
        for (race, _), exs in races.items():
            by_party = {self.markets[ex]["party"]: ex for ex in exs}
            if race and len(exs) == 2 and set(by_party) == {"D", "R"}:
                self.pairs[race] = (by_party["D"], by_party["R"])
        u = json.loads(UNIVERSE.read_text(encoding="utf-8")) if UNIVERSE.exists() else {}
        self.pticker = {ex: pid for ex, pid in u.get("polymarket", {}).items() if ex in self.markets}
        cached = u.get("kalshi", {})
        if cached and not self.a.remap:
            self.kticker = {ex: tk for ex, tk in cached.items() if ex in self.markets}
            return
        # Map each exchange to its Kalshi party market once (one call per race); refreshes then batch by ticker.
        self.kticker = {}
        for (race, rt), exs in races.items():
            et = event_ticker(race, rt) if race else None
            try:
                q = self.k.party_quotes(et) if et else {}
            except Exception:  # noqa: BLE001 - that race just has no fair value
                q = {}
            for ex in exs:
                kq = q.get(self.markets[ex]["party"])
                if kq:
                    self.kticker[ex] = kq["ticker"]
        if self.a.remap:
            slim = [{"id": m["id"], "title": m["title"], "status": m.get("status"),
                     "exchanges": [{"id": e["id"]} for e in m.get("exchanges", [])]} for m in mk]
            UNIVERSE.write_text(json.dumps({"saved": iso(utcnow()), "tournamentId": self.tid, "markets": slim,
                                            "kalshi": self.kticker, "polymarket": u.get("polymarket", {})},
                                           indent=1), encoding="utf-8")
            print(f"saved {UNIVERSE.name}: {len(slim)} markets, {len(self.kticker)} Kalshi tickers", flush=True)

    def outside_quotes(self, k: Kalshi, poly: Polymarket) -> tuple[dict | None, dict, str | None]:
        """(Kalshi quotes by ticker, Polymarket quotes by id, Kalshi error), fetched in parallel. A failed Polymarket
        comes back empty; a failed Kalshi comes back as None plus its error (the fair then falls back to Polymarket)."""
        def kal():
            tickers = list(set(self.kticker.values()))
            try:
                kq = k.quotes(tickers)
                missing = [t for t in tickers if t not in kq]
                if missing:   # usually a 429 on one 50-ticker chunk: one slower retry
                    time.sleep(3)
                    kq.update(k.quotes(missing))
                return kq, None
            except Exception as e:  # noqa: BLE001 - 503s / timeouts: Polymarket alone carries the fair
                return None, f"{type(e).__name__}: {str(e)[:200]}"

        def pol():
            try:
                return poly.quotes(list(set(self.pticker.values())))
            except Exception:  # noqa: BLE001 - Kalshi alone still prices 197 exchanges
                return {}
        with ThreadPoolExecutor(2) as pool:
            fk, fp = pool.submit(kal), pool.submit(pol)
            (kq, kerr), pq = fk.result(), fp.result()
            return kq, pq, kerr

    def fallback_fair(self, ex: str, praw: dict | None, now: datetime) -> dict | None:
        """Kalshi down: a Kalshi-priced exchange's fair from Polymarket alone, shifted by its last offset (the blend
        sits up to ~0.05 off Polymarket, and Kalshi wins outright when they're > 0.06 apart). None when Polymarket has
        no tight quote or the offset is unknown, large or stale, so those orders go rather than trade on a guess."""
        f = outside_fair(None, praw)
        off = self.poly_offset.get(ex)
        if not f or off is None or abs(off[0]) > FALLBACK_MAX_OFFSET or \
                (now - off[1]).total_seconds() > FALLBACK_MAX_AGE:
            return None
        p = min(MAX_PX, max(MIN_PX, f["p"] + off[0]))
        return {"p": p, "buffer": max(f["buffer"], off[2]) + FALLBACK_BUFFER,
                "sources": {"polymarket": f["p"], "offset": round(off[0], 4), "kalshi_down": True}}

    def refresh_fair(self):
        """Kalshi and Polymarket mids per mapped exchange (outside_fair); config/fair_overrides.csv wins.
        Kalshi failing outright: fallback_fair for the exchanges it prices, Polymarket as usual for the rest."""
        try:
            kq, pq, kerr = self.outside_quotes(self.k, self.poly)
        except Exception as e:  # noqa: BLE001 - keep the old values; they age out via --fair-max-age
            self.event({"kind": "fair_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            return False
        now = utcnow()
        out = {}
        for ex in self.markets:
            praw = pq.get(self.pticker[ex]) if ex in self.pticker else None
            if kq is None and ex in self.kticker:
                f = self.fallback_fair(ex, praw, now)
                if f:
                    out[ex] = {**f, "override": False, "at": now}
                elif ex in self.fair and (now - self.fair[ex]["at"]).total_seconds() < self.a.fair_max_age and \
                        not (self.fair[ex].get("sources") or {}).get("kalshi_down"):
                    out[ex] = self.fair[ex]   # no Polymarket fallback: the last Kalshi-based value, aging out
                continue
            kraw = kq.get(self.kticker[ex]) if kq is not None and ex in self.kticker else None
            f = outside_fair(kraw, praw)
            if f:
                out[ex] = {**f, "override": False, "at": now}
                if "kalshi" in f["sources"] and "polymarket" in f["sources"]:
                    self.poly_offset[ex] = (f["p"] - f["sources"]["polymarket"], now, f["buffer"])
            elif kraw is None and praw is None and ex in self.fair and \
                    (now - self.fair[ex]["at"]).total_seconds() < self.a.fair_max_age:
                # Missing from both responses (a rate-limited chunk): carry the last value over rather than
                # cancelling its orders and losing queue priority. A wide/inactive quote still drops it.
                out[ex] = self.fair[ex]
        ov = override_quotes()
        for ex, m in self.markets.items():
            p = ov.get((m["key"], m["party"])) if m["key"] and m["party"] else None
            if p is not None:
                out[ex] = {"p": p, "buffer": 0.0, "sources": {"override": p}, "override": True, "at": now}
        if sum(1 for v in out.values() if v["at"] == now) < MIN_FAIR:   # Kalshi broken: keep old set, it ages out
            self.event({"kind": "fair_error", "error": f"only {len(out)} fair values"})
            return False
        if kerr and not self.kalshi_down:
            n = sum(1 for v in out.values() if (v.get("sources") or {}).get("kalshi_down") and v["at"] == now)
            self.event({"kind": "kalshi_down", "error": kerr, "fallback": n, "fair": len(out)})
            print(f"Kalshi down ({kerr[:80]}): {n} exchanges on the Polymarket fallback, {len(out)} priced", flush=True)
        elif not kerr and self.kalshi_down:
            self.event({"kind": "kalshi_up", "fair": len(out)})
            print("Kalshi back", flush=True)
        self.kalshi_down = kerr
        self.fair, self.fair_at = out, utcnow()
        return True

    def tops(self) -> dict[str, tuple]:
        ids = list(self.markets)
        out = {}
        for i in range(0, len(ids), 100):
            r = self.c.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=self.tid)
            for x in (r.get("data") if isinstance(r, dict) else r) or []:
                out[str(x.get("exchangeId"))] = (x.get("bestBid"), x.get("bestAsk"))
        return out

    def positions(self) -> dict[str, dict]:
        """exchange -> {"side", "qty", "avg"} (one side per exchange: SIG nets opposite holdings)."""
        raw = self.c.get(f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions")
        out = {}
        for it in raw.get("positions", []):
            if it.get("settled"):
                continue
            q = float(it.get("quantity") or 0)
            if abs(q) < 1:
                continue
            opened = [l.get("openedAt") for l in it.get("lots") or [] if l.get("openedAt")]
            out[str(it["exchangeId"])] = {"side": "yes" if q > 0 else "no", "qty": int(abs(q)),
                                          "avg": float(it.get("avgCost") or 0),
                                          "value": float(it.get("marketValue") or 0),
                                          "opened": min(opened) if opened else None}
        return out

    def open_orders(self) -> list[dict]:
        return list(self.c.paginate("/orders", limit=200, status="open", tournamentId=self.tid))

    def balance(self) -> float:
        return float(self.c.get(f"/tournaments/{TOURNAMENT_SLUG}")["myBalance"])

    def book(self, tries: int = 2) -> tuple[float, dict]:
        """(cash, positions) read consistently: cash before and after the positions read must agree, otherwise a
        fill landed in between and the pair would misstate value by the fill's cost."""
        cash = self.balance()
        for _ in range(tries):
            pos = self.positions()
            after = self.balance()
            if abs(after - cash) < 1:
                break
            cash = after
        return after, pos

    def value_of(self, cash: float, pos: dict) -> float:
        return cash + sum(h["qty"] * ((self.fair[ex]["p"] if h["side"] == "yes" else 1 - self.fair[ex]["p"])
                                     if ex in self.fair else h["avg"]) for ex, h in pos.items())

    # ------------------------------------------------------------------ decisions
    def consensus(self, tops: dict) -> dict[str, dict]:
        """Fair values for races no outside market prices (since 4 Oct only Alaska Governor; Polymarket covers the
        other 36 Kalshi doesn't list, and this also catches any race whose outside quotes go wide): SIG's own D/R pair,
        p_R = (mid_R + 1 - mid_D) / 2, smoothed across cycles so one sweep can't drag it. Only used with the
        deep --sig-levels, never the tight ones."""
        races = defaultdict(dict)
        for ex, m in self.markets.items():
            if m["key"] and m["party"] in ("D", "R"):
                races[m["key"]][m["party"]] = ex
        out = {}
        for key, pair in races.items():
            if len(pair) != 2 or any(ex in self.fair for ex in pair.values()):
                continue
            mids = {}
            for party, ex in pair.items():
                b, a = tops.get(ex, (None, None))
                if b is None or a is None or a - b > 0.05:
                    break
                mids[party] = (a + b) / 2
            if len(mids) != 2:
                continue
            p_r = (mids["R"] + 1 - mids["D"]) / 2
            prev = self._sig_ema.get(key)
            p_r = p_r if prev is None else prev + self.a.sig_alpha * (p_r - prev)
            self._sig_ema[key] = p_r
            for party, ex in pair.items():
                out[ex] = {"p": p_r if party == "R" else 1 - p_r, "buffer": 0.0, "sources": {"sig": p_r},
                           "override": False, "sig": True}
        return out

    def pair_locked(self, pos: dict, cash: float) -> dict[str, int]:
        """exchange -> shares held as half of a NO+NO pair (No on both parties of a two-party race pays 1 whoever
        wins). Unwinding one leg sells it below fair (our exits never pay more than fair) and leaves the other leg
        naked, so while cash isn't short (>= --costly-exit-cash) the paired shares are held to settlement and only
        the excess unwinds. Basket takes (sniper) build these on purpose; catcher fills on both sides do too."""
        if not self.a.pair_hold or cash < self.a.costly_exit_cash:
            return {}
        out = {}
        for d, r in self.pairs.values():
            hd, hr = pos.get(d), pos.get(r)
            if hd and hr and hd["side"] == "no" and hr["side"] == "no":
                q = min(hd["qty"], hr["qty"])
                out[d] = out[r] = q
        return out

    def desired(self, tops: dict, pos: dict, cash: float, ladder_on: bool, catch_on: bool = True) -> list[dict]:
        a = self.a
        out = []
        locked = self.pair_locked(pos, cash)
        holds = set()
        race_cost = defaultdict(float)   # value buys are capped per race (the D and R legs of one race are one bet)
        for ex2, h in pos.items():
            race_cost[self.race_of(ex2)] += h["qty"] * h["avg"]
        rot = self.rotation(tops, pos, cash, race_cost) if catch_on else {}
        if rot:
            self.event({"kind": "rotate", "sell": {ex: {"qty": q, "bid": b} for ex, (q, b) in rot.items()}, "cash": cash})
        fu, fu_sell = self.followups(tops, pos, cash, race_cost) if catch_on else ({}, {})
        if fu:
            self.event({"kind": "followup", "targets": fu, "cash": cash,
                        "sell": {ex: {"qty": q, "bid": b} for ex, (q, b) in fu_sell.items()}})
        rot = {**fu_sell, **rot}
        prep = {**rot, **self.busy_prep(tops, pos, cash)}
        qset = self.quote_set(tops, pos) if catch_on else set()
        q_inv = sum(pos[ex]["qty"] * pos[ex]["avg"] for ex in qset if ex in pos)
        self._quoting = qset
        for ex, f in {**self.consensus(tops), **self.fair}.items():
            p = f["p"]
            bid, ask = tops.get(ex, (None, None))
            if bid is not None and ask is not None and abs((bid + ask) / 2 - p) > MAX_SANITY_GAP and not f["override"]:
                continue   # probable mapping error
            held = pos.get(ex)
            # unwind: close held side s by buying the other side (SIG nets the pair and pays 1 at once). The exit
            # price starts at fair_s - unwind_edge and slides to avg + min_gain over --unwind-decay-min, so
            # inventory turns over within the hour instead of waiting for settlement.
            foreign_exit = a.coexist and held and any(
                o["id"] not in self._mine and o.get("side") == other(held["side"]) and
                float(o.get("priceLimit") or 0) >= (p if held["side"] == "no" else 1 - p) - 0.015
                for o in self._open_by_ex.get(ex, []))
            # Paired NO+NO shares (pair_locked) are held to settlement; only the excess unwinds.
            uheld = {**held, "qty": held["qty"] - locked.get(ex, 0)} if held else None
            # Quote inventory exits through the quotes (passively); the normal unwind would cross the spread.
            if uheld and uheld["qty"] >= 10 and not foreign_exit and ex not in qset:
                s = held["side"]
                fair_s = p if s == "yes" else 1 - p
                floor_px = held["avg"] + a.min_gain
                age = 0.0
                if held.get("opened"):   # e.g. 2026-10-02T00:13:27.5158681+00:00 (7 fractional digits, UTC)
                    opened = datetime.strptime(held["opened"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                    age = max(0.0, (utcnow() - opened).total_seconds() / 60)
                frac = min(1.0, age / a.unwind_decay_min)
                # Never exit more than --max-exit-discount below fair: a position bought deep under fair (e.g. a
                # 0.885 No worth 0.99) is worth more held than sold cheap (that leak cost ~1.7k on 2 Oct 09:20Z).
                target = max(floor_px, fair_s - a.max_exit_discount,
                             (fair_s - a.unwind_edge) * (1 - frac) + floor_px * frac)
                sell_px = tick_ceil(target)
                hold = self.value_hold(f, uheld)
                prem = None if hold else self.premium_exit(ex, f, uheld, cash, age)
                if hold and ex in prep:
                    # Busy-hour prep: sell (part of) this hold at SIG's bid for the side, which clears cost.
                    holds.add(ex)
                    q, bid_s = prep[ex]
                    buy_px = round(1 - bid_s, 3)
                    if buy_px >= MIN_PX:
                        out.append({"ex": ex, "side": other(s), "price": buy_px, "qty": min(q, uheld["qty"]),
                                    "role": "unwind", "lo": round(buy_px - 0.0051, 4), "hi": round(buy_px + 0.0051, 4)})
                elif hold:
                    # Held to settlement: the only exit sells near fair (SIG rarely pays that; settlement does).
                    holds.add(ex)
                    buy_px = tick_floor(1 - fair_s + a.value_exit_under)
                    if buy_px >= MIN_PX:
                        out.append({"ex": ex, "side": other(s), "price": buy_px, "qty": uheld["qty"],
                                    "role": "unwind", "lo": round(buy_px - 0.0051, 4), "hi": round(buy_px + 0.0051, 4)})
                elif prem:
                    out.append(prem)
                else:
                    buy_px = round(1 - sell_px, 3)
                    rec = self.recycle_px(ex, fair_s, uheld, age, cash)
                    if rec is not None and rec > buy_px:
                        buy_px = rec
                    if buy_px >= MIN_PX:
                        out.append({"ex": ex, "side": other(s), "price": buy_px, "qty": uheld["qty"],
                                    "role": "unwind", "lo": round(buy_px - 0.0151, 4), "hi": round(buy_px + 0.0051, 4)})
            if not catch_on:
                continue
            spike_sides = set()
            if ex in fu:
                # Dump follow-up: take the dumped favourite now; our other buys here would commit the balance and turn
                # the take into a no-op (they are cancelled before anything is placed this cycle).
                t = fu[ex]
                qty = int(t["cost"] / t["px"])
                race_cost[self.race_of(ex)] += qty * t["px"]
                out.append({"ex": ex, "side": t["side"], "price": t["px"], "qty": qty, "role": "V", "fu": True,
                            "prio": -2.0 - t["roc"], "roc": t["roc"],
                            "lo": round(t["px"] - 0.0051, 4), "hi": round(t["px"] + 0.0051, 4)})
                continue
            if ex not in rot:   # never buy back what rotation is selling
                out += self.value_buys(ex, f, held, bid, ask, race_cost)
                kk = self.spikes(ex, f, held, bid, ask, race_cost)
                spike_sides = {w["side"] for w in kk}
                out += kk
            if ex in qset:
                out += self.quotes(ex, held, bid, ask, q_inv)
            if not a.no_dip:   # the dip ladder replaces the Kalshi-anchored ladder and sweep catchers
                if ladder_on:   # spike catchers replace the dips on value sides (closer and bigger)
                    out += [w for w in self.dips(ex, f, held, bid, ask) if w["side"] not in spike_sides]
                out += self.premium(ex, f, held, bid, ask)
                continue
            for s in ("yes", "no") if ladder_on else ():
                fair_s = p if s == "yes" else 1 - p
                if fair_s > a.max_fair_side:   # near-certain side: few sellers to exit to, cash would sit for weeks
                    continue
                held_cost = held["qty"] * held["avg"] if held and held["side"] == s else 0.0
                # Near-certain sides recycle slowly (few players sell longshots back), so cap them lower:
                # their cash is locked until settlement and the ladder stops once cash hits --reserve.
                if held_cost >= (a.max_pos_safe if fair_s > 0.9 else a.max_pos):
                    continue
                levels = parse_levels(a.sig_levels if f.get("sig") else a.levels)
                notionals = parse_levels(a.sig_notional if f.get("sig") else a.notional)
                for i, d in enumerate(levels):
                    delta = d + f["buffer"]
                    px = tick_floor(fair_s - delta)
                    if px < MIN_PX or px > self.open_cap(ex, s):
                        continue
                    notional = notionals[min(i, len(notionals) - 1)]
                    qty = int(min(a.max_shares, notional / px))
                    if qty < 10:
                        continue
                    # Keep window around the target, never reaching halfway to a neighbouring level (so levels
                    # can't adopt each other's orders) and never closer to fair than half this level's distance.
                    wider = min(a.keep_wider, (levels[i + 1] - d) / 2 - 0.001) if i + 1 < len(levels) else a.keep_wider
                    tighter = min(a.keep_tighter, (d - levels[i - 1]) / 2 - 0.001 if i else d / 2)
                    out.append({"ex": ex, "side": s, "price": px, "qty": qty, "role": f"L{i + 1}",
                                "roc": round((fair_s - px) / px, 4),
                                "lo": round(fair_s - delta - wider, 4),
                                "hi": round(fair_s - delta + tighter, 4)})
            if not a.no_stubs:
                out += self.stubs(ex, f, held, bid, ask)
            out += self.premium(ex, f, held, bid, ask)
        # An exit buys the other side at q, i.e. sells the held side at 1 - q (SIG's level, often under Kalshi fair).
        # A ladder/catcher buy of the held side at or above 1 - q would self-match: SIG cancels the incoming order,
        # so one of the pair died every cycle (3 Oct 02:35Z: 47 orders re-placed per cycle) and, when the exit came
        # second, the position never exited. Drop those buys while the exit stands.
        exits = {w["ex"]: w for w in out if w["role"] == "unwind"}
        # A dip/catcher buy of the side opposite a value hold would net the hold away far under fair.
        out = [w for w in out if w["role"] == "unwind" or w["ex"] not in holds or w["side"] == pos[w["ex"]]["side"]]
        return [w for w in out if w["role"] == "unwind" or w["ex"] not in exits or
                w["side"] == exits[w["ex"]]["side"] or w["price"] + exits[w["ex"]]["price"] < 1 - 1e-9]

    def race_of(self, ex: str) -> str:
        m = self.markets.get(ex) or {}
        return m.get("race") or ex   # e.g. "Delaware Senate"

    def is_control(self, ex: str) -> bool:
        """Chamber-control markets ("Will the Democratic Party win the U.S. Senate?", race "U.S. Senate")."""
        return not self.a.no_control and self.race_of(ex) in ("U.S. Senate", "U.S. House")

    def race_cap(self, ex: str) -> float:
        return self.a.control_max_race if self.is_control(ex) else self.a.value_max_race

    @staticmethod
    def pre_night() -> bool:
        return PRE_NIGHT <= utcnow() < ELECTION_HANDOVER

    def called_late(self, ex: str, fair_s: float) -> bool:
        """Races whose result comes hours into the night (or later): their holds tie up cash all night."""
        from sigbot.swing import POLL_CLOSE_H
        m = self.markets.get(ex) or {}
        if self.is_control(ex):
            return False       # chamber control is the bold overlay's call (election.py at 22:30Z), not a pre-sale
        if fair_s < 0.95:
            return True
        key = m.get("key")
        return POLL_CLOSE_H.get(key[0] if key else "", 2.0) >= LATE_CLOSE_H

    def in_prep(self) -> bool:
        """The --busy-prep-min minutes before --dip-full-hours start (15:00-16:00Z by default)."""
        if self.pre_night():
            return True
        lo_h = int(self.a.dip_full_hours.split("-")[0])
        now = utcnow()
        m = (now.hour * 60 + now.minute - (lo_h * 60 - self.a.busy_prep_min)) % 1440
        return self.a.busy_cash > 0 and m < self.a.busy_prep_min

    def busy_prep(self, tops: dict, pos: dict, cash: float) -> dict[str, tuple[int, float]]:
        """ex -> (shares, SIG bid for the held side): value holds to sell so busy hours start with --busy-cash free
        (6 Oct 12:50Z: "get rid of profitable positions to free money for busy hours"; at 12:16Z a RI Governor
        dip took the last 37k and 16-24Z would have had 10k). Only in the prep window, only holds whose SIG bid
        clears avg + --busy-prep-gain (never under cost), the smallest remaining edge to fair per SUSQie first
        (each sale gives up fair - bid, ~13-19c/share on 6 Oct's holds), and only as many shares as the gap needs."""
        a = self.a
        night = self.pre_night()
        if not self.in_prep() or (cash >= a.busy_cash and not night):
            return {}
        sell_all = night and a.night_sell_all and utcnow() >= NIGHT_SELL_FROM
        cands = []
        for ex, h in pos.items():
            f = self.fair.get(ex)
            if not f or h["qty"] < 10 or not (sell_all or self.value_hold(f, h)):
                continue
            if night and not sell_all and not self.called_late(ex, f["p"] if h["side"] == "yes" else 1 - f["p"]):
                continue
            bid, ask = tops.get(ex, (None, None))
            bid_s = bid if h["side"] == "yes" else (None if ask is None else round(1 - ask, 3))
            fair_s = f["p"] if h["side"] == "yes" else 1 - f["p"]
            if sell_all:     # every hold to cash for the night's sweeps, but never into an empty or stale book
                if bid_s is None or bid_s < max(h["avg"] - a.night_sell_loss, fair_s - a.night_sell_gap) - 1e-9:
                    continue
            elif bid_s is None or bid_s < h["avg"] + a.busy_prep_gain - 1e-9:
                continue
            cands.append(((fair_s - bid_s) / bid_s, ex, bid_s))
        need, out = (float("inf") if night else a.busy_cash - cash), {}
        self._prep_ex.update(ex for _, ex, _ in cands)
        for _, ex, bid_s in sorted(cands):
            if need <= 0:
                break
            # pre-night needs every share (need = inf: int(inf) raised OverflowError, so no pre-night sale ever ran)
            q = int(pos[ex]["qty"]) if night else min(int(pos[ex]["qty"]), int(need / bid_s) + 1)
            out[ex] = (q, bid_s)
            need -= q * bid_s
        return out

    def prep_allowance(self, pos: dict) -> float:
        """Fair value given up on purpose by busy-prep sales this run (shares sold x (fair - avg)), so the --max-dd
        switch doesn't read them as a loss: today's sale gives up ~8k of the 15k allowance by design."""
        out = 0.0
        for ex in self._prep_ex:
            h0, f = (self._start_pos or {}).get(ex), self.fair.get(ex)
            if not h0 or not f:
                continue
            h = pos.get(ex)
            sold = h0["qty"] - (h["qty"] if h and h["side"] == h0["side"] else 0)
            fair_s = f["p"] if h0["side"] == "yes" else 1 - f["p"]
            out += max(0, sold) * max(0.0, fair_s - h0["avg"])
        return out

    def value_hold(self, f: dict, held: dict | None) -> bool:
        """Held to settlement: worth >= --value-hold-edge over avg cost at Kalshi/Polymarket fair (net of the source
        buffer). 6 Oct: books were one tick wide on 232 of 234 exchanges and 4c dips had stopped, so turnover found
        nothing to do with 87k idle cash, while the 6h freeing rule was about to sell 69k Delaware Senate R NO (cost
        0.880, fair 0.986, SIG 0.875) ~3c under cost. With cash idle, holding costs nothing; the user chose to hold.
        A fair move under the threshold ends the hold and the normal exits (incl. freeing) take over."""
        if self.a.no_value or not held or f.get("sig"):
            return False
        fair_s = f["p"] if held["side"] == "yes" else 1 - f["p"]
        return fair_s - f["buffer"] - held["avg"] >= self.a.value_hold_edge - 1e-9

    def value_buys(self, ex: str, f: dict, held: dict | None, bid, ask, race_cost: dict) -> list[dict]:
        """Settlement-value buys: take a side SIG offers >= --value-edge under Kalshi/Polymarket fair (net of buffer)
        and hold it to settlement. 6 Oct 01:20Z: 77 exchanges were >= 0.03 under fair at the touch (mean 0.05, price
        ~0.88), mostly favourites of safe seats priced low because players overpay for the long shot (RI Senate D 0.86
        vs 0.991). Only while the side is offered that cheap right now (a limit at fair - edge takes it, any remainder
        rests there), at most --value-max-pos per exchange and --value-max-race per race; the pool keeps
        --value-min-cash free for dips and election night (budget())."""
        a = self.a
        if a.no_value or f.get("sig"):
            return []
        race = self.race_of(ex)
        out = []
        for s in ("yes", "no"):
            if held and held["qty"] >= 10 and held["side"] != s:
                continue   # buying the other side would only net the holding
            fair_s = f["p"] if s == "yes" else 1 - f["p"]
            best_s = ask if s == "yes" else (1 - bid if bid is not None else None)   # cheapest offer of side s
            if best_s is None:
                continue
            worth = fair_s - f["buffer"]
            lim = tick_floor(worth - a.value_edge)
            if not MIN_PX <= lim <= MAX_PX or lim < best_s - 1e-9:
                continue
            # Since 9 Oct: take only the best offer, and only at >= --value-take-roc to settlement; otherwise join the
            # side's best bid (lottery buyers of the long shot fill it: ~1M RI Senate favourite shares went to resting
            # bids in 17.8 h). The old limit at fair - edge swept everything up to it (RI: 0.835 -> 0.94).
            bid_s = bid if s == "yes" else (1 - ask if ask is not None else None)
            # Chamber control (since 11 Oct): a deep market one player pushed off fair (10 Oct 20:11-21:24Z,
            # calm-badger sold D Senate 0.61 -> 0.555 while Kalshi sat at 0.605 all day), so take the offer at
            # --control-take-roc; the hold's exit at fair - --value-exit-under returns the cash if it reverts.
            take_roc = a.control_take_roc if self.is_control(ex) else a.value_take_roc
            if (worth - best_s) / best_s >= take_roc - 1e-9:
                px = round(best_s, 3)
            elif bid_s is not None and bid_s < best_s - 1e-9:
                px = round(bid_s, 3)
            else:
                continue
            if px < MIN_PX or (worth - px) / px < a.value_min_roc - 1e-9:
                continue
            held_cost = held["qty"] * held["avg"] if held and held["side"] == s else 0.0
            room = min(a.value_max_pos - held_cost, self.race_cap(ex) - race_cost[race])
            qty = int(room / px)
            if qty < 100:
                continue
            race_cost[race] += qty * px
            # Budget goes to the best return to settlement first; control takes come before everything (rotation
            # sells holds to fund them, and resting value bids elsewhere would otherwise absorb that cash).
            ctrl_take = self.is_control(ex) and px >= best_s - 1e-9
            out.append({"ex": ex, "side": s, "price": px, "qty": qty, "role": "V",
                        "prio": -(worth - px) / px - (1.0 if ctrl_take else 0.0),
                        "roc": round((worth - px) / px, 4), "lo": round(px - 0.0051, 4), "hi": round(px + 0.0051, 4)})
        return out

    def spikes(self, ex: str, f: dict, held: dict | None, bid, ask, race_cost: dict) -> list[dict]:
        """Spike catchers (since 9 Oct): on a side SIG prices >= --spike-min-gap under Kalshi/Poly fair (the long-shot
        premium), rest big buys --spike-levels under SIG's own 30-min level for the side. 8 Oct 18Z a long-shot buying
        spree pushed RI Senate R to 0.205 (2.24M shares in the hour, 921k of the favourite at <= 0.82, fair 0.991);
        our premium catcher at SIG's level + 0.04 sat exactly at the peak and took 21k. Replay of the 9 Oct tape: 1-2
        such fills a day, each worth ~9-12k at settlement but only ~1k resold an hour later, so fills are held (they
        are value holds by construction). Resting costs no cash; the engine caps a fill at the free cash."""
        a = self.a
        if a.no_spike or f.get("sig"):
            return []
        anc = self.anchor(ex)
        if anc is None:
            return []
        room = a.spike_max_race - race_cost[self.race_of(ex)]
        out = []
        for s in ("yes", "no"):
            if held and held["qty"] >= 10 and held["side"] != s:
                continue
            worth = (f["p"] if s == "yes" else 1 - f["p"]) - f["buffer"]
            lvl = anc if s == "yes" else 1 - anc
            if worth - lvl < a.spike_min_gap - 1e-9:
                continue
            best_s = ask if s == "yes" else (1 - bid if bid is not None else None)
            for i, d in enumerate(parse_levels(a.spike_levels)):
                px = tick_floor(lvl - d)
                if px < MIN_PX or (best_s is not None and px >= best_s - 1e-9):
                    continue
                qty = int(min(a.spike_notional, room) / px)
                if qty < 100:
                    continue
                out.append({"ex": ex, "side": s, "price": px, "qty": qty, "role": f"K{i + 1}",
                            "prio": -(worth - px) / px, "roc": round((worth - px) / px, 4),
                            "lo": round(px - 0.0051, 4), "hi": round(px + 0.0051, 4)})
        return out

    @staticmethod
    def age_min(held: dict | None) -> float:
        """Minutes since a position's first lot (e.g. 2026-10-02T00:13:27.5158681+00:00, 7 fractional digits, UTC)."""
        if not held or not held.get("opened"):
            return 0.0
        opened = datetime.strptime(held["opened"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (utcnow() - opened).total_seconds() / 60)

    def quote_set(self, tops: dict, pos: dict) -> set[str]:
        """Exchanges to quote at the touch this cycle (since 9 Oct, test size): busy markets in --quote-hours with a
        one- or two-tick book whose mid is within --quote-max-gap of fair, holding nothing but quote inventory. The
        volume leaders (steady-finch, noble-birch, clever-robin: 20-35x their balance a week at ~1% of volume) quote
        like this; replaying the 9 Oct tape with passive exits gave ~0.16-0.2c/share, 1-2.4k/day on 25-50k of
        capital, positive in both halves, if we get ~3-10% of the touch volume (queue position unknown)."""
        a = self.a
        if a.no_quote:
            return set()
        lo_h, hi_h = (int(x) for x in a.quote_hours.split("-"))
        if not lo_h <= utcnow().hour < hi_h:
            return set()
        cands = []
        for ex in self.busy:
            f = self.fair.get(ex)
            if not f or f.get("sig") or f.get("override"):
                continue
            bid, ask = tops.get(ex, (None, None))
            if bid is None or ask is None or ask - bid > 0.01 + 1e-9 or not 0.05 <= bid < ask <= 0.95:
                continue
            gap = abs((bid + ask) / 2 - f["p"])
            h = pos.get(ex)
            if gap > a.quote_max_gap + 1e-9 or (h and (h["qty"] > a.quote_max_inv or self.value_hold(f, h) or
                                                        self.age_min(h) > a.quote_max_age)):
                continue
            cands.append((gap, ex))
        return {ex for _, ex in sorted(cands)[:a.quote_n]}

    def quotes(self, ex: str, held: dict | None, bid: float, ask: float, inv_cost: float) -> list[dict]:
        """Buy YES at the best bid and NO at 1 - best ask (= sell YES at the ask). Inventory exits passively through the
        other side's quote (sized up by the holding), not by crossing the spread like the normal unwind does."""
        a = self.a
        out = []
        for s in ("yes", "no"):
            px = round(bid if s == "yes" else 1 - ask, 3)
            mine = held["qty"] if held and held["side"] == s else 0
            exit_q = held["qty"] if held and held["side"] != s else 0
            if mine >= a.quote_max_inv or (not exit_q and inv_cost >= a.quote_max) or px < MIN_PX:
                continue
            # Never past --quote-max-inv: over it the market leaves the quote set and the normal unwind crosses the spread.
            out.append({"ex": ex, "side": s, "price": px, "qty": min(a.quote_size, a.quote_max_inv - mine) + exit_q,
                        "role": "Q", "roc": 0.0,
                        "lo": round(px - 0.0026, 4), "hi": round(px + 0.0026, 4)})
        return out

    def offered(self, ex: str, side: str, max_px: float) -> float | None:
        """Shares of `side` offered at <= max_px in the realtime book (an offer of side s at x is a bid for the other
        side at 1 - x). None without a healthy feed, or when the cached best offer isn't max_px: the cache is never
        refetched after a missed batch (the API guide says the last batch on a quiet topic can be lost), so it only
        counts when it agrees with this cycle's REST top."""
        if not self.books.healthy():
            return None
        bids = self.books.side_bids(ex, other(side))
        if not bids or abs((1 - bids[0][0]) - max_px) > 1e-6:
            return None
        return sum(q for p, q in bids if 1 - p <= max_px + 1e-9)

    def rotation(self, tops: dict, pos: dict, cash: float, race_cost: dict) -> dict[str, tuple[int, float]]:
        """ex -> (shares, SIG bid for the held side): value holds to sell at SIG's bid because a value buy on offer
        right now returns >= --rotate-gap more to settlement (9 Oct: "if they increase susqies, go ahead").
        9 Oct 08Z: Delaware Senate R NO (69k, cost 0.880, bid 0.895) had 9.9% left to settlement while RI Senate R NO
        was offered at 0.840 (18%). Only holds whose bid is >= avg - --rotate-max-loss, only as much as better offers
        can absorb (realtime book depth at the best offer, within the value caps, less free cash above the value
        floor), at most --rotate-chunk per hold per cycle; holds with the least return left go first."""
        a = self.a
        if a.no_rotate or a.no_value:
            return {}
        targets, race_room = [], {}
        now = datetime.now(timezone.utc)
        for ex, f in self.fair.items():
            if f.get("sig"):
                continue
            bid, ask = tops.get(ex, (None, None))
            held = pos.get(ex)
            for s in ("yes", "no"):
                if held and held["qty"] >= 10 and held["side"] != s:
                    continue
                # Since 13 Oct: a value buy SIG answered as a no-op (our resting dips/catchers on the exchange already
                # commit the balance) can't spend a sale. 13 Oct 00:01-00:24Z rotation sold 8.9k Arkansas Sen D NO at
                # 0.89 (Kalshi 0.94, ~-0.45k) for value buys on 1076/1043 that both came back no-ops.
                t_no = self._v_noop.get((ex, s))
                if t_no is not None and (now - t_no).total_seconds() < 60 * a.rotate_noop_min:
                    continue
                best_s = ask if s == "yes" else (1 - bid if bid is not None else None)
                if best_s is None:
                    continue
                worth = (f["p"] if s == "yes" else 1 - f["p"]) - f["buffer"]
                roc = (worth - best_s) / best_s
                ctrl = self.is_control(ex)
                take_roc = a.control_take_roc if ctrl else a.value_take_roc
                if roc < max(take_roc, a.value_min_roc) - 1e-9 or worth - best_s < a.value_edge - 1e-9:
                    continue   # value_buys only takes offers this cheap; others would leave the cash idle
                held_cost = held["qty"] * held["avg"] if held and held["side"] == s else 0.0
                room = min(a.value_max_pos - held_cost, self.race_cap(ex) - race_cost[self.race_of(ex)])
                depth = self.offered(ex, s, best_s)
                if room <= 0 or not depth:
                    continue
                race = self.race_of(ex)
                race_room[race] = self.race_cap(ex) - race_cost[race]
                targets.append((roc, race, min(room, depth * best_s), ctrl))
        if not targets:
            return {}
        holds = []
        for ex, h in pos.items():
            f = self.fair.get(ex)
            if not f or h["qty"] < 10 or not self.value_hold(f, h):
                continue
            bid, ask = tops.get(ex, (None, None))
            bid_s = bid if h["side"] == "yes" else (None if ask is None else round(1 - ask, 3))
            if bid_s is None or bid_s < h["avg"] - a.rotate_max_loss - 1e-9:
                continue
            worth = (f["p"] if h["side"] == "yes" else 1 - f["p"]) - f["buffer"]
            holds.append(((worth - bid_s) / bid_s, ex, bid_s))
        # Free cash above the value floor goes into the better offers first; below it, sales also refill the floor
        # (the quotes and spike catchers' working cash), so value buys can spend what was sold.
        spare = cash - self.value_floor()
        if spare < 0 and self.value_floor() >= a.busy_cash > 0:
            return {}   # under the busy-hour floor value buys can't spend a sale; busy_prep alone refills it
        if spare < 0 and not a.rotate_refill:
            # Since 12 Oct: no floor refills. 11-12 Oct they sold fresh catches and holds at SIG's bid right after a
            # sweep drained the cash (Delaware Sen R NO at 0.88 and RI Sen D YES at 0.865-0.87 at 00:06-00:38Z,
            # ~22.5k shares, ~-2.4k at Kalshi fair, 14:52Z alike) for off-hours cash that busy_prep refills at 15Z.
            return {}
        used, out = 0.0, {}
        for r_h, ex, bid_s in sorted(holds):
            race = self.race_of(ex)
            by_race = defaultdict(float)   # the D and R legs of a race share its --value-max-race room
            only_ctrl = True
            for roc, rc, c, ctrl in targets:
                if rc != race and roc >= r_h + a.rotate_gap - 1e-9:
                    only_ctrl = False
                # A control buy is a bet that one player's push reverts (then the cash comes back at fair - 0.01 and
                # value buys re-buy the sold hold at its bid), so it may fund from holds returning up to
                # --control-rotate-slack more; holding it to settlement instead costs about that slack.
                if rc != race and (roc >= r_h + a.rotate_gap - 1e-9 or
                                   (ctrl and roc >= r_h - a.control_rotate_slack - 1e-9)):
                    by_race[rc] += c
            cap = sum(min(c, race_room[rc]) for rc, c in by_race.items())
            # Control buys alone never make rotation refill the cash floor: they are funded only while cash is at or
            # above it (not in busy hours under --busy-cash, when value buys couldn't spend the sale anyway).
            if only_ctrl and by_race and spare < 0:
                continue
            need = min(a.rotate_chunk, cap - spare - used)
            if need <= 0:
                continue
            q = min(int(pos[ex]["qty"]), int(need / bid_s) + 1)
            out[ex] = (q, bid_s)
            used += q * bid_s
        self._prep_ex.update(out)   # fair value given up on purpose: prep_allowance keeps --max-dd from counting it
        return out

    def followups(self, tops: dict, pos: dict, cash: float, race_cost: dict) -> tuple[dict, dict]:
        """(targets, sales). Dump follow-ups (since 22 Oct): a player dumping a favourite sweeps its bids and leaves it
        offered cheap. 12-22 Oct tape: 20 such dumps (17 of them 13-15 Oct), ~411k of edge at Kalshi fair, we caught
        ~10k because the cash sat in holds (free cash 0-2k in most of them; Hawaii Gov 21 Oct, 572k shares in one
        sweep, took 12.5k on 25.7k cash). In 18 of 20 the favourite stayed offered >= 0.05 under fair for 5-29 of the
        next 30 minutes, and Kalshi didn't move after them (mean +30-60 min markout -0.0003), so there is time to sell
        a hold and take the offer. targets: ex -> {"side", "px", "roc", "cost"} for a side offered >= --followup-dip
        under SIG's own level (anchor) and >= --followup-roc under fair, sized to the realtime book's depth at that
        offer and the value caps. sales: ex -> (shares, SIG bid) of value holds >= --followup-min-age old whose bid
        clears avg - --rotate-max-loss and that return >= --followup-gap less than the target (least return first),
        only as much as the targets need beyond the cash above --hard-floor. Unlike rotation (off since 17 Oct): the
        buy is a real dump checked against depth, our other buys there are dropped so the take can't come back a
        no-op, and fresh catches are never sold."""
        a = self.a
        if a.no_followup or a.no_value or self.pre_night():
            return {}, {}
        now = datetime.now(timezone.utc)
        targets = {}
        for ex, f in self.fair.items():
            if f.get("sig") or f.get("override") or self.is_control(ex):
                continue
            anc = self.anchor(ex)
            bid, ask = tops.get(ex, (None, None))
            if anc is None or bid is None or ask is None:
                continue
            held = pos.get(ex)
            for s in ("yes", "no"):
                if held and held["qty"] >= 10 and held["side"] != s:
                    continue
                t_no = self._v_noop.get((ex, s))
                if t_no is not None and (now - t_no).total_seconds() < 60 * a.rotate_noop_min:
                    continue
                best_s = ask if s == "yes" else round(1 - bid, 3)
                lvl = anc if s == "yes" else 1 - anc
                worth = (f["p"] if s == "yes" else 1 - f["p"]) - f["buffer"]
                roc = (worth - best_s) / best_s
                if best_s > lvl - a.followup_dip + 1e-9 or roc < a.followup_roc - 1e-9:
                    continue
                held_cost = held["qty"] * held["avg"] if held and held["side"] == s else 0.0
                room = min(a.followup_max, a.value_max_pos - held_cost,
                           self.race_cap(ex) - race_cost[self.race_of(ex)])
                depth = self.offered(ex, s, best_s)
                if room < 100 * best_s or not depth:
                    continue
                cost = min(room, depth * best_s)
                if not targets.get(ex) or roc > targets[ex]["roc"]:
                    targets[ex] = {"side": s, "px": round(best_s, 3), "roc": round(roc, 4), "cost": cost}
        if not targets:
            return {}, {}
        holds = []
        for ex, h in pos.items():
            f = self.fair.get(ex)
            if ex in targets or not f or h["qty"] < 10 or not self.value_hold(f, h) or \
                    self.age_min(h) < a.followup_min_age:
                continue
            bid, ask = tops.get(ex, (None, None))
            bid_s = bid if h["side"] == "yes" else (None if ask is None else round(1 - ask, 3))
            if bid_s is None or bid_s < h["avg"] - a.rotate_max_loss - 1e-9:
                continue
            worth = (f["p"] if h["side"] == "yes" else 1 - f["p"]) - f["buffer"]
            holds.append(((worth - bid_s) / bid_s, ex, bid_s))
        spare = max(0.0, cash - a.hard_floor)
        used, sales = 0.0, {}
        for r_h, ex, bid_s in sorted(holds):
            race = self.race_of(ex)
            need = sum(t["cost"] for tx, t in targets.items()
                       if self.race_of(tx) != race and t["roc"] >= r_h + a.followup_gap - 1e-9) - spare - used
            need = min(a.rotate_chunk, need)
            if need <= 0:
                continue
            q = min(int(pos[ex]["qty"]), int(need / bid_s) + 1)
            sales[ex] = (q, bid_s)
            used += q * bid_s
        self._prep_ex.update(sales)
        return targets, sales

    def anchor(self, ex: str) -> float | None:
        """SIG's own recent Yes level: median of the book mids sampled each cycle over the last --dip-window-min
        (gapped books skipped), so one sweep or a book that refills low for a few minutes can't drag it. None until
        --dip-min-obs samples exist (1 in a dry run)."""
        h = self._mid_hist.get(ex)
        if not h or len(h) < (self.a.dip_min_obs if self.a.live else 1):
            return None
        v = sorted(m for _, m in h)
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    def dips(self, ex: str, f: dict, held: dict | None, bid, ask) -> list[dict]:
        """Dip ladder: buys on both sides at SIG's own recent level for the side (anchor) minus each --dip-levels
        distance. Replay of the 3-4 Oct tapes (143 exchanges, an order fills whenever a print goes through its price,
        valued at the side's level 15-45 min later less 0.005 to exit): 20k-share orders at 0.02 under the level lost
        -0.5c/share (half of a dip is a real move), 0.03 ~0, 0.04 +0.75c, 0.05 +1.4c, 0.06 +1.55c; ~12.6k/day in all,
        3/4 of it 16-24Z. The old Kalshi-anchored catchers (also >= 0.04 under SIG's mid) matched ~4 fills a day,
        which is what live runs showed on 4-5 Oct. Kalshi/Polymarket only guard: never more than --dip-max-over-fair
        above fair. Full size from --dip-full-hours on config/busy_markets.json (80% of traded notional), else
        scaled down. Fills exit through the unwind logic (SIG's level, never under cost)."""
        a = self.a
        anchor = self.anchor(ex)
        if anchor is None:
            return []
        lo_h, hi_h = (int(x) for x in a.dip_full_hours.split("-"))
        mult = (1.0 if lo_h <= utcnow().hour < hi_h else a.dip_off_mult) * (1.0 if ex in self.busy else a.dip_quiet_mult)
        out = []
        for s in ("yes", "no"):
            lvl = anchor if s == "yes" else 1 - anchor
            if held and held["side"] == s and held["qty"] * held["avg"] >= a.dip_max_pos:
                continue
            fair_s = None if f.get("sig") else (f["p"] if s == "yes" else 1 - f["p"])
            best_s = ask if s == "yes" else (1 - bid if bid is not None else None)   # cheapest offer of side s
            for i, d in enumerate(parse_levels(a.dip_levels)):
                px = tick_floor(lvl - d)
                if not a.dip_min_px <= px <= a.dip_max_px or (best_s is not None and px >= best_s - 1e-9):
                    continue
                if fair_s is not None and px > fair_s + a.dip_max_over_fair + 1e-9:
                    continue
                qty = int(min(a.dip_shares * mult, a.dip_notional * mult / px))
                if qty < 100:
                    continue
                # Kept within a tick either way (levels are 0.01 apart; diff gives each order to the closest level),
                # so the anchor's 0.0025 steps don't cost the order its queue place.
                out.append({"ex": ex, "side": s, "price": px, "qty": qty, "role": f"D{i + 1}",
                            "prio": (0 if ex in self.busy else 10) + i,
                            "roc": round((lvl - px) / px, 4), "lo": round(px - 0.0051, 4), "hi": round(px + 0.0051, 4)})
        return out

    def open_cap(self, ex: str, side: str) -> float:
        """Highest price an opening buy (ladder or sweep catcher) of `side` may rest at: SIG's own smoothed mid for that
        side minus --open-under-mid. 4 Oct: cash sat at 6-8k for 8 h with 138k in 37 positions bought at SIG's own level
        (WA-03 R No 36k at 0.795, SIG 0.797, Kalshi/Poly 0.905; NH Senate, Iowa Gov alike). Where SIG sits under
        Kalshi for hours, a bid at fair - 0.10 is just SIG's market, not a sweep: it fills, and the exit (SIG mid +
        0.02, never under cost) never does, so the cash waits for certification. Under SIG's mid a fill is a real dip
        and recycles within minutes. No smoothed mid yet: no cap."""
        m = self._mid_ema.get(ex)
        if m is None or self.a.open_under_mid <= 0:
            return 1.0
        return (m if side == "yes" else 1 - m) - self.a.open_under_mid + 1e-9

    def jump(self, ex: str, side: str, px: float, ceiling: float) -> float:
        """Catcher price one tick above the highest wall of other players' bids within --jump-max above `px` (never
        above `ceiling`). A sweep fills the best bids first, and on 4 Oct a median 300k shares of other bids sat ahead
        of our best catcher (others rest ~360M shares of bids), so most sweeps never reached us. Books come from the
        realtime cache (no reads); own orders are not walls. No healthy feed or no book: `px` unchanged."""
        a = self.a
        if a.jump_max <= 0 or not self.books.healthy():
            return px
        bids = self.books.side_bids(ex, side)
        if not bids:
            return px
        mine = defaultdict(float)
        for o in self._open_by_ex.get(ex, []):
            if o.get("side") == side and o.get("action", "buy") == "buy":
                mine[round(float(o.get("priceLimit") or o.get("price") or 0), 3)] += float(o.get("quantity") or 0)
        top = min(px + a.jump_max, ceiling - TICK)
        for p, q in bids:   # best first: the first wall inside the window is the highest
            if p < px - 1e-9:
                break
            if p <= top + 1e-9 and q - mine.get(round(p, 3), 0.0) >= a.jump_wall:
                return round(p + TICK, 3)
        return px

    def premium(self, ex: str, f: dict, held: dict | None, bid, ask) -> list[dict]:
        """Long-shot sweep catchers. Some players dump whole balances into long shots (RI Senate R, 2 Oct 13:18:52Z:
        one market buy of 591k Yes swept the book 0.10 -> 0.23, ~100k SUSQies, while Kalshi sat at 0.008). On
        markets whose long shot is under --prem-max-fair, offer it at SIG's own smoothed level + each --prem-levels
        distance (= buy the favourite at 1 - that). premium_exit then buys the long shot back at SIG's level, so the
        cash returns within minutes instead of sitting until certification. Kalshi/Polymarket fair only."""
        a = self.a
        p = f["p"]
        if a.no_premium or f.get("sig") or min(p, 1 - p) >= a.prem_max_fair or ex not in self._mid_ema:
            return []
        fav = "no" if p < 0.5 else "yes"
        fair_f = 1 - p if fav == "no" else p
        mid_yes = self._mid_ema[ex]
        long_mid = mid_yes if fav == "no" else 1 - mid_yes
        long_bid = bid if fav == "no" else (1 - ask if ask is not None else None)
        if held and held["side"] == fav and held["qty"] * held["avg"] >= a.prem_max_pos:
            return []   # resting size isn't capped by cash: SIG checks cash at match time, so fills stop at the balance
        # Lottery buyers come back to markets SIG already prices above Kalshi: those get the budget first.
        premium = long_mid - (1 - fair_f)
        sizes = parse_levels(a.prem_shares)
        out = []
        higher = None
        for i, d in enumerate(parse_levels(a.prem_levels)):
            long_px = tick_ceil(max(long_mid, 1 - fair_f) + d)
            if long_bid is not None and long_bid >= long_px - 1e-9:
                continue   # would cross the book on placement
            px = round(1 - long_px, 3)
            if not MIN_PX <= px <= MAX_PX:
                continue
            ceiling = min(fair_f - a.jump_keep_edge, self.open_cap(ex, fav),
                          1 - long_bid - TICK if long_bid is not None else 1.0,
                          higher - 0.02 if higher is not None else 1.0)
            jpx = self.jump(ex, fav, px, ceiling)
            qty = int(min(sizes[min(i, len(sizes) - 1)], a.prem_notional / jpx))
            if qty < 100:
                continue
            higher = jpx
            lo, hi = (jpx - 0.001, jpx + 0.006) if jpx != px else (px - 0.015, px + 0.015)
            out.append({"ex": ex, "side": fav, "price": jpx, "qty": qty, "role": f"P{i + 1}", "prio": -premium,
                        "jumped": round(jpx - px, 3), "roc": round((fair_f - jpx) / jpx, 4),
                        "lo": round(lo, 4), "hi": round(hi, 4)})
        return out

    def costly_exit(self, ex: str, fair_s: float, buy_px: float, cash: float) -> bool:
        """True if exiting held side (worth fair_s) by buying the other side at buy_px gives back more than
        --costly-exit per share vs Kalshi fair while cash isn't short. 2-4 Oct: opens earned +198k vs Kalshi, exits
        gave back -148k, -135k of it in exits >= 0.06/share (mostly long shots bought back at SIG's 0.13-0.16 vs
        Kalshi's 0.01) and -65k of that with >= 40k cash idle. Freeing cash costs ~12% of it, about what opens
        earn on it, so it only pays when cash is actually short; otherwise hold (settlement pays the Kalshi edge)."""
        return ex in self.fair and cash >= self.a.costly_exit_cash and fair_s - (1 - buy_px) > self.a.costly_exit + 1e-9

    def premium_exit(self, ex: str, f: dict, held: dict, cash: float, age: float = 0.0) -> dict | None:
        """Exit a long-shot market's favourite at SIG's own long-shot level (not Kalshi's, which SIG never trades
        at) when that still clears avg + --prem-exit-gain (a freed position: avg - --free-max-loss); otherwise None
        and the normal unwind holds it."""
        a = self.a
        s = held["side"]
        fair_s = f["p"] if s == "yes" else 1 - f["p"]
        if a.no_premium or f.get("sig") or 1 - fair_s >= a.prem_max_fair or ex not in self._mid_ema:
            return None
        long_mid = self._mid_ema[ex] if s == "no" else 1 - self._mid_ema[ex]
        free = self.freeing(age)
        gain = -a.free_max_loss if free else a.prem_exit_gain
        buy_px = tick_floor(min(1 - held["avg"] - gain, long_mid + a.prem_exit_over))
        if buy_px < MIN_PX or buy_px < long_mid - 0.005 or (not free and self.costly_exit(ex, fair_s, buy_px, cash)):
            return None   # SIG's level doesn't clear the gain, or costs too much with cash idle: hold for settlement
        return {"ex": ex, "side": other(s), "price": buy_px, "qty": held["qty"], "role": "unwind",
                "lo": round(buy_px - 0.0151, 4), "hi": round(buy_px + 0.0051, 4)}

    def freeing(self, age: float) -> bool:
        """Positions this old (minutes) are sold at SIG's level even under cost. 4 Oct the user chose turnover over
        settlement value: 138k sat 8 h in positions SIG prices at cost (worth ~+11.5k at Kalshi in November) while cash
        was 6k and the leaders' sweeps went to others. --free-age-min 0 turns it off."""
        return self.a.free_age_min > 0 and age >= self.a.free_age_min

    def recycle_px(self, ex: str, fair_s: float, held: dict, age: float, cash: float) -> float | None:
        """Exit price (as the other side's buy price) at SIG's own level. SIG prices long shots 2-3c over Kalshi, so
        Kalshi-anchored exits rarely fill and the cash sat until certification (3 Oct: 111k locked, 1k free, while
        ~1.5M/day of sweep edge went to others). Positions older than --recycle-age-min exit at SIG's smoothed mid
        + --recycle-over when that clears avg + --recycle-gain; one older than --dead-age-min with under --dead-edge
        left to settlement may give back up to --dead-loss. None = no recycle price."""
        a = self.a
        if cash >= a.recycle_below or age < a.recycle_age_min or ex not in self._mid_ema:
            return None
        other_mid = self._mid_ema[ex] if held["side"] == "no" else 1 - self._mid_ema[ex]
        # Loss-taking exits only for old positions, so a fresh L1 fill (fair - 0.02) is never dumped under cost.
        dead = fair_s - held["avg"] < a.dead_edge and age >= a.dead_age_min
        free = self.freeing(age)
        gain = a.recycle_gain
        if dead:
            gain = -a.dead_loss
        if free:   # held over --free-age-min: SIG's level, down to --free-max-loss under cost, cash idle or not
            gain = min(gain, -a.free_max_loss)
        px = tick_floor(min(1 - held["avg"] - gain, other_mid + a.recycle_over))
        if not free and self.costly_exit(ex, fair_s, px, cash):   # cash idle: rest at most --costly-exit under fair
            px = tick_floor(1 - fair_s + a.costly_exit)
        return px if px >= MIN_PX else None

    def stubs(self, ex: str, f: dict, held: dict | None, bid, ask) -> list[dict]:
        """Sweep catchers: big resting buys below fair on BOTH sides. Players' market orders run through the book
        (buys default to price 1); on 2 Oct ~2.1M shares traded >15c from fair, up to 147k shares in one trade. Resting
        orders reserve no cash, so these cost nothing until a fill; the unwind logic then exits near fair.
        Since 4 Oct at 0.04/0.06/0.08 under fair (were 0.125/0.18/0.30 + a fixed 0.02): research/fill_model.py on the 24 h to
        4 Oct 11:41Z (123 exchanges) found a buy >= 0.15 under fair never traded through within an hour and >= 0.10 only
        87 times in 69k samples, while 0.04 traded through 15% of hours and 0.04-0.05 earned the most per order-hour
        (Kalshi markout at +30 min kept the full distance: no adverse selection)."""
        a = self.a
        out = []
        # Hot markets (config/hot_markets.json: 25 exchanges carried 96% of the >= 0.10 off-fair edge on 2 Oct, in
        # repeated sweeps of 30k-650k shares) get --hot-mult x the size, the tighter --hot-levels, and no
        # near-certain-side filter (recycle_px returns that cash instead of holding it to certification).
        hot = ex in self.hot
        size = a.hot_mult if hot else 1.0
        for s in ("yes", "no"):
            fair_s = f["p"] if s == "yes" else 1 - f["p"]
            if held and held["side"] == s and held["qty"] * held["avg"] >= (a.hot_max_pos if hot else a.stub_max_pos):
                continue
            # ~75% of off-fair volume since 1 Oct 22Z was takers buying Yes far above fair: our No buys catch those.
            mult = (a.stub_no_mult if s == "no" else 1.0) * size
            # Never cross on placement: a book already 15c+ through fair more likely means a bad fair than a gift.
            best_s = ask if s == "yes" else (1 - bid if bid is not None else None)
            targets = [tick_floor(fair_s - d - f["buffer"]) for d in parse_levels(a.hot_levels if hot else a.stub_levels)]
            targets += [p for p in parse_levels(a.stub_fixed) if fair_s - p >= a.stub_fixed_min_edge]
            last = higher = None
            for i, px in enumerate(sorted(set(targets), reverse=True)):
                # Closest-first spacing: a level must sit >= --stub-spacing under the previous one, and keep windows
                # are under half of it, so they never overlap.
                if px < MIN_PX or (last is not None and last - px < a.stub_spacing - 1e-9) or \
                        (best_s is not None and px >= best_s - 1e-9) or px > self.open_cap(ex, s):
                    continue
                last = px
                ceiling = min(fair_s - f["buffer"] - a.jump_keep_edge, self.open_cap(ex, s),
                              best_s - TICK if best_s is not None else 1.0,
                              higher - 0.02 if higher is not None else 1.0)
                jpx = self.jump(ex, s, px, ceiling)
                roc = (fair_s - jpx) / jpx
                # A favourite bought at 0.84 (fair 0.99) holds its cash until certification, weeks away: on sides
                # worth more than --stub-safe-fair keep only catchers that at least double the cash (e.g. 0.02).
                if not hot and fair_s > a.stub_safe_fair and roc < a.stub_safe_roc:
                    continue
                qty = int(min(a.stub_shares * mult, a.stub_notional * mult / jpx))
                if qty < 10:
                    continue
                higher = jpx
                # Window under half the spacing either way, so catcher levels never adopt each other's orders. A ladder
                # order at the same price (L2/L3 sit at 0.04/0.08 too) may swap with a catcher in diff; each is matched
                # once and the total resting at that price is unchanged, so nothing churns. A jumped order must stay
                # above its wall: keep it only within a tick of the new price.
                half = a.stub_spacing / 2 - 0.001
                lo, hi = (jpx - 0.001, jpx + 0.006) if jpx != px else (px - half, px + min(0.01, half))
                out.append({"ex": ex, "side": s, "price": jpx, "qty": qty, "role": f"S{i + 1}", "hot": hot,
                            "jumped": round(jpx - px, 3), "roc": round(roc, 4), "lo": round(lo, 4), "hi": round(hi, 4)})
        return out

    def diff(self, want: list[dict], have: list[dict], now: datetime):
        """-> (keep, cancel, place). Existing orders inside a wanted order's window are kept."""
        by_key = defaultdict(list)
        for o in have:
            by_key[(str(o["exchangeId"]), o["side"])].append(o)
        keep, place, used = [], [], set()
        refresh_before = now + timedelta(minutes=self.a.refresh_min)
        for w in sorted(want, key=lambda w: (w["role"] != "unwind", w["role"][0] == "L")):
            # Closest price wins: hot catchers at fair - 0.10 sit inside L3's keep window, and a first-fit match let
            # L3 adopt the catcher's order and churn both every cycle.
            match, best = None, None
            for o in by_key.get((w["ex"], w["side"]), []):
                if o["id"] in used or o.get("action") != "buy":
                    continue
                px = float(o.get("priceLimit") or o.get("price") or 0)
                exp = o.get("expirationDate")
                if exp and datetime.fromisoformat(exp.replace("Z", "+00:00")) < refresh_before:
                    continue
                rem = float(o.get("quantity") or 0)
                # SIG shrinks every resting buy to the cash balance (9 Oct: 26 orders sat at exactly the 15,689 cash),
                # so an order as big as the balance allows counts as full size: re-placing it would lose its queue
                # place and usually come back a no-op (see noop_blocked).
                full = min(w["qty"], 0.95 * self.last_cash / max(px, 0.005)) if self.last_cash else w["qty"]
                # Dips shrink outside --dip-full-hours, so a day-size dip order isn't kept overnight.
                if w["lo"] - 1e-9 <= px <= w["hi"] + 1e-9 and (rem >= 0.3 * w["qty"] or rem >= full) and \
                        (w["role"] != "unwind" or rem <= w["qty"] + 1e-9) and \
                        (w["role"][0] != "D" or rem <= 1.5 * w["qty"]) and \
                        (best is None or abs(px - w["price"]) < best):
                    match, best = o, abs(px - w["price"])
            if match:
                used.add(match["id"])
                keep.append((w, match))
            else:
                place.append(w)
        cancel = [o for o in have if o["id"] not in used and (not self.a.coexist or o["id"] in self._mine)]
        return keep, cancel, place

    def value_floor(self) -> float:
        """Cash value buys must leave free: --value-min-cash in --dip-full-hours (room for a 40k busy-market dip),
        --value-min-cash-off otherwise. 6 Oct 04:30Z-10:30Z sat idle on 49.6k cash under the 60k floor."""
        if self.pre_night():
            return float("inf")
        lo_h, hi_h = (int(x) for x in self.a.dip_full_hours.split("-"))
        busy = lo_h <= utcnow().hour < hi_h
        # Cash freed for busy hours isn't spent on new holds in the prep hour or the busy hours themselves (since
        # 11 Oct: "free cash ... for busy hours"); dips and spike catchers take it at fill time.
        if self.in_prep() or (busy and self.a.busy_cash > 0):
            return max(self.a.value_min_cash, self.a.busy_cash)
        return self.a.value_min_cash if busy else self.a.value_min_cash_off

    def budget(self, place: list[dict], have_kept: list, cash: float) -> list[dict]:
        """Cap total resting ladder notional at --overcommit x cash, sweep catchers separately at --stub-overcommit x
        cash; unwinds are never capped."""
        floor = self.a.hard_floor if cash < self.a.reserve else self.a.reserve   # scarce: only high-roc orders reach here
        cap = {"L": self.a.overcommit * max(0.0, cash - floor),
               "S": self.a.stub_overcommit * max(0.0, cash - self.a.hard_floor),
               "P": self.a.prem_overcommit * max(0.0, cash - self.a.hard_floor),
               # Hot catchers get a pool of their own: kept ordinary catchers filled the shared one at low cash.
               "H": self.a.stub_overcommit * max(0.0, cash - self.a.hard_floor),
               "D": self.a.dip_overcommit * max(0.0, cash - (max(self.a.hard_floor, self.a.busy_cash)
                                                               if self.in_prep() else self.a.hard_floor)),
               # Value buys fill at once and hold to settlement: no overcommit, never below --value-min-cash
               # (--value-min-cash-off outside --dip-full-hours, when dips are small and cash sat idle).
               "V": max(0.0, cash - self.value_floor()),
               # Spike catchers rest like the long-shot catchers (no cash until a fill, capped at the cash then).
               "K": 0.0 if self.pre_night() else self.a.spike_overcommit * max(0.0, cash - self.a.hard_floor),
               "Q": self.a.quote_max}
        if utcnow() >= NO_DIPS_AFTER:
            cap["D"] = 0.0

        # Dump follow-ups spend down to the hard floor (rather than the value floor kept for catches: they are one)
        # and come out of the same cash as value buys, before them.
        cap["F"] = max(0.0, cash - self.a.hard_floor)

        def kind_of(w):
            return "F" if w.get("fu") else "H" if w.get("hot") else w["role"][0]

        used = defaultdict(float)
        for w, o in have_kept:
            if w["role"] != "unwind":   # what actually rests (value buys are shrunk to the pool, SIG shrinks to cash)
                used[kind_of(w)] += min(w["qty"], float(o.get("quantity") or w["qty"])) * w["price"]
        out = []
        # Cheapest-per-share first within the stubs: the 0.02 catchers cost little and carry the biggest edge.
        # Long-shot catchers: biggest SIG premium first (lottery buyers return there), then level.
        # Dips: busy markets first, then the closest level.
        for w in sorted(place, key=lambda w: (w["role"] != "unwind", w["role"][0], w["price"] if w["role"][0] == "S" else 0,
                                              w.get("prio", 0), w["role"])):
            cost = w["qty"] * w["price"]
            if w["role"] != "unwind":
                kind = kind_of(w)
                if kind == "V":
                    cap["V"] = min(cap["V"], max(0.0, cap["F"] - used["F"]))
                if kind in "VF" and used[kind] + cost > cap[kind]:
                    # Value buys are sized to their caps (up to 40k since 9 Oct); spend what the pool has left
                    # rather than nothing.
                    q = int((cap[kind] - used[kind]) / w["price"])
                    if q >= 100:
                        w = {**w, "qty": q}
                        cost = q * w["price"]
                if used[kind] + cost > cap[kind]:
                    continue
                used[kind] += cost
            out.append(w)
        return out

    # ------------------------------------------------------------------ execution
    def cancel(self, orders: list[dict]) -> int:
        if not orders or not self.a.live:
            return 0
        per_ex = defaultdict(list)
        for o in orders:
            per_ex[str(o["exchangeId"])].append(o)

        def one(item):
            ex, os_ = item
            try:
                if len(os_) >= 3 and self.all_mine_on(ex, os_):
                    self.c.post("/orders/cancel-all", {"exchangeId": ex, "tournamentId": self.tid})
                else:
                    for o in os_:
                        self.c.delete(f"/orders/{o['id']}")
                return len(os_)
            except SigAPIError as e:
                if e.status not in (404, 409):   # 409 "already closed": filled or expired meanwhile
                    self.event({"kind": "cancel_error", "ex": ex, "error": f"{e.status} {e.code} {e.message[:120]}"})
            except Exception as e:  # noqa: BLE001
                self.event({"kind": "cancel_error", "ex": ex, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            return 0

        with ThreadPoolExecutor(self.a.writers) as pool:
            return sum(pool.map(one, per_ex.items()))

    def all_mine_on(self, ex: str, cancelling: list[dict]) -> bool:
        return len(cancelling) == len(self._open_by_ex.get(ex, []))

    def noop_blocked(self, w: dict, cash: float, now: datetime) -> bool:
        """SIG answers ok with orderId null ("accepted but nothing placed") when our resting buys on that exchange
        already commit the balance. 9 Oct 04:31Z run: 39-50 of those every 20-s cycle for 2 h (mostly the 2nd/3rd
        long-shot catcher on ~20 exchanges), each counted as placed and re-sent next cycle. Such an order is held back
        until cash rises, one of our orders on the exchange goes, or --noop-retry-min passes."""
        k = (w["ex"], w["side"], w["role"])
        r = self._noop.get(k)
        if r is None:
            return False
        if (now - r["at"]).total_seconds() > 60 * self.a.noop_retry_min or cash > r["cash"] * 1.05 + 500 or \
                len(self._open_by_ex.get(w["ex"], [])) < r["n"]:
            del self._noop[k]
            return False
        return True

    def place(self, items: list[dict], now: datetime) -> dict:
        st = {"sent": 0, "ok": 0, "noop": 0, "failed": 0, "traded": 0.0, "errors": [], "noops": []}
        if not items or not self.a.live:
            return st
        chunks = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
        # SIG takes 20-90 s per batch under load; latency, not the 30 writes/min budget, is the bottleneck.
        with ThreadPoolExecutor(self.a.writers) as pool:
            for chunk, res in zip(chunks, pool.map(lambda ch: self._send_batch(ch, now), chunks)):
                if isinstance(res, str):
                    st["failed"] += len(chunk)
                    st["errors"].append(res)
                    continue
                st["sent"] += len(chunk)
                # Each result names its order by `index` (API guide); order is preserved today, but don't rely on it.
                results = res.get("results", [])
                if all(isinstance(r.get("index"), int) and 0 <= r["index"] < len(chunk) for r in results):
                    pairs = [(r, chunk[r["index"]]) for r in results]
                else:
                    pairs = list(zip(results, chunk))
                for r, w in pairs:
                    d = r.get("data") or {}
                    if r.get("ok"):
                        t = float(d.get("quantityTraded") or 0)
                        if d.get("orderId") is None and not t:   # no-op: the exchange's resting buys commit the balance
                            st["noop"] += 1
                            st["noops"].append([w["role"], w["ex"], w["side"], w["price"], int(w["qty"])])
                            self._noop[(w["ex"], w["side"], w["role"])] = {
                                "at": now, "cash": self.last_cash or 0.0, "n": len(self._open_by_ex.get(w["ex"], []))}
                            if w["role"] == "V":
                                self._v_noop[(w["ex"], w["side"])] = now
                            continue
                        st["ok"] += 1
                        if d.get("orderId") is not None:
                            self._mine.add(d["orderId"])
                        st["traded"] += t
                        if t:
                            self.event({"kind": "fill_on_place", "ex": w["ex"], "side": w["side"],
                                        "price": w["price"], "qty": t, "role": w["role"],
                                        "title": self.markets[w["ex"]]["title"]})
                    else:
                        st["failed"] += 1
                        st["errors"].append(json.dumps(d.get("error") or d)[:160])
        return st

    def _send_batch(self, chunk: list[dict], now: datetime):
        """-> batch response dict, or an error string. Sweep and long-shot catchers get the longer --stub-expiry-min
        (re-placing them every ~2 h gave up their place in the queue at prices sweeps reach only now and then)."""
        def expiry(w):
            return iso(now + timedelta(minutes=self.a.stub_expiry_min if w["role"][0] in "SPDK" else self.a.expiry_min))
        orders = [{"exchangeId": w["ex"], "side": w["side"], "action": "buy", "quantity": int(w["qty"]),
                   "price": w["price"], "tournamentId": self.tid, "expirationDate": expiry(w)} for w in chunk]
        t0 = time.monotonic()
        try:
            return self.c.post("/orders/batch", {"idempotencyKey": new_idempotency_key(), "orders": orders})
        except SigAPIError as e:
            if isinstance(e.details, dict) and "results" in e.details:
                return e.details
            return f"{e.status} {e.code}: {e.message[:160]}"
        except Exception as e:  # noqa: BLE001 - one slow batch must not abort the others
            return f"{type(e).__name__}: {str(e)[:160]}"
        finally:   # write latency per batch (election night sizing: ELECTION_NIGHT.md); one list append is thread-safe
            self.batch_ms.append((utcnow().hour, int((time.monotonic() - t0) * 1000), len(chunk)))

    def cancel_everything(self, why: str) -> bool:
        """-> True once SIG confirms nothing is resting (cancel-all returns 200 only then)."""
        self.event({"kind": "cancel_all", "why": why})
        print(f"[{utcnow():%H:%M:%S}Z] cancel all: {why}", flush=True)
        if self.a.live:
            try:
                self.c.post("/orders/cancel-all", {"tournamentId": self.tid})
                return True
            except SigAPIError as e:
                print(f"  cancel-all failed: {e}", flush=True)
        return False

    def event(self, d: dict):
        d = {"ts": iso(utcnow()), **d}
        with self._event_lock, self.log_path.open("a", encoding="utf-8") as f:   # the sniper thread logs too
            f.write(json.dumps(d, default=str) + "\n")

    def refresh_busy(self):
        """config/busy_markets.json is a fixed 3-4 Oct ranking; rebuild it from the trades the last day's runs saw on
        the realtime feed (each run's summary on run-logs carries `feed_trades`), so dips follow where the shares trade.
        Ranked by shares (dip orders are capped in shares; on the 3-4 Oct replay the top 40 by shares held 99.8% of the
        dip profit). Needs --busy-min-hours of feed coverage, otherwise the static list stays."""
        a = self.a
        try:
            if subprocess.run(["git", "fetch", "-q", "origin", "run-logs"], cwd=ROOT, timeout=60).returncode:
                return
            names = subprocess.run(["git", "ls-tree", "-r", "--name-only", "FETCH_HEAD", "maker_logs/"], cwd=ROOT,
                                   capture_output=True, text=True, timeout=60).stdout.split()
            cutoff = utcnow() - timedelta(hours=24)
            shares, hours, runs = defaultdict(float), 0.0, 0
            for n in names:
                m = re.search(r"maker_(\d{8}T\d{6}Z)\.json$", n)
                if not m or datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) < cutoff:
                    continue
                txt = subprocess.run(["git", "show", f"FETCH_HEAD:{n}"], cwd=ROOT, capture_output=True, text=True,
                                     timeout=60).stdout
                ft = (json.loads(txt) if txt else {}).get("feed_trades")
                if not ft or not ft.get("since"):
                    continue
                since, until = (datetime.fromisoformat(ft[k].replace("Z", "+00:00")) for k in ("since", "until"))
                hours += (until - since).total_seconds() / 3600
                runs += 1
                for ex, (_, q) in ft["by_ex"].items():
                    shares[ex] += q
        except Exception as e:  # noqa: BLE001 - the static list is a fine fallback
            self.event({"kind": "busy_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            return
        if hours < a.busy_min_hours:
            self.event({"kind": "busy", "source": "static", "feed_hours": round(hours, 1)})
            return
        self.busy = set(sorted((ex for ex in shares if ex in self.markets), key=lambda ex: -shares[ex])[:a.busy_top])
        self.busy_source = f"feed trades, {runs} runs / {hours:.0f} h"
        self.event({"kind": "busy", "source": self.busy_source, "exchanges": sorted(self.busy)})

    # ------------------------------------------------------------------ loop
    def start_feed(self):
        """SIG's realtime books for all 237 markets (live runs). The maker owns the feed: the book cache serves the
        catchers' wall jumps and the sniper (which then polls only every --snipe-poll-rt s), and costs no reads."""
        if not self.a.live or self.a.no_realtime:
            return
        try:
            from sigbot.realtime import RealtimeFeed
            mids = sorted({m["market"] for m in self.markets.values()})
            self.feed = RealtimeFeed(SigClient(log=False), self.tid, mids, self.on_batch,
                                     extra_topics=[] if self.a.no_lbtrack else ["leaderboard:all"])
            self.feed.start()
            self.feed_since = utcnow()
        except Exception as e:  # noqa: BLE001 - polling still runs
            self.event({"kind": "realtime_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            return
        if not self.a.no_lbtrack:   # 1d leaderboard steps vs the tape: what the leaders trade (leader_study.py)
            try:
                lc = SigClient(log=False)
                lc.read_limiter = RateLimiter(self.a.lb_reads)
                self.lbpoll = LeaderPoller(lc, self.event, every_s=self.a.lb_every, active=self.books.healthy)
                self.lbpoll.start()
            except Exception as e:  # noqa: BLE001
                self.event({"kind": "lb_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def rebalance_reads(self):
        """The sniper needs --snipe-reads/min while it must poll REST every scan, only --snipe-reads-rt while the feed
        is live; the maker gets the rest. (4 Oct: the cloud runs' feed never connected, 5,962 failed connects in one
        run, so the split must follow what the feed actually does.)"""
        lb = self.a.lb_reads if self.lbpoll and self.books.healthy() else 0   # the tracker idles while the feed is down
        if not self.sniper:
            self.c.read_limiter.per_min = READS_PER_MIN - lb
            return
        r = self.a.snipe_reads_rt if self.books.healthy() else self.a.snipe_reads
        self.sniper.c.read_limiter.per_min = r
        self.c.read_limiter.per_min = READS_PER_MIN - r - lb

    def on_batch(self, topic: str, event: str, payload: dict):
        if topic.startswith("leaderboard"):   # joined 8 Oct, pushed nothing in a 100 s probe: log what ever comes
            self._lb_pushes += 1
            if self._lb_pushes <= 200:
                self.event({"kind": "lb_push", "event": event, "payload": json.dumps(payload, default=str)[:4000]})
            return
        if payload.get("trades") and self.lbpoll:   # book top before this batch's books land = before the prints
            self.tape.add(payload["trades"], {str(t.get("exchangeId")): self.books.top(str(t.get("exchangeId")))
                                              for t in payload["trades"]})
        self.books.apply(payload)
        with self._trade_lock:   # one thread per socket
            for t in payload.get("trades") or []:   # {exchangeId, id, price, quantity, executedAt}; batches can repeat
                tid = t.get("id")
                if tid is None or tid in self._trade_ids:
                    continue
                self._trade_ids.add(tid)
                ft = self.feed_trades[str(t.get("exchangeId"))]
                ft[0] += 1
                ft[1] += float(t.get("quantity") or 0)
        if self.sniper:
            self.sniper.on_batch(topic, event, payload)

    def stop_reason(self, now: datetime) -> str | None:
        if os.environ.get("SIGBOT_DISABLED") == "1" or (ROOT / "STOP").exists():
            return "kill switch (SIGBOT_DISABLED=1 or STOP file)"
        if not (OPEN_UTC <= now < CLOSE_UTC - timedelta(minutes=2)):
            return "outside the trading window"
        if now >= ELECTION_HANDOVER:
            return "election night: election.py trades from 3 Nov 21:40Z"
        p = ROOT / "config" / "pause_until_utc.txt"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    if now < datetime.fromisoformat(line.replace("Z", "+00:00")):
                        return f"paused until {line} (config/pause_until_utc.txt)"
                    break
        return None

    def cycle(self, n: int) -> bool:
        a = self.a
        now = utcnow()
        if self.fair_at is None or (now - self.fair_at).total_seconds() > a.fair_every:
            self.refresh_fair()
        if self.fair_at is None or (now - self.fair_at).total_seconds() > a.fair_max_age:
            self.cancel_everything("fair values stale")
            return False
        tops = self.tops()
        t_now = time.time()
        for ex, (b, s_) in tops.items():   # gapped books (mid-sweep) don't move the anchors
            h = self._mid_hist[ex]
            if b is not None and s_ is not None and s_ - b <= 0.05:
                prev = self._mid_ema.get(ex)
                self._mid_ema[ex] = (b + s_) / 2 if prev is None else prev + a.mid_alpha * ((b + s_) / 2 - prev)
                h.append((t_now, (b + s_) / 2))
            while h and h[0][0] < t_now - a.dip_window_min * 60:
                h.popleft()
        have = self.open_orders()
        self._open_by_ex = defaultdict(list)
        for o in have:
            self._open_by_ex[str(o["exchangeId"])].append(o)
        cash_at = utcnow()
        cash, pos = self.book()
        self.last_cash, self.last_cash_at = cash, cash_at
        value = self.value_of(cash, pos)
        if self.start_value is None:
            self.start_value = value
            self._start_pos = pos
        if value + self.prep_allowance(pos) < self.start_value - a.max_dd:
            # 3 Oct 02:40Z: a 14k catcher fill landed between the positions and cash reads, the run saw a 15k
            # "drawdown" and cancelled everything. Re-read before believing it.
            cash2, pos2 = self.book(tries=4)
            value2 = self.value_of(cash2, pos2)
            self.event({"kind": "drawdown_check", "value": value, "recheck": value2, "start": self.start_value})
            if value2 + self.prep_allowance(pos2) < self.start_value - a.max_dd:
                self.cancel_everything(f"drawdown: value {value2:,.0f} (recheck) vs start {self.start_value:,.0f}")
                return False
            cash, pos, value = cash2, pos2, value2
            self.last_cash, self.last_cash_at = cash, utcnow()
        # Below the hard floor the ladder is pulled but the run goes on: unwinds are what bring cash back.
        # Catchers stay resting at any cash (3 Oct: two sweeps took cash to 0, the run cancelled 821 and then 1,059
        # catchers, lost their queue places and spent 13-20 min cancelling/re-placing). Resting orders reserve no
        # cash and a fill stops at the balance, so a kept catcher costs nothing; budget() only limits new ones.
        ladder_on = now < NO_LADDER_AFTER and cash >= a.hard_floor
        want = self.desired(tops, pos, cash, ladder_on, catch_on=now < NO_LADDER_AFTER)
        scarce = cash < a.reserve
        if scarce:   # scarce cash: the ladder only for fills with a high return per SUSQie; catchers keep going
            want = [w for w in want if w["role"] == "unwind" or w["role"][0] in "SPDKQ" or w.get("fu") or
                    w.get("roc", 0) >= a.scarce_roc]
        keep, cancel, place = self.diff(want, have, now)
        if a.live and not a.no_dip and n <= a.dip_min_obs:
            # The dip anchor needs --dip-min-obs cycles of mids; until then the last run's dip orders aren't wanted yet
            # and would lose their queue place, so for those first cycles only possible exits (buys of the side
            # opposite a holding) are cancelled: a stale exit next to a re-placed one could over-close the position.
            cancel = [o for o in cancel if str(o["exchangeId"]) in pos and o["side"] == other(pos[str(o["exchangeId"])]["side"])]
        if a.live and not a.coexist and len(cancel) > a.mass_cancel:
            # One cancel-all write instead of hundreds of deletes at ~21 writes/min (switching to the dip ladder on
            # 5 Oct meant 773 cancels, ~35 min of a blocked cycle); everything wanted is then placed afresh.
            if self.cancel_everything(f"{len(cancel)} orders to cancel (> --mass-cancel {a.mass_cancel})"):
                keep, cancel, place = self.diff(want, [], now)
        held_back = [w for w in place if self.noop_blocked(w, cash, now)]
        if held_back:
            place = [w for w in place if not self.noop_blocked(w, cash, now)]
        place = self.budget(place, keep, cash)
        ncan = self.cancel(cancel)
        st = self.place(place, now)
        resting = sum(w["qty"] * w["price"] for w, _ in keep) + sum(w["qty"] * w["price"] for w in place)
        rt = f" | rt {'live' if self.books.healthy() else 'quiet'} {len(self.books.books)} books" if self.feed else ""
        line = (f"[{now:%H:%M:%S}Z] #{n} cash {cash:,.0f} value {value:,.0f} ({value - self.start_value:+,.0f}) "
                f"pos {len(pos)} | orders keep {len(keep)} cancel {ncan if a.live else len(cancel)} "
                f"place {st['ok'] if a.live else len(place)} (fail {st['failed']}, no-op {st['noop']}, "
                f"held back {len(held_back)}) traded {st['traded']:,.0f} "
                f"| resting ~{resting:,.0f} | quote {len(self._quoting)} | fair {len(self.fair)} "
                f"({max(0, (utcnow() - self.fair_at).total_seconds()):.0f}s old)"
                f"{rt}")
        print(line, flush=True)
        for e in st["errors"][:3]:
            print("   err:", e, flush=True)
        self.totals["traded_on_place"] += st["traded"]
        self.event({"kind": "cycle", "n": n, "cash": cash, "value": value, "positions": pos,
                    "keep": len(keep), "cancel": len(cancel), "place": len(place), "held_back": len(held_back),
                    "quoting": sorted(self._quoting),
                    "roles": {r: sum(1 for w, _ in keep if w["role"][0] == r) + sum(1 for w in place if w["role"][0] == r)
                              for r in "VKQPD"},
                    "stats": st})
        if not a.live:
            by_role = defaultdict(lambda: [0, 0.0, 0.0])
            fairs = {**self.consensus(tops), **self.fair}
            for w in place:
                r = by_role[w["role"]]
                fs = fairs[w["ex"]]["p"] if w["side"] == "yes" else 1 - fairs[w["ex"]]["p"]
                r[0] += 1
                r[1] += w["qty"] * w["price"]
                r[2] += w["qty"] * (fs - w["price"])
            print("DRY RUN would place (role: orders, cash if all filled, edge if all filled):")
            for role, (k, c, e) in sorted(by_role.items()):
                print(f"   {role:<6} {k:>4}  {c:>11,.0f}  {e:>11,.0f}")
            print("   cancel:", len(cancel))
            stubs = [w for w in place if w["role"][0] in "SP"]
            sample = sorted(stubs, key=lambda w: -w["roc"])[:6] + sorted(stubs, key=lambda w: w["roc"])[:3] +                 [w for w in place if w["role"][0] == "P"][:9]
            for w in sorted(place, key=lambda w: w["role"])[:6] + sample:
                f = {**self.consensus(tops), **self.fair}[w["ex"]]["p"]
                print(f"   {w['role']:<6} BUY {w['qty']:>6} {w['side'].upper():<3} @ {w['price']:.3f}  "
                      f"(fair YES {f:.3f})  {self.markets[w['ex']]['title'][:55]}")
            if not a.no_snipe:   # no smoothing history in a one-cycle dry run, so the agreement check is skipped
                opp = Sniper(self).opportunities(tops, utcnow(), require_history=False)
                print(f"DRY RUN sniper would take {len(opp)} (live it also needs SIG's smoothed mid within "
                      f"{a.snipe_agree} of fair):")
                for o in opp[:10]:
                    print(f"   BUY {o['qty']:>6} {o['side'].upper():<3} limit {o['limit']:.3f} best {o['best']:.3f} "
                          f"fair {o['fair_s']:.3f}  {self.markets[o['ex']]['title'][:55]}")
                bk = Sniper(self).baskets(tops)
                print(f"DRY RUN race baskets (YES bids summing to >= 1 + {a.basket_edge}): {len(bk)}")
                for o in bk[:12]:
                    print(f"   {o['race']:<28} bid D {o['bd']:.3f} + bid R {o['br']:.3f} = {1 + o['edge']:.3f}  "
                          f"fair D {self.fair[o['d']]['p']:.3f} R {self.fair[o['r']]['p']:.3f}")
                locked = self.pair_locked(pos, cash)
                print(f"pairs held NO+NO (held, not unwound): {len(locked) // 2} races, "
                      f"{sum(locked.values()) // 2:,} pairs")
        return True

    def run(self):
        a = self.a
        deadline = min(utcnow() + timedelta(minutes=a.minutes), next_slot_end(utcnow()), ELECTION_HANDOVER)
        if a.stop_at:
            deadline = min(deadline, datetime.fromisoformat(a.stop_at.replace("Z", "+00:00")))
        why = self.stop_reason(utcnow())
        if not why and deadline - utcnow() < timedelta(minutes=8):
            why = f"too close to the next scheduled run (would stop at {deadline:%H:%M}Z)"
        if why:
            print(f"Not trading: {why}. No orders touched.")
            return
        self.load_markets(deadline)
        if a.live and not a.no_dip:
            self.refresh_busy()
        self.start_feed()
        quotes = f"levels {a.levels} notional {a.notional}" if a.no_dip else \
            f"dips {a.dip_levels} under SIG's {a.dip_window_min:.0f}-min level, full size {a.dip_full_hours}Z on " \
            f"{len(self.busy)} busy ({self.busy_source})"
        print(f"maker {'LIVE' if a.live else 'DRY RUN'}: {len(self.markets)} exchanges, {quotes}, "
              f"until {deadline:%H:%M}Z, log {self.log_path.name}", flush=True)
        if a.live and not a.no_snipe:   # the sniper gets its own slice of the per-key rate limit
            self.c.read_limiter = RateLimiter(READS_PER_MIN - a.snipe_reads)
            self.c.write_limiter = RateLimiter(WRITES_PER_MIN - a.snipe_writes)
        try:
            self._loop(deadline)
        finally:
            if self.sniper:
                self.sniper.halt.set()
                self.sniper.join(30)
            if self.lbpoll:
                self.lbpoll.halt.set()
                t = self.tape.flush()
                t and self.event(t)
            if self.feed:
                self.feed.stop()
        # Orders are deliberately left resting at the deadline: the next scheduled run adopts them
        # (they keep queue priority) and expirationDate kills them if no run follows.
        self.finish()

    def _loop(self, deadline: datetime):
        a = self.a
        n = 0
        while True:
            n += 1
            t0 = time.time()
            try:
                ok = self.cycle(n)
            except SigAPIError as e:
                print(f"[{utcnow():%H:%M:%S}Z] API error: {e}", flush=True)
                self.event({"kind": "error", "error": str(e)[:300]})
                ok = True
            except Exception as e:  # noqa: BLE001 - network blips must not kill a 2-hour run
                print(f"[{utcnow():%H:%M:%S}Z] error: {type(e).__name__}: {str(e)[:200]}", flush=True)
                self.event({"kind": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"})
                ok = True
            t = self.tape.flush()
            if t:
                self.event(t)
            if not a.live or not ok:
                break
            if self.sniper is None and not a.no_snipe:   # starts once a cycle has produced fresh fair values
                self.sniper = Sniper(self)
                self.sniper.start()
            self.rebalance_reads()
            why = self.stop_reason(utcnow())
            if why:
                self.cancel_everything(why)
                break
            if utcnow() + timedelta(seconds=a.cycle) >= deadline:
                break
            time.sleep(max(1.0, a.cycle - (time.time() - t0)))

    def finish(self):
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        lines = self.log_path.read_text(encoding="utf-8").splitlines() if self.log_path.exists() else []
        cycles = [json.loads(l) for l in lines if '"kind": "cycle"' in l]
        summary = {"run": self.run_ts, "live": self.a.live, "args": vars(self.a), "cycles": len(cycles),
                   "start_value": self.start_value, "end": cycles[-1] if cycles else None,
                   "fills_on_place": [json.loads(l) for l in lines if "fill_on_place" in l],
                   "sniper": dict(self.sniper.stats) if self.sniper else None,
                   "realtime": {**self.feed.summary(), "book_pushes": self.books.pushes,
                                "books_cached": len(self.books.books)} if self.feed else None,
                   "feed_trades": {"since": iso(self.feed_since), "until": iso(utcnow()),
                                   "by_ex": dict(self.feed_trades)}
                   if self.feed_since and self.feed and self.feed.summary().get("batches") else None,
                   "snipes": [json.loads(l) for l in lines if '"kind": "snipe"' in l],
                   "baskets": [json.loads(l) for l in lines if '"kind": "basket"' in l],
                   "lbtrack": {**self.lbpoll.stats, "lb_pushes": self._lb_pushes} if self.lbpoll else None,
                   "batch_latency": latency_stats(self.batch_ms),
                   "events": [json.loads(l) for l in lines
                              if not re.search(r'"kind": "(cycle|tape|lb|lb_push)"', l)][-200:]}
        path = logs / f"maker_{self.run_ts}.json"
        path.write_text(json.dumps(summary, indent=1, default=str))
        if cycles:
            e = cycles[-1]
            print(f"done: {len(cycles)} cycles, value {e['value']:,.0f} "
                  f"({e['value'] - (self.start_value or e['value']):+,.0f} this run), cash {e['cash']:,.0f}, "
                  f"{len(e['positions'])} positions. Log: logs/{path.name}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="actually trade (default: one dry-run cycle)")
    ap.add_argument("--coexist", action="store_true",
                    help="another maker is live on the key: cancel only own orders, leave its resting exits alone")
    ap.add_argument("--remap", action="store_true", help="rebuild the Kalshi map and save config/universe.json")
    ap.add_argument("--minutes", type=float, default=110, help="run length")
    ap.add_argument("--stop-at", help="hard UTC stop time, e.g. 2026-10-02T04:25Z (whichever is first)")
    ap.add_argument("--cycle", type=float, default=20,
                    help="seconds between cycles (30 until 4 Oct; the realtime feed freed the sniper's polling reads)")
    ap.add_argument("--sig-levels", default="0.08,0.15,0.25", help="levels for races priced only by SIG's own book")
    ap.add_argument("--sig-notional", default="600,900,1200")
    ap.add_argument("--sig-alpha", type=float, default=0.15, help="smoothing of the SIG-consensus fair per cycle")
    ap.add_argument("--levels", default="0.02,0.04,0.08", help="ladder distances from fair (before buffers)")
    ap.add_argument("--notional", default="1200,1800,2400", help="SUSQies per ladder order, per level")
    ap.add_argument("--max-shares", type=int, default=20000)
    ap.add_argument("--writers", type=int, default=4, help="order batches in flight at once")
    ap.add_argument("--keep-tighter", type=float, default=0.015, help="keep an order up to this much closer to fair")
    ap.add_argument("--keep-wider", type=float, default=0.03, help="keep an order up to this much further from fair")
    ap.add_argument("--unwind-edge", type=float, default=0.01, help="unwind fills at fair minus this at first")
    ap.add_argument("--max-exit-discount", type=float, default=0.025, help="exits never sell below fair minus this")
    ap.add_argument("--unwind-decay-min", type=float, default=10, help="minutes for the exit price to slide to avg + min-gain")
    ap.add_argument("--max-fair-side", type=float, default=0.92, help="never ladder a side whose fair is above this")
    ap.add_argument("--min-gain", type=float, default=0.005, help="never unwind below avg cost plus this")
    ap.add_argument("--max-pos", type=float, default=5000, help="no more ladder buys on a side holding this cost")
    ap.add_argument("--max-pos-safe", type=float, default=3000, help="same, for sides whose fair is above 0.9")
    ap.add_argument("--overcommit", type=float, default=8.0, help="resting ladder notional cap = x * (cash - reserve)")
    # Replaying 2 Oct's sweeps: catcher profit at 60k cash was ~131k with 1 h exits, ~47k with 3 h, ~7k held to
    # settlement, and the exit cost (SIG's long-shot premium) was ~0.04 at 5-20 min as at 1-3 h: so exit fast, always.
    ap.add_argument("--recycle-below", type=float, default=float("inf"), help="below this cash, positions exit at SIG's level")
    ap.add_argument("--recycle-age-min", type=float, default=2, help="... once their first lot is this many minutes old")
    ap.add_argument("--recycle-gain", type=float, default=0.005, help="... if that clears avg cost + this")
    ap.add_argument("--recycle-over", type=float, default=0.02, help="... paying up to SIG's smoothed mid + this")
    ap.add_argument("--costly-exit", type=float, default=0.03,
                    help="exits giving back more than this per share vs Kalshi fair ...")
    # inf since 4 Oct ~13Z (was 25k): no holding for settlement. research/pnl_study.py (60 h of own fills): round trips closed
    # within 2-30 min made +2-3c/share, 30 min-6 h ~+0.3c, > 6 h lost.
    ap.add_argument("--costly-exit-cash", type=float, default=float("inf"), help="... only while cash is under this")
    ap.add_argument("--dead-edge", type=float, default=0.02, help="positions with less than this left to settlement ...")
    ap.add_argument("--dead-loss", type=float, default=0.03, help="... may exit up to this below avg cost")
    ap.add_argument("--dead-age-min", type=float, default=360, help="... once their first lot is this many minutes old")
    # 360 / 0.03 since 4 Oct ~13Z (were 60 / 0.10): the 60-min dump at up to 10c under cost lost 3.6k at 08Z and 1.5k
    # at 11Z on 4 Oct, while positions held 30 min-6 h still closed at ~+0.3c/share on average.
    ap.add_argument("--free-age-min", type=float, default=360,
                    help="positions this old (first lot, minutes) exit at SIG's level even under cost (0 = off)")
    ap.add_argument("--free-max-loss", type=float, default=0.03, help="... giving back at most this per share under avg")
    # Since 6 Oct ~02Z (decision: hold Delaware, buy partly for settlement): books one tick wide, 4c dips gone, cash idle.
    ap.add_argument("--value-edge", type=float, default=0.05,
                    help="settlement-value buys: take a side SIG offers this far under Kalshi/Poly fair (net of buffer)")
    ap.add_argument("--value-hold-edge", type=float, default=0.03,
                    help="positions worth this much over avg cost at fair are held to settlement (no under-fair exits)")
    ap.add_argument("--value-exit-under", type=float, default=0.01, help="a held position's exit sells at fair minus this")
    # 12k / 15k until 9 Oct: the 9 Oct tape (17.8 h) had 5.9M shares trade >= 3c through outside fair on the passive
    # side (468k of settlement edge, RI Senate alone 162k at ~19%), and the caps, not cash, stopped us.
    ap.add_argument("--value-max-pos", type=float, default=40000, help="value buys stop at this cost per exchange side")
    ap.add_argument("--value-max-race", type=float, default=60000, help="... and at this cost per race (all its exchanges)")
    ap.add_argument("--value-min-roc", type=float, default=0.07,
                    help="value buys need this return to settlement ((fair - buffer - price) / price)")
    ap.add_argument("--value-take-roc", type=float, default=0.15,
                    help="take the best offer only at this return or more; otherwise rest at the side's best bid")
    # 60k (busy hours) / 25k until 9 Oct: kept for busy-market dips, which stopped (9 Oct tape: 2 dip fills in 17.8 h
    # whatever the cash), and on 8 Oct 18:07Z the 60k floor stopped the value role during a RI Senate spike that
    # traded 921k shares of the favourite at <= 0.82 (fair 0.991) while we sat on exactly 60,000. 20k = working cash for
    # the quotes (--quote-max) and spike catchers.
    ap.add_argument("--value-min-cash", type=float, default=20000, help="value buys only spend cash above this")
    ap.add_argument("--value-min-cash-off", type=float, default=20000,
                    help="... outside --dip-full-hours (6 Oct: off-hours dips barely fill, cash sat idle)")
    # 50k since 11 Oct (decision: "free cash ... for busy hours"; 0 from 9 Oct, 60k before): every 9-10 Oct sweep catch
    # was capped by the ~25k free cash (SIG sizes resting buys to the balance).
    ap.add_argument("--busy-cash", type=float, default=50000,
                    help="free cash to have when --dip-full-hours start: in the --busy-prep-min before, value holds in "
                         "profit at SIG's bid are sold (smallest edge first) and value buys don't spend it until the "
                         "busy hours end; 0 = off")
    ap.add_argument("--busy-prep-min", type=int, default=60, help="minutes before --dip-full-hours for --busy-cash")
    ap.add_argument("--busy-prep-gain", type=float, default=0.005, help="prep sales only at >= avg + this per share")
    ap.add_argument("--night-sell-all", action="store_true",
                    help="from 3 Nov 19:00Z sell every hold at SIG's bid (not only late-called ones) for election-night cash")
    ap.add_argument("--night-sell-loss", type=float, default=0.03,
                    help="--night-sell-all: no sale more than this under avg cost ...")
    ap.add_argument("--night-sell-gap", type=float, default=0.06, help="... or more than this under fair (an empty or stale book)")
    ap.add_argument("--no-spike", action="store_true", help="no spike catchers")
    ap.add_argument("--spike-levels", default="0.03,0.05",
                    help="spike catchers: buy a side SIG prices >= --spike-min-gap under fair this far under SIG's level")
    ap.add_argument("--spike-min-gap", type=float, default=0.05, help="... on sides worth this much over SIG's level")
    ap.add_argument("--spike-notional", type=float, default=40000, help="SUSQies per spike catcher order")
    ap.add_argument("--spike-max-race", type=float, default=80000, help="no spike catchers on a race holding this cost")
    ap.add_argument("--spike-overcommit", type=float, default=60.0, help="resting spike catcher cap = x * (cash - hard floor)")
    # Off by default since 17 Oct: 16 Oct 15:20-20:33Z it sold 6.7k Colorado Sen R NO at 0.90-0.905 (Kalshi ~0.99,
    # ~-0.6k) six times for value buys on 840/1076/1073/1051 that all came back no-ops (our resting dips commit the
    # balance), and the no-op memory starts empty at each run; 12-16 Oct it gave ~-5k at fair for ~+0.5k of buys.
    ap.add_argument("--no-rotate", action="store_true", default=True, help="never sell a value hold to buy better value")
    ap.add_argument("--rotate", dest="no_rotate", action="store_false", help="turn rotation (below) back on")
    ap.add_argument("--rotate-gap", type=float, default=0.10,   # 0.06 until 10 Oct (sales gave up value at fair)
                    help="sell a hold at SIG's bid when a value buy on offer returns this much more to settlement")
    ap.add_argument("--rotate-refill", action="store_true",
                    help="let rotation also sell holds to refill the value floor after a fill drained the cash "
                         "(off since 12 Oct: those sales gave ~8-10c/share away at fair)")
    ap.add_argument("--rotate-noop-min", type=float, default=60,
                    help="... never to fund a value buy that came back a no-op within this many minutes")
    ap.add_argument("--rotate-max-loss", type=float, default=0.01, help="... at no more than this per share under avg")
    ap.add_argument("--rotate-chunk", type=float, default=15000, help="... and at most this many SUSQies per hold per cycle")
    # Dump follow-ups (since 22 Oct: "replicate the big profitable trades"): see Maker.followups.
    ap.add_argument("--no-followup", action="store_true", help="never sell holds to buy a favourite right after a dump")
    ap.add_argument("--followup-dip", type=float, default=0.03,
                    help="a dump: the favourite offered this far under SIG's own 30-min level for the side")
    ap.add_argument("--followup-roc", type=float, default=0.06, help="... and returning at least this to settlement")
    ap.add_argument("--followup-gap", type=float, default=0.04,
                    help="sell a hold for it only if the dump returns this much more than the hold left")
    ap.add_argument("--followup-max", type=float, default=40000, help="at most this cost per dumped exchange side")
    ap.add_argument("--followup-min-age", type=float, default=360,
                    help="never sell a hold younger than this (minutes): fresh catches stay")
    ap.add_argument("--no-control", action="store_true", help="treat the chamber-control markets like any value buy")
    ap.add_argument("--control-take-roc", type=float, default=0.07,
                    help="value buys take the offer on chamber-control markets at this return to settlement")
    ap.add_argument("--control-max-race", type=float, default=40000, help="value buys stop at this cost per chamber")
    ap.add_argument("--control-rotate-slack", type=float, default=0.02,
                    help="rotation may sell a hold returning up to this much more than a control buy to fund it")
    ap.add_argument("--no-quote", action="store_true", help="no two-sided quotes at the touch")
    ap.add_argument("--quote-n", type=int, default=0,
                    help="quote this many busy, fairly priced markets (0 since 11 Oct: two losing evenings; was 15)")
    ap.add_argument("--quote-hours", default="16-24", help="UTC hours with quotes")
    ap.add_argument("--quote-size", type=int, default=500, help="shares per quote (plus any inventory on the exit side)")
    ap.add_argument("--quote-max-inv", type=int, default=2000, help="no more quotes on a side holding this many shares")
    ap.add_argument("--quote-max", type=float, default=10000, help="max quote inventory cost (and resting quote notional)")
    ap.add_argument("--quote-max-gap", type=float, default=0.03, help="quote only where SIG's mid is this close to fair")
    ap.add_argument("--quote-max-age", type=float, default=60,
                    help="quote inventory older than this (minutes) goes to the normal exits")
    ap.add_argument("--no-value", action="store_true", help="no value buys and no settlement holds (turnover only)")
    ap.add_argument("--hot-levels", default="0.04,0.06,0.08",
                    help="sweep-catcher distances on config/hot_markets.json (0.10,0.15,0.22 until 4 Oct)")
    ap.add_argument("--hot-mult", type=float, default=1.0,
                    help="hot-market catchers are this many times bigger (4 until 4 Oct: the 2 Oct sweep list is stale)")
    ap.add_argument("--hot-max-pos", type=float, default=40000, help="no new hot catchers on a side holding this cost")
    ap.add_argument("--no-hot", action="store_true", help="treat hot markets like the rest")
    ap.add_argument("--stub-levels", default="0.04,0.06,0.08",
                    help="sweep catchers: distances below fair, both sides (0.125,0.18,0.30 until 4 Oct)")
    ap.add_argument("--stub-spacing", type=float, default=0.02,
                    help="catcher levels at least this far apart; keep windows are half of it (0.05 until 4 Oct)")
    ap.add_argument("--stub-fixed", default="",
                    help="sweep catchers at fixed prices, e.g. 0.02 = selling the other side at 0.98 (0.02 until 4 Oct)")
    ap.add_argument("--stub-fixed-min-edge", type=float, default=0.35, help="fixed-price catcher only if fair is this far above it")
    ap.add_argument("--stub-notional", type=float, default=2500, help="max SUSQies per Yes sweep-catcher order")
    ap.add_argument("--stub-shares", type=int, default=10000, help="max shares per Yes sweep-catcher order")
    ap.add_argument("--stub-no-mult", type=float, default=2.0, help="No-side catchers are this many times bigger")
    ap.add_argument("--stub-safe-fair", type=float, default=0.90, help="on sides worth more than this ...")
    ap.add_argument("--stub-safe-roc", type=float, default=1.0, help="... keep only catchers with (fair - px) / px >= this")
    ap.add_argument("--stub-expiry-min", type=float, default=360, help="expirationDate on sweep catchers (keeps queue place)")
    ap.add_argument("--stub-max-pos", type=float, default=15000, help="no new sweep catchers on a side holding this cost")
    ap.add_argument("--stub-overcommit", type=float, default=60.0, help="resting sweep-catcher cap = x * (cash - hard floor)")
    ap.add_argument("--no-stubs", action="store_true", help="disable the sweep catchers")
    ap.add_argument("--no-snipe", action="store_true", help="disable the sniper thread")
    ap.add_argument("--no-premium", action="store_true", help="disable the long-shot sweep catchers and their exits")
    ap.add_argument("--prem-max-fair", type=float, default=0.08, help="long-shot catchers where the long shot's fair is under this")
    ap.add_argument("--prem-levels", default="0.04,0.08,0.13", help="offer the long shot this far above SIG's smoothed mid")
    ap.add_argument("--prem-notional", type=float, default=60000, help="max SUSQies per long-shot catcher order")
    ap.add_argument("--prem-shares", default="20000,40000,60000", help="shares per long-shot catcher order, per level")
    ap.add_argument("--prem-max-pos", type=float, default=40000, help="no new long-shot catchers on a market holding this cost")
    ap.add_argument("--prem-overcommit", type=float, default=60.0, help="resting long-shot catcher cap = x * (cash - hard floor)")
    ap.add_argument("--prem-exit-gain", type=float, default=0.005, help="exit at SIG's level only if it clears avg + this")
    ap.add_argument("--prem-exit-over", type=float, default=0.04, help="pay up to SIG's smoothed long-shot mid + this to exit")
    ap.add_argument("--mid-alpha", type=float, default=0.2, help="smoothing of SIG's mid per cycle")
    ap.add_argument("--snipe-every", type=float, default=2, help="seconds between sniper scans of every book")
    ap.add_argument("--snipe-edge", type=float, default=0.04, help="take only orders this far through fair (+ buffer)")
    ap.add_argument("--snipe-edge-wide", type=float, default=0.06,
                    help="... or this far where SIG's smoothed mid and the smoothed fair differ by > --snipe-tight-agree")
    ap.add_argument("--snipe-tight-agree", type=float, default=0.03)
    ap.add_argument("--snipe-under-mid", type=float, default=0.02,
                    help="snipe only at least this far under SIG's own smoothed level for the side (0 = off)")
    ap.add_argument("--snipe-agree", type=float, default=0.06, help="... and only where SIG's smoothed mid is this close to fair")
    ap.add_argument("--snipe-alpha", type=float, default=0.02, help="smoothing of SIG's mid per scan (2 s scans)")
    ap.add_argument("--snipe-min-obs", type=int, default=12, help="scans of history needed before sniping an exchange")
    ap.add_argument("--snipe-fair-age", type=float, default=60, help="never snipe on a fair older than this (s)")
    ap.add_argument("--snipe-cooldown", type=float, default=30, help="seconds between snipes on one exchange")
    ap.add_argument("--snipe-notional", type=float, default=4000, help="max SUSQies per snipe order")
    ap.add_argument("--snipe-shares", type=int, default=20000, help="max shares per snipe order")
    ap.add_argument("--snipe-run-cap", type=float, default=40000, help="max SUSQies sniped per run")
    ap.add_argument("--snipe-per-scan", type=int, default=3, help="max snipes sent per scan")
    ap.add_argument("--snipe-expiry-min", type=float, default=2, help="expirationDate on snipe orders")
    ap.add_argument("--snipe-reads", type=int, default=56, help="reads/min reserved for the sniper (of 90) while it polls")
    ap.add_argument("--snipe-reads-rt", type=int, default=24, help="... and while the realtime feed is live")
    ap.add_argument("--snipe-poll-rt", type=float, default=10,
                    help="while the realtime feed is live the sniper scans pushed books every --snipe-every s and "
                         "polls REST only this often (a cross-check; dropped batches are never replayed)")
    ap.add_argument("--no-realtime", action="store_true", help="no realtime feed: sniper polls, no wall jumps")
    ap.add_argument("--no-lbtrack", action="store_true", help="don't log the 1d leaderboard steps and the tape")
    ap.add_argument("--lb-every", type=float, default=15, help="seconds between leaderboard polls (2 reads each)")
    ap.add_argument("--lb-reads", type=int, default=8, help="reads/min reserved for the leaderboard tracker")
    # Off by default: 4 Oct 08:40Z live books had 2k+ share walls at nearly every tick, so 1,163 of 1,180 catchers
    # jumped the full window and the others' bids ahead of them only fell from a median 1.06M to 0.84M shares, for
    # 3.5c of edge on every fill (sweeps were a median 1.5k shares).
    ap.add_argument("--jump-max", type=float, default=0.0,
                    help="catchers move up to this far above their level to sit one tick above a wall (0 = off)")
    ap.add_argument("--jump-wall", type=float, default=2000, help="... a wall = this many shares of others' bids at one price")
    ap.add_argument("--jump-keep-edge", type=float, default=0.05, help="... never closer to fair than this")
    ap.add_argument("--snipe-kalshi-every", type=float, default=3,
                    help="seconds between the sniper's own Kalshi + Polymarket refreshes")
    ap.add_argument("--snipe-chunks", type=int, default=2, help="100-id price reads per sniper scan (window rotates)")
    ap.add_argument("--snipe-writes", type=int, default=6, help="writes/min reserved for the sniper (of 27)")
    # 0.04 since 4 Oct ~13Z (was 0.02): entries >= 0.04 under SIG's own level closed at +2.5-3c/share, 0-0.02 under at
    # +0.2c, at or above it at a loss (research/pnl_study.py, entries of the 24 h to 4 Oct 12:40Z).
    ap.add_argument("--open-under-mid", type=float, default=0.04,
                    help="ladder/catcher buys rest at least this far under SIG's smoothed mid for the side (0 = off)")
    # Dip ladder (since 5 Oct: "3k/h"): anchored on SIG's own level instead of Kalshi; see Maker.dips.
    ap.add_argument("--no-dip", action="store_true",
                    help="old behaviour: Kalshi-anchored ladder (--levels) and sweep catchers (--stub-*) instead of dips")
    ap.add_argument("--dip-levels", default="0.04,0.05,0.06",
                    help="buy this far under SIG's own level for the side (0.02 lost and 0.03 broke even in the replay)")
    ap.add_argument("--dip-window-min", type=float, default=30, help="SIG level = median book mid over this many minutes")
    ap.add_argument("--dip-min-obs", type=int, default=3, help="cycles of mids needed before an exchange gets dips")
    # 50k shares since 5 Oct ~08Z (was 20k): replay profit roughly doubled (12.6k -> 25.6k/day), sweeps run deeper.
    ap.add_argument("--dip-shares", type=float, default=50000, help="max shares per dip order (busy market, full hours)")
    ap.add_argument("--dip-notional", type=float, default=40000, help="max SUSQies per dip order (busy market, full hours)")
    ap.add_argument("--dip-full-hours", default="16-24", help="UTC hours with full-size dips (US daytime/evening)")
    ap.add_argument("--dip-off-mult", type=float, default=0.25, help="dip size outside --dip-full-hours")
    ap.add_argument("--dip-quiet-mult", type=float, default=0.25, help="dip size off config/busy_markets.json")
    ap.add_argument("--dip-max-over-fair", type=float, default=0.03,
                    help="never buy a side more than this above its Kalshi/Polymarket fair")
    ap.add_argument("--dip-min-px", type=float, default=0.03)
    ap.add_argument("--dip-max-px", type=float, default=0.95)
    ap.add_argument("--dip-max-pos", type=float, default=50000, help="no new dips on a side holding this cost")
    ap.add_argument("--dip-overcommit", type=float, default=150.0, help="resting dip cap = x * (cash - hard floor)")
    ap.add_argument("--busy-top", type=int, default=40, help="busy markets = this many with the most shares traded")
    ap.add_argument("--busy-min-hours", type=float, default=12,
                    help="feed hours (last 24 h of runs on run-logs) needed to replace config/busy_markets.json")
    ap.add_argument("--mass-cancel", type=int, default=300,
                    help="more cancels than this in one cycle: one cancel-all, then re-place what is wanted")
    # Baskets and pair holding park cash until settlement: off since 4 Oct ~12:30Z (decision: turnover now, settlement
    # holding only near the end). The 08:30Z run had put ~59k into 61k pairs for ~+1.3k locked.
    ap.add_argument("--basket", action="store_true", help="sniper: race baskets (NO on both parties), off by default")
    ap.add_argument("--basket-edge", type=float, default=0.02, help="basket when the two YES bids sum to >= 1 + this")
    ap.add_argument("--basket-leg-tol", type=float, default=0.02, help="... and each bid is at least fair minus this")
    ap.add_argument("--basket-notional", type=float, default=10000, help="max SUSQies per basket (both legs)")
    ap.add_argument("--basket-run-cap", type=float, default=60000, help="max SUSQies into baskets per run")
    # 80k until 4 Oct 09:45Z; freed cash then sat idle (fills, not cash, are what's scarce) while 7 races offered
    # 2.5-3.5c locked baskets, so baskets may use cash above 40k (40k stays free for sweeps and snipes).
    ap.add_argument("--basket-min-cash", type=float, default=40000,
                    help="baskets hold cash to settlement: only with cash above this left after them")
    ap.add_argument("--basket-min-qty", type=int, default=200, help="smallest basket worth sending (pairs)")
    ap.add_argument("--basket-expiry-s", type=float, default=30, help="expirationDate on basket legs")
    ap.add_argument("--basket-top-age", type=float, default=10, help="a top of book this old (s) can still trigger one")
    ap.add_argument("--pair-hold", action="store_true",
                    help="hold NO+NO pairs to settlement while cash isn't short (default: unwind like any position)")
    ap.add_argument("--reserve", type=float, default=25000,   # 50k until 4 Oct 09:45Z (positions freed)
                    help="below this cash only ladder orders with return-on-cash >= --scarce-roc stay or get placed")
    ap.add_argument("--scarce-roc", type=float, default=0.12, help="(fair - price) / price needed when cash is scarce")
    ap.add_argument("--hard-floor", type=float, default=3000, help="pull the ladder (keep unwinds) below this cash")
    ap.add_argument("--max-dd", type=float, default=15000, help="cancel everything if value drops this much in a run")
    ap.add_argument("--expiry-min", type=float, default=150, help="expirationDate on every order")
    ap.add_argument("--refresh-min", type=float, default=25, help="replace orders expiring within this")
    ap.add_argument("--noop-retry-min", type=float, default=15,
                    help="re-send an order SIG answered with orderId null (balance committed) after this, at the latest")
    ap.add_argument("--fair-every", type=float, default=20, help="seconds between fair-value refreshes")
    ap.add_argument("--fair-max-age", type=float, default=900, help="cancel everything if fair is older")
    a = ap.parse_args()
    now = utcnow()
    if a.live and ELECTION_HANDOVER <= now < CLOSE_UTC and os.environ.get("SIGBOT_NO_ELECTION") != "1":
        # Election night: this routine slot runs a second election.py until its own :29 slot end. The two share a
        # lease (sigbot/lease.py): one trades, the other is a hot standby that takes over if the first dies.
        # --secondary: with GitHub down this copy never trades without the lease (the routine's primary run does).
        print(f"maker: election night, handing over to election.py until {next_slot_end(now):%H:%M}Z", flush=True)
        os.execv(sys.executable, [sys.executable, "-u", str(ROOT / "election.py"), "--live", "--secondary",
                                  "--deadline", iso(next_slot_end(now))])
    Maker(a).run()


if __name__ == "__main__":
    main()
