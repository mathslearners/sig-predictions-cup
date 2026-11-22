"""Sniper: takes resting orders that sit far through fair (players' limit orders at absurd prices).

Runs as a thread inside a live maker.py run. 4 Oct study of the 195 snipes sent 2-4 Oct: 152 went out at exactly
the 0.06 threshold and in 140 of 157 missed/partial ones someone else had taken the level first, mostly 0-6 s
earlier. Levels drift into range as Kalshi moves, so bots with fresher Kalshi prices and lower thresholds got there
first. Hence (since 4 Oct):
  * its own Kalshi refresh every --snipe-kalshi-every s (the maker's fair was up to ~30 s old, allowed 60 s);
  * scans only the exchanges with an outside fair (2 reads, sent in parallel) every --snipe-every s;
  * threshold --snipe-edge 0.04 (+ Kalshi buffer) where SIG and Kalshi normally agree within
    --snipe-tight-agree (smoothed), else --snipe-edge-wide 0.06; 30 s cooldown per exchange;
  * SIG's realtime books (tournament:{t}:market:{m}, ~1 s batches) trigger it the moment a book changes, when the
    feed is live (SIG disabled realtime on 4 Oct 01:08Z "for a few hours"; all 237 joins were accepted). Polling
    keeps running as the fallback.

Safety: acts only on Kalshi/Polymarket/override fair values (never the SIG-consensus fair), only while those
are fresh, only where SIG's own smoothed mid has agreed with fair (a persistent gap means a bad mapping or a
real move Kalshi hasn't shown, not a gift), with a per-exchange cooldown, a per-order and a per-run cap, and
a short expirationDate so an unfilled remainder dies within minutes (the maker's next cycle cancels it too).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from concurrent.futures import ThreadPoolExecutor

from sigbot.client import RateLimiter, SigAPIError, SigClient, new_idempotency_key
from sigbot.kalshi import Kalshi
from sigbot.outside import outside_fair
from sigbot.polymarket import Polymarket

TICK, MIN_PX, MAX_PX = 0.005, 0.005, 0.995


def _floor(p: float) -> float:
    return round(int(round(p / TICK, 6)) * TICK, 3)


class Sniper(threading.Thread):
    def __init__(self, maker):
        super().__init__(daemon=True, name="sniper")
        self.m, self.a = maker, maker.a
        self.c = SigClient()
        self.c.read_limiter = RateLimiter(self.a.snipe_reads)
        self.c.write_limiter = RateLimiter(self.a.snipe_writes)
        self.halt = threading.Event()
        self.ema: dict[str, float] = {}     # SIG mid, smoothed across scans
        self.fema: dict[str, float] = {}    # fair, smoothed the same way
        self.seen = defaultdict(int)
        self.last_hit: dict[str, float] = {}
        self.spent = 0.0
        self.stats = defaultdict(float)
        self.lock = threading.Lock()          # polling and realtime threads both evaluate and take
        self.pool = ThreadPoolExecutor(4)
        self.k = Kalshi()
        self.poly = Polymarket()
        self.kfair: dict[str, dict] = {}      # exchange -> {"p", "buffer"} from the sniper's own Kalshi/Poly refresh
        self.kfair_at = 0.0
        self._off = 0                         # rotating start of the tops() window
        self.top_cache: dict[str, tuple] = {}  # exchange -> (bid, ask, time seen), for two-leg baskets
        self.basket_spent = 0.0
        self.basket_pending = 0.0             # basket cost since the maker last read cash
        self._cash_seen = None

    def tops(self) -> dict[str, tuple]:
        """Top of book for the exchanges with an outside fair (the only ones it may snipe): --snipe-chunks reads of
        100 ids in parallel. With Polymarket that is ~233 exchanges, so the window rotates (each one is read every
        ~2.3 scans' worth of ids) instead of spending a third read per scan."""
        ids = sorted(ex for ex in self.m.markets if ex in self.m.fair) or sorted(self.m.markets)
        n = min(len(ids), 100 * self.a.snipe_chunks)
        start = self._off % len(ids)
        window = (ids + ids)[start:start + n]
        self._off = start + n

        def one(chunk):
            r = self.c.get("/exchanges/prices", ids=",".join(chunk), tournamentId=self.m.tid)
            return {str(x.get("exchangeId")): (x.get("bestBid"), x.get("bestAsk"))
                    for x in (r.get("data") if isinstance(r, dict) else r) or []}
        out = {}
        for part in self.pool.map(one, [window[i:i + 100] for i in range(0, len(window), 100)]):
            out.update(part)
        return out

    def refresh_kalshi(self):
        """Fresh Kalshi + Polymarket mids for the maker's outside fairs (~2 s, public APIs, no SIG reads)."""
        m = self.m
        live = [ex for ex, f in m.fair.items() if not f.get("override")]
        try:
            kq, pq, kerr = m.outside_quotes(self.k, self.poly)
        except Exception:  # noqa: BLE001 - the maker's fair still applies
            kq, kerr = None, "error"
        if kq is None:   # Kalshi down: no overlay; the maker's fair (with its Polymarket fallback) applies
            self.stats["kalshi_errors"] += 1
            return
        out = {}
        for ex in live:
            f = outside_fair(kq.get(m.kticker[ex]) if ex in m.kticker else None,
                             pq.get(m.pticker[ex]) if ex in m.pticker else None)
            if f:
                out[ex] = {"p": f["p"], "buffer": f["buffer"]}
        self.kfair, self.kfair_at = out, time.time()

    def fair(self) -> dict[str, dict]:
        """The maker's fair with the sniper's fresher Kalshi mids laid over it."""
        f = dict(self.m.fair)
        if time.time() - self.kfair_at < self.a.snipe_fair_age:
            for ex, v in self.kfair.items():
                if ex in f:
                    f[ex] = {**f[ex], **v}
        return f

    def observe(self, tops: dict):
        for ex, (b, s) in tops.items():
            if b is None or s is None or s - b > 0.10:
                continue   # one-sided or gapped book: no reliable mid this scan
            mid = (b + s) / 2
            prev = self.ema.get(ex)
            self.ema[ex] = mid if prev is None else prev + self.a.snipe_alpha * (mid - prev)
            self.seen[ex] += 1
        for ex, f in self.fair().items():   # smoothed fair too: a fresh Kalshi move shows as fair vs its own EMA
            prev = self.fema.get(ex)
            self.fema[ex] = f["p"] if prev is None else prev + self.a.snipe_alpha * (f["p"] - prev)

    def opportunities(self, tops: dict, now: datetime, require_history: bool = True) -> list[dict]:
        a, m = self.a, self.m
        if m.fair_at is None or (now - m.fair_at).total_seconds() > a.snipe_fair_age:
            return []
        out = []
        t = time.time()
        for ex, f in self.fair().items():
            if ex not in tops:
                continue
            if t - self.last_hit.get(ex, 0) < a.snipe_cooldown:
                continue
            bid, ask = tops.get(ex, (None, None))
            p, edge = f["p"], a.snipe_edge_wide + f["buffer"]
            if require_history and (self.seen[ex] < a.snipe_min_obs or abs(self.ema[ex] - p) > a.snipe_agree):
                continue
            # Tight tier: where SIG and fair normally agree (smoothed), a level a.snipe_edge through fair is a fresh
            # dislocation that other bots race for. Where they persistently differ (4 Oct: 35 races 4-6c apart for
            # hours) only the wide tier applies, so standing gaps don't eat the run cap.
            if not require_history or abs(self.ema[ex] - self.fema.get(ex, p)) <= a.snipe_tight_agree:
                edge = a.snipe_edge + f["buffer"]
            # The best level may be our own exit (e.g. buying a long shot back at SIG's level): own orders never
            # match, so 3 Oct's run "sniped" MA Senate R's own 0.075 Yes bid three times for nothing.
            own = {(o.get("side"), round(float(o.get("priceLimit") or o.get("price") or 0), 3))
                   for o in m._open_by_ex.get(ex, [])}
            # Turnover (since 4 Oct ~12:30Z): only a real dip under SIG's own smoothed level, so the position resells
            # above cost within minutes. Before, the wide tier bought favourites at SIG's normal price where SIG sits
            # under Kalshi (MA Senate D 14.7k at 0.92 vs 0.982, IL Gov D 13.2k at 0.91), which never resell above cost
            # and get freed an hour later under it.
            ema = self.ema.get(ex)
            cap_yes = _floor(ema - a.snipe_under_mid) if ema is not None and a.snipe_under_mid > 0 else 1.0
            cap_no = _floor(1 - ema - a.snipe_under_mid) if ema is not None and a.snipe_under_mid > 0 else 1.0
            lim_yes, lim_no = min(_floor(p - edge), cap_yes), min(_floor(1 - p - edge), cap_no)
            if ask is not None and ask <= lim_yes + 1e-9 and ("no", round(1 - ask, 3)) not in own:   # Yes cheap: buy Yes
                out.append({"ex": ex, "side": "yes", "best": ask, "limit": lim_yes, "fair_s": p})
            elif bid is not None and 1 - bid <= lim_no + 1e-9 and ("yes", round(bid, 3)) not in own:   # Yes dear: buy No
                out.append({"ex": ex, "side": "no", "best": round(1 - bid, 3), "limit": lim_no, "fair_s": 1 - p})
        for o in out:
            o["limit"] = min(max(o["limit"], MIN_PX), MAX_PX)
            o["qty"] = int(min(a.snipe_shares, a.snipe_notional / max(o["limit"], MIN_PX)))
            o["edge"] = round(o["fair_s"] - o["best"], 3)
        return sorted(out, key=lambda o: -o["edge"] * o["qty"])

    def take(self, o: dict):
        a = self.a
        cost = o["qty"] * o["limit"]
        if self.spent + cost > a.snipe_run_cap or (self.m.last_cash or 0) - cost < a.hard_floor:
            self.stats["skipped_cash"] += 1
            return
        self.last_hit[o["ex"]] = time.time()
        body = {"idempotencyKey": new_idempotency_key(), "exchangeId": o["ex"], "side": o["side"], "action": "buy",
                "quantity": o["qty"], "price": o["limit"], "tournamentId": self.m.tid,
                "expirationDate": (datetime.now(timezone.utc) + timedelta(minutes=a.snipe_expiry_min))
                .strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        t0 = time.time()
        try:
            r = self.c.post("/orders", body)
        except SigAPIError as e:
            self.m.event({"kind": "snipe_error", **o, "error": f"{e.status} {e.code} {e.message[:120]}"})
            return
        except Exception as e:  # noqa: BLE001
            self.m.event({"kind": "snipe_error", **o, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            return
        d = (r.get("data") if isinstance(r, dict) and isinstance(r.get("data"), dict) else r) or {}
        traded = float(d.get("quantityTraded") or 0)
        if d.get("orderId") is not None:
            self.m._mine.add(d["orderId"])
        self.spent += traded * o["limit"]          # upper bound on cash used (fills are at or under the limit)
        self.stats["sent"] += 1
        self.stats["traded"] += traded
        self.stats["edge"] += traded * (o["fair_s"] - o["limit"])   # lower bound
        self.m.event({"kind": "snipe", **o, "orderId": d.get("orderId"), "traded": traded,
                      "latency_s": round(time.time() - t0, 1),
                      "title": self.m.markets[o["ex"]]["title"]})
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] SNIPE {o['side'].upper()} {o['ex']} best {o['best']:.3f} "
              f"fair {o['fair_s']:.3f} limit {o['limit']:.3f} qty {o['qty']} -> traded {traded:,.0f} "
              f"({time.time() - t0:.1f}s) {self.m.markets[o['ex']]['title'][:50]}", flush=True)

    # ------------------------------------------------------------------ race baskets
    def baskets(self, tops: dict) -> list[dict]:
        """Two-party races whose YES bids sum to >= 1 + --basket-edge: buying NO on both parties at 1 - bid pays >= 1
        whoever wins, so the excess is locked without trusting any fair value (4 Oct: another team's bot held 104k
        such pairs at ~0.955, the bids in 12 races summing to 1.035-1.045 for hours). Each leg must also be within
        --basket-leg-tol of fair, so a leg left alone (part fill, or a cash-short unwind) is still a sound position."""
        a, m = self.a, self.m
        if not a.basket:
            return []
        fair, t, out = self.fair(), time.time(), []
        for race, (d, r) in m.pairs.items():
            if d not in tops or r not in tops or d not in fair or r not in fair or \
                    t - self.last_hit.get(race, 0) < a.snipe_cooldown:
                continue
            bd, br = tops[d][0], tops[r][0]
            if bd is None or br is None or bd + br < 1 + a.basket_edge - 1e-9:
                continue
            if bd < fair[d]["p"] - a.basket_leg_tol or br < fair[r]["p"] - a.basket_leg_tol:
                continue
            out.append({"race": race, "d": d, "r": r, "bd": bd, "br": br, "edge": round(bd + br - 1, 3)})
        return sorted(out, key=lambda o: -o["edge"])

    def _bids(self, ex: str, book: dict) -> list[list]:
        """[[price, qty]] YES bids, best first, minus our own resting YES buys (own orders never match)."""
        mine = defaultdict(float)
        for o in self.m._open_by_ex.get(ex, []):
            if o.get("side") == "yes" and o.get("action", "buy") == "buy":
                mine[round(float(o.get("priceLimit") or o.get("price") or 0), 3)] += float(o.get("quantity") or 0)
        out = []
        for lv in book.get("bids") or []:
            px = round(float(lv["price"]), 3)
            q = float(lv["quantity"]) - mine.get(px, 0.0)
            if q >= 1:
                out.append([px, q])
        return sorted(out, key=lambda x: -x[0])

    def take_basket(self, o: dict):
        a, m = self.a, self.m
        self.last_hit[o["race"]] = time.time()
        if m.last_cash_at != self._cash_seen:   # the maker read cash again: earlier baskets are in it now
            self._cash_seen, self.basket_pending = m.last_cash_at, 0.0
        budget = min(a.basket_notional, a.basket_run_cap - self.basket_spent,
                     (m.last_cash or 0) - self.basket_pending - a.basket_min_cash)
        if budget < 100:
            self.stats["basket_skipped_cash"] += 1
            return
        try:
            books = list(self.pool.map(lambda ex: self.c.get(f"/exchanges/{ex}/orderbook", tournamentId=m.tid,
                                                             depth=20), (o["d"], o["r"])))
        except Exception as e:  # noqa: BLE001
            self.m.event({"kind": "basket_error", **o, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            return
        ld, lr = self._bids(o["d"], books[0]), self._bids(o["r"], books[1])
        i = j = 0
        qty, cost, pd, pr = 0, 0.0, None, None
        while i < len(ld) and j < len(lr) and ld[i][0] + lr[j][0] >= 1 + a.basket_edge - 1e-9:
            unit = (1 - ld[i][0]) + (1 - lr[j][0])
            q = int(min(ld[i][1], lr[j][1], (budget - cost) / unit))
            if q < 1:
                break
            qty, cost, pd, pr = qty + q, cost + q * unit, ld[i][0], lr[j][0]
            ld[i][1] -= q
            lr[j][1] -= q
            i += ld[i][1] < 1
            j += lr[j][1] < 1
        if qty < a.basket_min_qty:
            self.stats["basket_too_thin"] += 1
            return
        exp = (datetime.now(timezone.utc) + timedelta(seconds=a.basket_expiry_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        legs = [(o["d"], round(1 - pd, 3)), (o["r"], round(1 - pr, 3))]
        body = {"idempotencyKey": new_idempotency_key(), "orders": [
            {"exchangeId": ex, "side": "no", "action": "buy", "quantity": qty, "price": px,
             "tournamentId": m.tid, "expirationDate": exp} for ex, px in legs]}
        t0 = time.time()
        try:
            r = self.c.post("/orders/batch", body)
        except SigAPIError as e:
            if not (isinstance(e.details, dict) and "results" in e.details):   # 207: some legs went through
                self.m.event({"kind": "basket_error", **o, "error": f"{e.status} {e.code} {e.message[:120]}"})
                return
            r = e.details
        except Exception as e:  # noqa: BLE001
            self.m.event({"kind": "basket_error", **o, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            return
        traded = []
        for res, (ex, px) in zip(r.get("results", []), legs):
            d = res.get("data") or {}
            if d.get("orderId") is not None:
                m._mine.add(d["orderId"])
            traded.append(float(d.get("quantityTraded") or 0) if res.get("ok") else 0.0)
        traded += [0.0] * (2 - len(traded))
        spent = sum(t * px for t, (_, px) in zip(traded, legs))   # upper bound (fills at or under the limit)
        self.basket_spent += spent
        self.basket_pending += spent
        pairs = min(traded)
        self.stats["baskets"] += 1
        self.stats["basket_pairs"] += pairs
        self.stats["basket_locked"] += pairs * (1 - legs[0][1] - legs[1][1])   # lower bound
        self.m.event({"kind": "basket", **o, "qty": qty, "limits": [px for _, px in legs], "traded": traded,
                      "cost_planned": round(cost, 2), "latency_s": round(time.time() - t0, 1)})
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] BASKET {o['race']}: NO D @{legs[0][1]:.3f} + NO R "
              f"@{legs[1][1]:.3f} = {legs[0][1] + legs[1][1]:.3f} x {qty:,} -> traded {traded[0]:,.0f} / "
              f"{traded[1]:,.0f} ({time.time() - t0:.1f}s)", flush=True)

    def act(self, tops: dict, observe: bool = True):
        with self.lock:
            if observe:
                self.observe(tops)
            for o in self.opportunities(tops, datetime.now(timezone.utc))[:self.a.snipe_per_scan]:
                self.take(o)
            now = time.time()   # baskets need both legs: polled windows and pushed books each carry only some
            self.top_cache.update({ex: (*v, now) for ex, v in tops.items()})
            fresh = {ex: v[:2] for ex, v in self.top_cache.items() if now - v[2] <= self.a.basket_top_age}
            for o in self.baskets(fresh)[:1]:
                self.take_basket(o)

    def on_batch(self, topic: str, event: str, payload: dict):
        """Realtime market batch: evaluate the pushed books at once (no SIG read spent)."""
        tops = {}
        for b in payload.get("books") or []:
            ex = str(b.get("exchangeId") or b.get("exchange_id") or "")
            bids = [float(l["price"]) for l in b.get("bids") or [] if float(l.get("quantity") or 0) > 0]
            asks = [float(l["price"]) for l in b.get("asks") or [] if float(l.get("quantity") or 0) > 0]
            if ex:
                tops[ex] = (max(bids) if bids else None, min(asks) if asks else None)
        if tops:
            self.stats["rt_books"] += len(tops)
            self.act(tops, observe=False)   # the EMA stays on polled tops, so one sweep can't drag it

    def run(self):
        """Scan every --snipe-every s. While the maker's realtime feed is live the scan reads the pushed-book cache
        (no reads; pushed books also trigger on_batch at once) and REST is polled only every --snipe-poll-rt s as a
        cross-check, so the maker gets the reads back. Feed quiet: REST every scan, as before."""
        last_k = last_poll = 0.0
        while not self.halt.is_set():
            t0 = time.time()
            try:
                if t0 - last_k >= self.a.snipe_kalshi_every:
                    self.refresh_kalshi()
                    last_k = t0
                if self.m.books.healthy() and t0 - last_poll < self.a.snipe_poll_rt:
                    self.act(self.m.books.tops())
                    self.stats["scans_rt"] += 1
                else:
                    self.act(self.tops())
                    last_poll = t0
                    self.stats["scans"] += 1
            except Exception as e:  # noqa: BLE001 - a failed scan must not end the thread
                self.m.event({"kind": "snipe_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            self.halt.wait(max(0.3, self.a.snipe_every - (time.time() - t0)))
        self.pool.shutdown(wait=False)
