"""Election-night bot (4 Nov 2026). Fully automatic: calls races itself from the outside venues, sizes the bold
overlay by the gap to 3rd place, never waits for a human. Decision logic: sigbot/night.py; rehearsal: sim_election.py.

  python election.py                    # DRY RUN until the deadline: decisions logged, nothing sent (any date)
  python election.py --steps 3          # dry run, three steps
  python election.py --live             # trades from --start (3 Nov 21:40Z) to the close, stops by the next odd :38

Scheduling: a routine fires at :40 of every odd UTC hour on 3-4 Nov (handovers never land on a :00/:30 poll close);
maker.py stops trading at --start. Kill switch: STOP file or SIGBOT_DISABLED=1 (cancels everything of ours, ends the
run). Remote kill switch, reachable in a running cloud session: the file config/STOP_ELECTION on origin/main (checked
every --lease-every s; every bot cancels everything and stands by, none takes the lease, until it is deleted).
GitHub down (no lease read for --lease-ttl + 30 s): the routine's plain --live run trades without the lease;
maker.py's copy (--secondary) stands by, so a git outage at a hand-over or restart doesn't leave nobody trading.
Network: Kalshi (api.elections.kalshi.com), Polymarket Gamma (gamma-api.polymarket.com) and, for push prices,
ws-subscriptions-clob.polymarket.com (without it Polymarket is polled every --poly-every s). --desk-calls (OFF by
default) also reads public news-desk projections (sigbot/desk.py: www.nbcnews.com, static01.nyt.com): a projected race
with one venue >= --desk-venue is called at once instead of after the venues' 60-300 s hold (config/DESK_CALLS on main
turns it on for every run).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import requests

from sigbot.books import BookCache
from sigbot.calls import NIGHT0, RaceState, first_close
from sigbot.client import ROOT, SigAPIError, SigClient, new_idempotency_key
from sigbot.kalshi import Kalshi
from sigbot.lease import Lease, RemoteStop
from sigbot.markets import TOURNAMENT_SLUG, parse_title
from sigbot.night import Engine, Params
from sigbot.outside import outside_fair
from sigbot.polymarket import Polymarket
from sigbot.races import STATES
from sigbot.swing import Race, SwingMixture

UNIVERSE = ROOT / "config" / "universe.json"
BASELINE = ROOT / "config" / "swing_baseline.json"
RUN_BASELINE = ROOT / "data" / "election" / "swing_baseline.json"
OUT_DIR = ROOT / "data" / "election"
START_UTC = datetime(2026, 11, 3, 21, 40, tzinfo=timezone.utc)
CLOSE_UTC = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
BATCH = 25


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def next_slot_end(now: datetime) -> datetime:
    """Runs hand over at :40 of odd UTC hours (poll closes are all on :00/:30): stop by the next odd :38."""
    t = now.replace(minute=38, second=0, microsecond=0)
    while t.hour % 2 == 0 or t <= now:
        t += timedelta(hours=1)
    return t


def venue_value(q: dict | None) -> float | None:
    """One price per venue quote, also for the one-sided books a called race leaves (bid 0.99 / no ask)."""
    if not q:
        return None
    if q.get("mid") is not None:
        return q["mid"]
    b, a = q.get("bid"), q.get("ask")
    if b is not None and b >= 0.5 and (a is None or a >= 1):
        return b
    if a is not None and 0 < a <= 0.5 and (b is None or b <= 0):
        return a
    return q.get("last")


class LiveOutside:
    """Kalshi every --kalshi-every s, Polymarket by websocket (sigbot/polyws.py) or Gamma every --poly-every s."""

    def __init__(self, kticker: dict, pticker: dict, a):
        self.kt, self.pt, self.a = kticker, pticker, a
        self.k, self.poly = Kalshi(), Polymarket()
        self.kq, self.pq = {}, {}
        self.k_at = self.p_at = 0.0
        self.halt = threading.Event()
        self.stats = defaultdict(int)
        self.feed = None
        if a.polyws:
            try:
                from sigbot.polyws import PolyFeed
                self.feed = PolyFeed(list(set(pticker.values())))
                self.feed.start()
            except Exception as e:  # noqa: BLE001 - Gamma polling covers it
                self.stats["polyws_error"] += 1
                print(f"polyws off: {type(e).__name__}: {str(e)[:120]}", flush=True)
        threading.Thread(target=self._kal, daemon=True, name="kalshi").start()
        threading.Thread(target=self._pol, daemon=True, name="gamma").start()

    def _kal(self):
        tickers = sorted(set(self.kt.values()))
        while not self.halt.is_set():
            t0 = time.time()
            try:
                self.kq, self.k_at = self.k.quotes(tickers), time.time()
            except Exception:  # noqa: BLE001
                self.stats["kalshi_errors"] += 1
            self.halt.wait(max(0.2, self.a.kalshi_every - (time.time() - t0)))

    def _pol(self):
        ids = sorted(set(self.pt.values()))
        while not self.halt.is_set():
            t0 = time.time()
            if self.feed is None or not self.feed.books.live():
                try:
                    self.pq, self.p_at = self.poly.quotes(ids), time.time()
                except Exception:  # noqa: BLE001
                    self.stats["gamma_errors"] += 1
            self.halt.wait(max(0.5, self.a.poly_every - (time.time() - t0)))

    def quotes(self) -> dict:
        now = time.time()
        kq = self.kq if now - self.k_at < self.a.stale_s else {}
        if self.feed is not None and self.feed.books.live():
            pq = self.feed.books.quotes()
        else:
            pq = self.pq if now - self.p_at < self.a.stale_s else {}
        out = {}
        for ex in set(self.kt) | set(self.pt):
            k = kq.get(self.kt.get(ex)) if ex in self.kt else None
            p = pq.get(self.pt.get(ex)) if ex in self.pt else None
            out[ex] = {"kalshi": venue_value(k), "polymarket": venue_value(p), "fair": outside_fair(k, p)}
        return out


def rest_books(tops: dict, depth: dict, fallback_depth: float) -> dict:
    """Books while the realtime feed is quiet: the full book where one was read (the called races), else the REST
    top of book with an assumed depth."""
    out = {ex: {"bids": [(b, fallback_depth)] if b is not None else [],
                "asks": [(s, fallback_depth)] if s is not None else []} for ex, (b, s) in tops.items()}
    for ex, b in depth.items():
        out[ex] = {"bids": list(b["bids"]), "asks": list(b["asks"])}
    return out


def parse_book(raw: dict) -> dict:
    """GET /exchanges/{id}/orderbook -> {"bids", "asks"} as (YES price, qty), best first."""
    raw = raw.get("data", raw) if isinstance(raw, dict) else {}
    lv = lambda side: [(float(x["price"]), float(x["quantity"])) for x in raw.get(side) or []  # noqa: E731
                       if x.get("price") is not None and float(x.get("quantity") or 0) > 0]
    return {"bids": sorted(lv("bids"), key=lambda t: -t[0]), "asks": sorted(lv("asks"), key=lambda t: t[0])}


class SigVenue:
    """Live SIG venue. Reads that can be slow on the night (account, REST books) run in background threads, so a
    step never waits on them: books() and account() return the latest snapshot at once."""

    def __init__(self, a, tid: str, market_ids: list[str], log, client=None, threads: bool = True):
        self.a, self.tid, self.log = a, tid, log
        self.c = client or SigClient()
        self.books_cache = BookCache()
        self.feed = None
        self._third = (None, 0.0)
        self._tops = ({}, 0.0)
        self._depth: dict[str, dict] = {}         # ex -> full REST book {"bids", "asks", "at"} (feed quiet only)
        self._depth_reads: list[float] = []       # times of those reads (budget --fallback-book-reads per minute)
        self.focus: list[str] = []                # set by the engine each step: called races, latest first
        self._acct = None                         # (cash, pos, read started at)
        self._spent: list[tuple] = []             # (time, cost) of our fills, netted off an older cash snapshot
        self.read_errors = defaultdict(int)
        self.halt = threading.Event()
        self.ids = None
        self.trading = False      # set by the lease: a hot standby reads everything and writes nothing
        self.noops = 0
        if client is None and (a.feed or a.live):
            try:
                from sigbot.realtime import RealtimeFeed
                self.feed = RealtimeFeed(SigClient(log=False), tid, market_ids,
                                         lambda topic, ev, p: self.books_cache.apply(p))
                self.feed.start()
            except Exception as e:  # noqa: BLE001 - REST tops are the fallback
                log({"kind": "realtime_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
        if threads:
            threading.Thread(target=self._loop, args=(self._read_account, a.account_every), daemon=True,
                             name="account").start()
            threading.Thread(target=self._loop, args=(self._read_rest, 1.0), daemon=True, name="rest-books").start()

    def writes_ok(self) -> bool:
        return self.a.live and self.trading and not self.a.paper

    def _loop(self, fn, every: float):
        while not self.halt.is_set():
            t0 = time.time()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - counted; the snapshot it would replace stays in use
                self.read_errors[fn.__name__] += 1
                self.log({"kind": "read_error", "what": fn.__name__, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            self.halt.wait(max(0.2, every - (time.time() - t0)))

    # ------------------------------------------------------------------ books
    def books(self) -> dict:
        if self.books_cache.healthy():
            with self.books_cache.lock:
                return {ex: {"bids": list(b["bids"]), "asks": list(b["asks"])} for ex, b in self.books_cache.books.items()}
        tops, _ = self._tops
        fresh = {ex: b for ex, b in list(self._depth.items()) if time.time() - b["at"] <= 3 * self.a.rest_tops_every}
        return rest_books(tops, fresh, self.a.fallback_depth)

    def _read_rest(self):
        """Feed quiet: REST tops of book every --rest-tops-every s, plus full books for the called races (where the
        stale depth is) within --fallback-book-reads per minute, so a call sweep isn't capped at the assumed depth."""
        if self.books_cache.healthy():
            return
        now = time.time()
        if now - self._tops[1] > self.a.rest_tops_every:
            tops = {}
            ids = self.ids or []
            for i in range(0, len(ids), 100):
                r = self.c.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=self.tid)
                for x in (r.get("data") if isinstance(r, dict) else r) or []:
                    tops[str(x.get("exchangeId"))] = (x.get("bestBid"), x.get("bestAsk"))
            self._tops = (tops, time.time())
        self._depth_reads = [t for t in self._depth_reads if now - t < 60]
        for ex in list(self.focus):
            if len(self._depth_reads) >= self.a.fallback_book_reads:
                break
            b = self._depth.get(ex)
            if b and time.time() - b["at"] < self.a.rest_tops_every:
                continue
            self._depth_reads.append(time.time())
            self._depth[ex] = {**parse_book(self.c.get(f"/exchanges/{ex}/orderbook", tournamentId=self.tid,
                                                       depth=50)), "at": time.time()}

    # ------------------------------------------------------------------ account
    def account(self):
        """Latest snapshot, minus our fills since that read began (the engine also nets each step's fills)."""
        if self._acct is None:
            raise RuntimeError("no account snapshot yet")
        cash, pos, at = self._acct
        if time.time() - at > max(60.0, 6 * self.a.account_every):
            self.read_errors["account_stale"] += 1
        return cash - sum(c for t, c in list(self._spent) if t >= at), pos

    def _read_account(self):
        at = time.time()
        cash, pos = self.read_account()
        self._acct = (cash, pos, at)
        self._spent = [(t, c) for t, c in self._spent if t >= at - 300]

    def read_account(self):
        cash = float(self.c.get(f"/tournaments/{TOURNAMENT_SLUG}")["myBalance"])
        raw = self.c.get(f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions")
        pos = {}
        for it in raw.get("positions", []):
            q = float(it.get("quantity") or 0)
            if it.get("settled") or abs(q) < 1:
                continue
            pos[str(it["exchangeId"])] = {"side": "yes" if q > 0 else "no", "qty": int(abs(q)),
                                          "avg": float(it.get("avgCost") or 0)}
        return cash, pos

    def open_orders(self):
        return list(self.c.paginate("/orders", limit=200, status="open", tournamentId=self.tid))

    def _batch(self, chunk: list[dict], expiry_s: float):
        exp = iso(utcnow() + timedelta(seconds=expiry_s))
        orders = [{"exchangeId": o["ex"], "side": o["side"], "action": "buy", "quantity": int(o["qty"]),
                   "price": round(o["price"], 3), "tournamentId": self.tid, "expirationDate": exp} for o in chunk]
        try:
            return self.c.post("/orders/batch", {"idempotencyKey": new_idempotency_key(), "orders": orders})
        except SigAPIError as e:
            if isinstance(e.details, dict) and "results" in e.details:
                return e.details
            return {"error": f"{e.status} {e.code}: {e.message[:160]}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:160]}"}

    def _send(self, orders: list[dict], expiry_s: float) -> list[dict]:
        if self.a.paper and self.trading and orders:
            self.log({"kind": "paper_orders", "expiry_s": expiry_s,
                      "orders": [{k: o.get(k) for k in ("ex", "side", "price", "qty", "role")} for o in orders]})
            return []
        if not self.writes_ok() or not orders:
            return []
        chunks = [orders[i:i + BATCH] for i in range(0, len(orders), BATCH)]
        out = []
        with ThreadPoolExecutor(self.a.writers) as pool:
            for chunk, res in zip(chunks, pool.map(lambda ch: self._batch(ch, expiry_s), chunks)):
                if "error" in res:
                    self.log({"kind": "batch_error", "error": res["error"], "n": len(chunk)})
                    continue
                results = res.get("results", [])
                idx = all(isinstance(r.get("index"), int) and 0 <= r["index"] < len(chunk) for r in results)
                for k, r in enumerate(results):
                    o = chunk[r["index"]] if idx else chunk[k]
                    d = r.get("data") or {}
                    t = float(d.get("quantityTraded") or 0) if r.get("ok") else 0.0
                    # ok + orderId null + nothing traded = SIG's no-op: our resting buys on that exchange already
                    # commit the balance (CLAUDE.md, 9 Oct). Counted, so a night of silent no-ops shows up.
                    noop = bool(r.get("ok")) and d.get("orderId") is None and not t
                    out.append({"ex": o["ex"], "side": o["side"], "role": o.get("role"), "ok": bool(r.get("ok")),
                                "orderId": d.get("orderId"), "filled": t, "cost": t * o["price"], "noop": noop,
                                "error": None if r.get("ok") else json.dumps(d.get("error") or d)[:160]})
                    if noop:
                        self.noops += 1
                    if t:   # stamped on the reply: a snapshot read since then may or may not hold it (counted twice
                        self._spent.append((time.time(), t * o["price"]))   # at worst, never missed)
        return out

    def take(self, orders, expiry_s):
        return self._send(orders, expiry_s)

    def rest(self, orders, expiry_s):
        return [r["orderId"] for r in self._send(orders, expiry_s) if r.get("orderId")]

    def cancel(self, ids):
        if not self.writes_ok():
            return 0
        n = 0
        for i in ids:
            try:
                self.c.delete(f"/orders/{i}")
                n += 1
            except SigAPIError:
                pass
        return n

    def cancel_all(self, force: bool = False) -> bool:
        """force: a new lease holder clears the board BEFORE it starts trading (venue.trading is still False)."""
        if not (self.writes_ok() or (force and self.a.live and not self.a.paper)):
            return False
        try:
            self.c.post("/orders/cancel-all", {"tournamentId": self.tid})
            return True
        except SigAPIError:
            return False

    def third_balance(self):
        v, at = self._third
        if v is not None and time.time() - at < 300:
            return v
        try:
            lb = self.c.get("/leaderboards").get("leaderboard", [])
            row = sorted(lb, key=lambda r: r.get("rank") or 1e9)[2]
            v = 100000.0 + float(row.get("pnl") or 0)
            self._third = (v, time.time())
        except Exception:  # noqa: BLE001
            pass
        return v


class LeaseKeeper:
    """Hot standby: every bot keeps its feeds and model warm; only the lease holder writes. The holder keeps trading
    while git is unreachable; a standby takes over once the holder's lease lapses (crash, killed run).
    check() runs in its own thread (start()): a git fetch + push takes seconds and must not stall the takes.
    remote_stop (RemoteStop.check) is the kill switch every bot honours: while it is set nobody trades or takes the
    lease, and it stays set while git is unreachable (only a successful read clears it).
    GitHub down for long (no lease read for fail_open_s, > the lease ttl): a primary run (the election routine's
    plain `election.py --live`) trades without the lease, so an outage at a hand-over or restart doesn't leave
    the night with no trader; a secondary (maker.py's hand-over copy, --secondary) stops trading after step_down_s
    (= the ttl, before any primary fails open), so at most one bot trades blind. Primary runs never overlap (each
    stops by :38, the next starts at :40). A known remote stop still wins; one set during the outage can't arrive."""

    def __init__(self, lease, venue, eng, notify, log, remote_stop=None, primary=True, fail_open_s=90.0,
                 step_down_s=60.0, clock=time.time):
        self.lease, self.venue, self.eng, self.notify, self.log = lease, venue, eng, notify, log
        self.remote_stop = remote_stop
        self.primary, self.fail_open_s, self.step_down_s, self.clock = primary, fail_open_s, step_down_s, clock
        self.git_ok_at = clock()    # last successful lease read (run start until the first one)
        self.stopped = False
        self.sweeps = 0
        self.holder = False
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.thread = None

    def check(self):
        with self.lock:
            s = self.remote_stop() if self.remote_stop is not None else False
            if s is not None and s != self.stopped:
                self.stopped = s
                self.sweeps = 2 if s else 0
                self.notify("REMOTE STOP (config/STOP_ELECTION on main): cancelling everything, not trading" if s
                            else "remote stop cleared: back to the lease")
            if self.stopped:
                was = self.holder
                self.venue.trading = self.eng.live = False
                self.holder = False
                if self.sweeps:     # twice: the second sweep catches a take that was already in flight
                    self.venue.cancel_all(force=True)
                    self.sweeps -= 1
                if was and self.lease is not None:
                    self.lease.release()
                self.log({"kind": "lease", "state": "remote_stop", "trading": False, "stop_read": s})
                return
            self._ledger_out()
            st = "held" if self.lease is None else self.lease.try_acquire()
            self._ledger_in()
            now = self.clock()
            if st != "error":
                self.git_ok_at = now
            down = now - self.git_ok_at
            if st != "error":
                trading = st == "held"
            elif self.primary:
                trading = self.holder or down >= self.fail_open_s
            else:
                trading = self.holder and down < self.step_down_s
            if trading and not self.holder and st == "error":
                self.notify(f"GitHub unreachable for {down:.0f} s: trading WITHOUT the lease (primary run)")
            elif not trading and self.holder and st == "error":
                self.notify(f"GitHub unreachable for {down:.0f} s: secondary stops trading (primary takes over)")
            if trading and not self.holder:
                # adopt nothing: maker leftovers and a dead run's takes are stale on this night. Cleared before
                # trading starts, so the cancel can't hit this bot's own first takes.
                self.venue.cancel_all(force=True)
                self.notify(f"trading (lease {st})")
            elif not trading and self.holder:
                self.notify(f"lease lost ({st}): standing by")
            self.venue.trading = self.eng.live = trading
            self.holder = trading
            self.log({"kind": "lease", "state": st, "trading": trading,
                      "holder": (self.lease.last or {}).get("owner") if self.lease else "self",
                      **({"git_down_s": round(down), "primary": self.primary} if st == "error" else {})})

    # The night's sweep ledger (adaptive bold, 31 Oct) rides in the lease file: the holder writes it with every
    # renewal, every bot merges what it reads, so a restarted run or a standby taking over knows the night so far.
    # Never allowed to cost the lease itself.
    def _ledger_out(self):
        if self.lease is None:
            return
        try:
            self.lease.extra = {"ledger": self.eng.ledger_state()} if self.holder else {}
        except Exception:  # noqa: BLE001
            self.lease.extra = {}

    def _ledger_in(self):
        try:
            if self.lease is not None:
                self.eng.adopt_ledger((self.lease.last or {}).get("ledger"))
        except Exception:  # noqa: BLE001
            pass

    def _run(self, every: float):
        while not self.halt.wait(every):
            try:
                self.check()
            except Exception as e:  # noqa: BLE001
                self.log({"kind": "lease_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def start(self, every: float):
        self.thread = threading.Thread(target=self._run, args=(every,), daemon=True, name="lease")
        self.thread.start()

    def stop(self):
        """Stop renewing; release the lease if held so the next run (or the standby) takes over at once."""
        self.halt.set()
        if self.thread is not None:
            self.thread.join(timeout=90)
        with self.lock:
            self.venue.trading = self.eng.live = False
            if self.lease is not None and self.holder:
                self._ledger_out()      # the last renewal's ledger is up to 15 s old
                self.lease.release()
            self.holder = False


def load_baseline() -> dict | None:
    """The night's pre-results prior: config/swing_baseline.json (python swing.py --save-baseline), else the one the
    first election run froze and the routine pushed to run-logs (election_logs/swing_baseline.json), else this
    container's own copy."""
    if BASELINE.exists():
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    try:
        if subprocess.run(["git", "fetch", "-q", "origin", "run-logs"], cwd=ROOT, timeout=60).returncode == 0:
            r = subprocess.run(["git", "show", "FETCH_HEAD:election_logs/swing_baseline.json"], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip() and fresh(json.loads(r.stdout)):
                return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        pass
    if RUN_BASELINE.exists() and fresh(json.loads(RUN_BASELINE.read_text(encoding="utf-8"))):
        return json.loads(RUN_BASELINE.read_text(encoding="utf-8"))
    return None


def fresh(b: dict) -> bool:
    """A frozen baseline counts only if saved in the 30 h before the first polls close (not an October test)."""
    t = datetime.fromisoformat(b["saved"].replace("Z", "+00:00"))
    return NIGHT0 - timedelta(hours=30) <= t <= NIGHT0


def build(a, log):
    """Markets, race states, swing model on the frozen baseline (python swing.py --save-baseline on 3 Nov)."""
    u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    markets, states = {}, {}
    senate_ex = house_ex = None
    by_race = defaultdict(dict)
    rtype = {}
    for m in u["markets"]:
        race, rt, party = parse_title(m["title"])
        ex = str(m["exchanges"][0]["id"])
        markets[ex] = {"title": m["title"], "race": race, "party": party, "market": str(m["id"])}
        by_race[race][party] = ex
        rtype[race] = rt
    for race, exs in by_race.items():
        chamber = rtype[race] == "Chamber"
        st = race.split("-")[0] if rtype[race] == "House" else STATES.get(race.rsplit(" ", 1)[0])
        states[race] = RaceState(race, None if chamber else st, exs, chamber=chamber)
        if race == "U.S. Senate":
            senate_ex = (exs["D"], exs["R"])
        if race == "U.S. House":
            house_ex = (exs["D"], exs["R"])
    b = load_baseline()
    if b:
        swing_races = [Race(**r) for r in b["races"]]
        src = f"baseline {b['saved']}"
    else:
        from swing import prices
        swing_races, chamber = prices()
        if utcnow() < NIGHT0:   # pre-results prices: freeze them for every later run (the routine pushes data/)
            RUN_BASELINE.parent.mkdir(parents=True, exist_ok=True)
            RUN_BASELINE.write_text(json.dumps({"saved": iso(utcnow()), "chamber": chamber,
                                                "races": [asdict(r) for r in swing_races]}), encoding="utf-8")
            src = "current prices (saved as the night's baseline)"
        else:
            src = "current prices AFTER the polls opened closing (no baseline found: prior includes results)"
    model = SwingMixture(swing_races)
    log({"kind": "model", "source": src, "races": len(swing_races)})
    print(f"swing model: {len(swing_races)} races from {src}", flush=True)
    return u, markets, states, model, {r.name: r for r in swing_races}, senate_ex, house_ex


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--paper", action="store_true",
                    help="test the live path now (lease on election-lease-paper, realtime books, standby/take-over) "
                         "but send nothing: orders it would send are logged as paper_orders")
    ap.add_argument("--feed", action="store_true", help="dry run: also join SIG's realtime books (read-only)")
    ap.add_argument("--steps", type=int, default=0, help="stop after this many steps (0 = until the deadline)")
    ap.add_argument("--minutes", type=float, default=118)
    ap.add_argument("--start", default=iso(START_UTC), help="live trading starts at (UTC)")
    ap.add_argument("--deadline", help="hard UTC end of this run (maker.py passes its own slot end)")
    ap.add_argument("--no-lease", action="store_true", help="trade without the election-lease (only one bot running)")
    ap.add_argument("--secondary", action="store_true",
                    help="maker.py's hand-over copy: never trades without the lease (GitHub down: only the primary "
                         "election-routine run trades, after --lease-ttl + 30 s)")
    ap.add_argument("--lease-ttl", type=float, default=60.0)
    ap.add_argument("--lease-every", type=float, default=15.0,
                    help="renew / standby / remote-stop poll interval (s)")
    ap.add_argument("--no-remote-stop", action="store_true",
                    help="ignore config/STOP_ELECTION on origin/main (tests without a git remote only)")
    ap.add_argument("--step-s", type=float, default=2.0)
    ap.add_argument("--account-every", type=float, default=10.0)
    ap.add_argument("--kalshi-every", type=float, default=2.0)
    ap.add_argument("--poly-every", type=float, default=3.0)
    ap.add_argument("--stale-s", type=float, default=20.0, help="outside quotes older than this are ignored")
    ap.add_argument("--no-polyws", dest="polyws", action="store_false")
    ap.add_argument("--rest-tops-every", type=float, default=6.0)
    ap.add_argument("--fallback-depth", type=float, default=1000.0)
    ap.add_argument("--fallback-book-reads", type=float, default=30.0,
                    help="feed quiet: full orderbook reads per minute for the called races (their real depth)")
    ap.add_argument("--writers", type=int, default=3)
    ap.add_argument("--bold-at", default="2026-11-03T22:30Z", help="comma-separated UTC times for bold decisions")
    ap.add_argument("--no-bold", action="store_true")
    ap.add_argument("--desk-calls", action="store_true",
                    help="also call races from public news-desk projections (NBC Decision Desk, AP via the NYT) when "
                         "one venue agrees >= --desk-venue (sigbot/desk.py; OFF until verified on the night's feeds). "
                         "The file config/DESK_CALLS turns it on for every run (the routine's and maker.py's copy)")
    ap.add_argument("--desk-sources", default="nbc,nyt")
    ap.add_argument("--desk-every", type=float, default=10.0, help="desk poll interval (s)")
    ap.add_argument("--desk-venue", type=float, default=0.95)
    ap.add_argument("--desk-venue-two", type=float, default=0.90, help="venue gate when two desks agree")
    ap.add_argument("--desk-date", default="2026-11-03", help="election date in the NYT results URLs")
    p0 = Params()
    tunable = ("call_margin_hi", "call_margin_lo", "lag_edge", "model_edge", "chamber_edge", "pair_edge", "agree",
              "model_agree", "race_cap_call", "race_cap", "reserve", "min_take", "take_expiry_s", "exit_px",
              "recycle_below", "exit_expiry_s", "risk_off_min", "bold_every_min", "bold_max", "lock_keep",
              "race_cap_sure", "recycle_always", "recycle_bid_min", "recycle_take_gain",
              "bold_adaptive", "bold_min_calls", "gain_prior_calls", "gain_rel_sd", "gain_rate_h")
    for k in tunable:
        ap.add_argument("--" + k.replace("_", "-"), type=float, default=getattr(p0, k))
    a = ap.parse_args()
    if a.paper:
        a.live, a.feed = True, True
        a.start = iso(utcnow())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_ts = utcnow().strftime("%Y%m%dT%H%M%SZ")
    log_path = OUT_DIR / f"election_{run_ts}.jsonl"
    lock = threading.Lock()

    def log(d):
        with lock, log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso(utcnow()), **d}, default=str) + "\n")

    topic = os.environ.get("NTFY_TOPIC")

    def notify(msg):
        log({"kind": "notify", "msg": msg})
        print(f"[{utcnow():%H:%M:%S}Z] {msg}", flush=True)
        if topic and a.live and not a.paper:
            try:
                requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(), timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def stop_reason(now):
        if os.environ.get("SIGBOT_DISABLED") == "1" or (ROOT / "STOP").exists():
            return "kill switch"
        if a.live and now >= CLOSE_UTC - timedelta(minutes=1):
            return "trading closed"
        return None

    now = utcnow()
    start = datetime.fromisoformat(a.start.replace("Z", "+00:00"))
    if a.live and now < start:
        print(f"Not trading: live election mode starts {start:%Y-%m-%d %H:%M}Z (maker.py trades until then).")
        return
    deadline = min(now + timedelta(minutes=a.minutes), next_slot_end(now)) if a.live else now + timedelta(minutes=a.minutes)
    if a.deadline:
        deadline = min(now + timedelta(minutes=a.minutes), datetime.fromisoformat(a.deadline.replace("Z", "+00:00")))
    if stop_reason(now):
        print(f"Not trading: {stop_reason(now)}")
        return

    u, markets, states, model, swing_races, senate_ex, house_ex = build(a, log)
    p = Params()
    for k in tunable:
        setattr(p, k, getattr(a, k))
    p.model_async = p.bold_async = True
    p.bold_times = () if a.no_bold else tuple(datetime.fromisoformat(t.strip().replace("Z", "+00:00"))
                                              for t in a.bold_at.split(",") if t.strip())
    if a.no_bold:
        p.bold_every_min, p.bold_on = 1e9, False
    venue = SigVenue(a, u["tournamentId"], sorted({m["market"] for m in markets.values()}), log)
    venue.ids = sorted(markets)
    outside = LiveOutside({ex: t for ex, t in u["kalshi"].items() if ex in markets},
                          {ex: t for ex, t in u["polymarket"].items() if ex in markets}, a)
    calls_path = OUT_DIR / "calls.json"
    sp = ROOT / "config" / "structural.json"     # markets settled by structure (research/structural_audit.py, checked by hand)
    structural = {ex: r["yes"] for ex, r in json.loads(sp.read_text(encoding="utf-8")).items()} if sp.exists() else {}
    leaders = {}
    lp = ROOT / "config" / "leader_exposure.json"   # research/leader_exposure.py --write; used only if validated and < 48 h old
    if lp.exists():
        le = json.loads(lp.read_text(encoding="utf-8"))
        age_h = (utcnow() - datetime.fromisoformat(le["saved"].replace("Z", "+00:00"))).total_seconds() / 3600
        if le.get("trusted") and age_h < 48:
            leaders = le["leaders"]
        log({"kind": "leaders", "used": bool(leaders), "trusted": le.get("trusted"), "age_h": round(age_h, 1)})
    desk = None
    if a.desk_calls or (ROOT / "config" / "DESK_CALLS").exists():
        from sigbot.desk import DeskFeed
        desk = DeskFeed(set(states), sources=tuple(s.strip() for s in a.desk_sources.split(",") if s.strip()),
                        year=int(a.desk_date[:4]), date=a.desk_date, every=a.desk_every, log=log,
                        due_states=lambda: sorted({s.state for s in states.values() if s.state and not s.chamber
                                                   and s.status != "CALLED" and utcnow() >= first_close(s.state)}))
        desk.start()
    eng = Engine(venue, outside, markets, states, p, model=model, swing_races=swing_races, log=log, notify=notify,
                 senate_ex=senate_ex, house_ex=house_ex, live=False, structural=structural, leaders=leaders,
                 desk=desk, detector_kw={"desk_venue": a.desk_venue,
                                                    "desk_venue_two": a.desk_venue_two})
    if calls_path.exists():
        eng.det.load(calls_path.read_text(encoding="utf-8"))
    print(f"election {'PAPER (live path, sends nothing)' if a.paper else 'LIVE' if a.live else 'DRY RUN'}: {len(markets)} exchanges, {len(states)} races, until "
          f"{deadline:%H:%M}Z, bold at {[t.strftime('%d %H:%MZ') for t in p.bold_times]}, log {log_path.name}",
          flush=True)
    lease = None
    if a.live and not a.no_lease:
        lease = Lease(f"{platform.node()}-{os.getpid()}-{run_ts}", ROOT, ttl_s=a.lease_ttl,
                      branch="election-lease-paper" if a.paper else "election-lease")
    keeper = LeaseKeeper(lease, venue, eng, notify, log,
                         remote_stop=None if a.no_remote_stop else RemoteStop(ROOT).check, primary=not a.secondary,
                         fail_open_s=a.lease_ttl + 30, step_down_s=a.lease_ttl) if a.live else None

    time.sleep(3)   # first outside quotes and account snapshot
    if keeper is not None:
        keeper.check()
        keeper.start(a.lease_every)
    if keeper is not None and keeper.stopped:
        print(f"[{utcnow():%H:%M:%S}Z] REMOTE STOP is set (config/STOP_ELECTION on origin/main): not trading; "
              f"resumes within ~{a.lease_every:.0f} s of it being deleted", flush=True)
    elif a.live and not venue.trading and lease is not None and lease.state == "error":
        print(f"[{utcnow():%H:%M:%S}Z] GitHub unreachable (lease): "
              + ("standing by (secondary)" if a.secondary else
                 f"trading without the lease if it stays down {a.lease_ttl + 30:.0f} s"), flush=True)
    elif a.live and not venue.trading:
        print(f"[{utcnow():%H:%M:%S}Z] hot standby: another election.py holds the lease "
              f"({(lease.last or {}).get('owner')}); taking over if it lapses", flush=True)
    n, last_acct, last_dump = 0, 0.0, 0.0
    try:
        while True:
            n += 1
            t0 = time.time()
            now = utcnow()
            why = stop_reason(now)
            if why:
                notify(f"stopping: {why}")
                if why == "kill switch":
                    venue.cancel_all()
                break
            try:
                acct = t0 - last_acct >= a.account_every
                r = eng.step(now, refresh_account=acct)
                if acct:
                    last_acct = t0
                eng.cash -= sum(f.get("cost", 0) for f in r["fills"])
                if r["takes"] or n % 30 == 1:
                    called = sum(1 for s in states.values() if s.status == "CALLED")
                    print(f"[{now:%H:%M:%S}Z] {'' if venue.trading or not a.live else '(standby) '}#{n} cash {eng.cash:,.0f} pos {len(eng.pos)} called {called} "
                          f"takes {len(r['takes'])} ({sum(o['cost'] for o in r['takes']):,.0f}, edge "
                          f"{sum(o['edge'] for o in r['takes']):,.0f}) filled "
                          f"{sum(f.get('filled', 0) for f in r['fills']):,.0f} | books "
                          f"{'rt' if venue.books_cache.healthy() else 'rest'} | poly "
                          f"{'ws' if outside.feed and outside.feed.books.live() else 'rest'}", flush=True)
                    for o in r["takes"][:8]:
                        print(f"     {o['role']} BUY {o['qty']:>7,} {o['side'].upper():<3} <= {o['price']:.3f} "
                              f"(fair {o['fair_s'] if o['fair_s'] is None else round(o['fair_s'], 3)}) "
                              f"{markets[o['ex']]['title'][:60]}", flush=True)
            except SigAPIError as e:
                log({"kind": "error", "error": str(e)[:300]})
            except Exception as e:  # noqa: BLE001 - one bad step must not end the night
                log({"kind": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"})
                print(f"[{now:%H:%M:%S}Z] error: {type(e).__name__}: {str(e)[:200]}", flush=True)
            if t0 - last_dump > 60:
                calls_path.write_text(eng.det.dump(), encoding="utf-8")
                last_dump = t0
            if (a.steps and n >= a.steps) or utcnow() + timedelta(seconds=a.step_s) >= deadline:
                break
            time.sleep(max(0.2, a.step_s - (time.time() - t0)))
    finally:
        if keeper is not None:
            keeper.stop()         # releases the lease: the next run (or the standby) takes over at once
        outside.halt.set()
        venue.halt.set()
        if desk is not None:
            desk.stop()
        if venue.feed:
            venue.feed.stop()
        if outside.feed:
            outside.feed.stop()
        calls_path.write_text(eng.det.dump(), encoding="utf-8")
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        (logs / f"election_{run_ts}.json").write_text(json.dumps(
            {"run": run_ts, "live": a.live, "steps": n, "cash": eng.cash, "positions": eng.pos,
             "stats": dict(eng.stats), "outside": dict(outside.stats), "noops": venue.noops,
             "read_errors": dict(venue.read_errors), "desk": dict(desk.stats) if desk else None,
             "calls": {k: v.winner for k, v in states.items() if v.status == "CALLED"}}, indent=1, default=str))
        print(f"done: {n} steps, log {log_path.name}", flush=True)


if __name__ == "__main__":
    main()
