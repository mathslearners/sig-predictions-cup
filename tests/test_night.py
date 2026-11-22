"""Offline checks for the election-night pieces. Run: python tests/test_night.py (or pytest)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigbot.bold import Lot, optimise                      # noqa: E402
from sigbot.calls import NIGHT0, CallDetector, RaceState   # noqa: E402
from sigbot.night import Engine, Params                    # noqa: E402
from sigbot.polyws import PolyBooks                        # noqa: E402


def race(name="Georgia Senate", st="GA", d="1", r="2"):
    return RaceState(name, st, {"D": d, "R": r})


def test_no_call_before_poll_close():
    det = CallDetector({"Georgia Senate": race()})
    ev = det.update({"1": {"kalshi": 0.99, "polymarket": 0.99}}, NIGHT0 - timedelta(minutes=5))
    assert ev == [] and det.races["Georgia Senate"].status == "PRE"


def test_call_needs_hold_or_hard():
    det = CallDetector({"Georgia Senate": race()})
    t = NIGHT0 + timedelta(minutes=40)
    q = {"1": {"kalshi": 0.975, "polymarket": 0.98}}
    assert det.update(q, t) == [] and det.update(q, t + timedelta(seconds=31)) == []
    ev = det.update(q, t + timedelta(seconds=61))
    assert ev and ev[0]["winner"] == "D" and ev[0]["source"] == "both"
    det2 = CallDetector({"Georgia Senate": race()})
    q2 = {"2": {"kalshi": 0.992, "polymarket": 0.955}}
    assert det2.update(q2, t) == []
    ev = det2.update(q2, t + timedelta(seconds=61))
    assert ev and ev[0]["winner"] == "R" and ev[0]["source"] == "hard"


def test_contested_race_waits_after_close():
    det = CallDetector({"Georgia Senate": race()})
    det.update({"1": {"kalshi": 0.45, "polymarket": 0.46}}, NIGHT0 - timedelta(minutes=1))
    det.update({"1": {"kalshi": 0.47, "polymarket": 0.46}}, NIGHT0 + timedelta(seconds=1))      # closes at 0.47
    q = {"1": {"kalshi": 0.99, "polymarket": 0.99}}                                          # NY-04 2024 style spike
    t = NIGHT0 + timedelta(minutes=19)
    assert det.update(q, t) == [] and det.update(q, t + timedelta(seconds=120)) == []
    assert det.update(q, t + timedelta(seconds=301))


def test_one_venue_spike_is_not_a_call():
    det = CallDetector({"Georgia Senate": race()})
    t = NIGHT0 + timedelta(minutes=10)
    assert det.update({"1": {"kalshi": 0.995, "polymarket": 0.70}}, t) == []


def test_safe_seat_called_at_the_close():
    det = CallDetector({"Georgia Senate": race()})
    q = {"1": {"kalshi": 0.985, "polymarket": 0.98}}
    det.update(q, NIGHT0 - timedelta(minutes=10))          # hold starts before the close
    ev = det.update(q, NIGHT0 + timedelta(seconds=2))
    assert ev and ev[0]["source"] == "both"


def test_retract():
    det = CallDetector({"Georgia Senate": race()})
    t = NIGHT0 + timedelta(minutes=10)
    det.manual("Georgia Senate", "D", t)
    ev = det.update({"1": {"kalshi": 0.80, "polymarket": 0.85}}, t + timedelta(seconds=5))
    assert ev and ev[0]["kind"] == "retract" and det.races["Georgia Senate"].status == "CLOSED"


class Venue:
    def __init__(self, books, cash=100000.0, pos=None, orders=None):
        self._b, self.cash, self.pos, self.orders = books, cash, pos or {}, orders or []
        self.taken, self.rested, self.cancelled = [], [], []

    def books(self):
        return self._b

    def account(self):
        return self.cash, self.pos

    def open_orders(self):
        return self.orders

    def take(self, orders, expiry_s):
        self.taken += orders
        return []

    def rest(self, orders, expiry_s):
        self.rested += orders
        return []

    def cancel(self, ids):
        self.cancelled += ids
        return len(ids)

    def cancel_all(self):
        return True

    def third_balance(self):
        return None


class Outside:
    def __init__(self, q):
        self.q = q

    def quotes(self):
        return self.q


def engine(books, quotes, **kw):
    states = {"Georgia Senate": race()}
    markets = {"1": {"party": "D", "race": "Georgia Senate"}, "2": {"party": "R", "race": "Georgia Senate"}}
    v = Venue(books, **kw)
    return Engine(v, Outside(quotes), markets, states, Params(), log=lambda e: None), v


def test_call_sweep_buys_winner_and_loser_no():
    books = {"1": {"bids": [(0.55, 1000)], "asks": [(0.60, 2000), (0.99, 500)]},   # D YES offered 0.60
             "2": {"bids": [(0.40, 3000)], "asks": [(0.45, 1000)]}}                 # R YES bid 0.40 = R NO at 0.60
    q = {"1": {"kalshi": 0.995, "polymarket": 0.99, "fair": {"p": 0.99, "buffer": 0}},
         "2": {"kalshi": 0.005, "polymarket": 0.01, "fair": {"p": 0.01, "buffer": 0}}}
    e, v = engine(books, q)
    e.det.manual("Georgia Senate", "D", NIGHT0 + timedelta(minutes=5))
    e.step(NIGHT0 + timedelta(minutes=5))
    got = {(o["ex"], o["side"]): o for o in v.taken}
    assert got[("1", "yes")]["qty"] == 2000 and got[("1", "yes")]["role"] == "C"   # 0.99 level not taken
    assert got[("2", "no")]["qty"] == 3000


def test_no_take_when_venues_disagree():
    books = {"1": {"bids": [(0.40, 1000)], "asks": [(0.45, 2000)]}, "2": {"bids": [(0.50, 100)], "asks": [(0.55, 100)]}}
    q = {"1": {"kalshi": 0.60, "polymarket": 0.45, "fair": {"p": 0.60, "buffer": 0.08}}}
    e, v = engine(books, q)
    e.step(NIGHT0 - timedelta(hours=3))
    assert not [o for o in v.taken if o["ex"] == "1"]


def test_pair_arb():
    books = {"1": {"bids": [(0.40, 1000)], "asks": [(0.45, 2000)]}, "2": {"bids": [(0.40, 1000)], "asks": [(0.50, 1500)]}}
    e, v = engine(books, {})
    e.step(NIGHT0 - timedelta(hours=3))
    pairs = [o for o in v.taken if o["role"] == "P"]
    assert len(pairs) == 2 and all(o["qty"] == 1500 and o["side"] == "yes" for o in pairs)


def test_pair_trimmed_as_one():
    books = {"1": {"bids": [], "asks": [(0.45, 20000)]}, "2": {"bids": [], "asks": [(0.50, 20000)]}}
    e, v = engine(books, {}, cash=5000.0)
    e.step(NIGHT0 - timedelta(hours=3))
    legs = [o for o in v.taken if o["role"] == "P"]
    assert len(legs) == 2 and legs[0]["qty"] == legs[1]["qty"] and sum(o["cost"] for o in legs) <= 4000 + 1


def test_no_retake_within_cooldown():
    books = {"1": {"bids": [], "asks": [(0.45, 2000)]}, "2": {"bids": [], "asks": [(0.50, 1500)]}}
    e, v = engine(books, {})
    t = NIGHT0 - timedelta(hours=3)
    e.step(t)
    n = len(v.taken)
    e.step(t + timedelta(seconds=5))
    assert len(v.taken) == n
    e.step(t + timedelta(seconds=20))
    assert len(v.taken) == 2 * n


def test_risk_off_cancels_before_close():
    orders = [{"id": "a", "exchangeId": "1"}, {"id": "b", "exchangeId": "9"}]
    e, v = engine({}, {}, orders=orders)
    e.step(NIGHT0 - timedelta(minutes=30))
    assert v.cancelled == []
    e.step(NIGHT0 - timedelta(minutes=4))
    assert v.cancelled == ["a"]


def test_recycle_offers_called_winner_when_cash_short():
    books = {"1": {"bids": [(0.98, 10)], "asks": [(0.60, 100000)]}, "2": {"bids": [], "asks": []}}
    q = {"1": {"kalshi": 0.995, "polymarket": 0.995, "fair": {"p": 0.995, "buffer": 0}}}
    e, v = engine(books, q, cash=5000.0, pos={"1": {"side": "yes", "qty": 8000, "avg": 0.6}})
    e.det.manual("Georgia Senate", "D", NIGHT0 + timedelta(minutes=5))
    e.step(NIGHT0 + timedelta(minutes=5))
    x = [o for o in v.rested if o["role"] == "X"]
    assert x and x[0]["side"] == "no" and x[0]["price"] == 0.01 and x[0]["qty"] == 8000


def test_bold_ahead_keeps_p_behind_concentrates():
    rng = np.random.default_rng(0)
    n = 8000
    z = rng.normal(size=n)
    pay = {str(i): (0.8 * z + 0.6 * rng.normal(size=n)) > 1.2816 for i in range(6)}   # fair 0.10 long shots
    lots = [Lot(str(i), "yes", 0.13, 1e6) for i in range(6)] + [Lot(str(i), "no", 0.88, 1e6) for i in range(6)]
    G = np.zeros(n)
    ahead = optimise(200e3, {}, lots, pay, np.full(n, 150e3), G)
    assert ahead["p_after"] == 1.0
    behind = optimise(200e3, {}, lots, pay, np.full(n, 900e3), G)   # SIG's premium makes the long shot -EV
    assert behind["p_after"] > 0.08 and behind["orders"][0]["side"] == "yes"


def test_structural_market_is_certain():
    books = {"1": {"bids": [(0.06, 5000)], "asks": [(0.08, 5000)]}, "2": {"bids": [], "asks": []}}
    states = {"Georgia Senate": race()}
    markets = {"1": {"party": "D", "race": "Georgia Senate"}, "2": {"party": "R", "race": "Georgia Senate"}}
    v = Venue(books)
    e = Engine(v, Outside({}), markets, states, Params(), log=lambda e: None, structural={"1": 0.0})
    e.step(NIGHT0 - timedelta(hours=3))
    got = [o for o in v.taken if o["ex"] == "1"]
    assert got and got[0]["side"] == "no" and got[0]["price"] == 0.98     # NO of a certain loser, bid 0.06 = NO 0.94


def test_lock_keep_relaxes_late():
    e, v = engine({}, {})
    e.now = NIGHT0 + timedelta(hours=2)
    assert e.lock_keep_now() == Params.lock_keep
    e.now = NIGHT0 + timedelta(hours=7)
    assert abs(e.lock_keep_now() - Params.lock_keep / 2) < 1e-9
    e.now = NIGHT0 + timedelta(hours=11)
    assert e.lock_keep_now() == 0


def test_recycle_after_recent_block_even_with_cash():
    books = {"1": {"bids": [(0.98, 10)], "asks": [(0.60, 100000)]}, "2": {"bids": [], "asks": []}}
    q = {"1": {"kalshi": 0.995, "polymarket": 0.995, "fair": {"p": 0.995, "buffer": 0}}}
    e, v = engine(books, q, cash=50000.0, pos={"1": {"side": "yes", "qty": 8000, "avg": 0.6}})
    e.det.manual("Georgia Senate", "D", NIGHT0 + timedelta(minutes=5))
    e.step(NIGHT0 + timedelta(minutes=5))      # the sweep wants far more than 50k: blocked -> recycle
    assert [o for o in v.rested if o["role"] == "X"]


def test_leader_target_follows_outcomes():
    from sigbot.bold import leader_targets
    rng = np.random.default_rng(0)
    n = 4000
    x = rng.random(n) < 0.5
    leaders = {"a": {"balance": 900e3, "exposures": {"9": 400e3}},     # long 400k YES at mid 0.5
               "b": {"balance": 800e3, "exposures": {}}, "c": {"balance": 700e3, "exposures": {}}}
    T = leader_targets(leaders, {"9": x}, {"9": 0.5}, n, rng, 2, 0.0, 0.0)     # 2nd highest of the three
    assert abs(T[x].mean() - 800e3) < 1 and abs(T[~x].mean() - 700e3) < 1   # a drops to 700k when 9 is NO


def test_polyws_books():
    b = PolyBooks({"tokY": "m1"})
    b.handle([{"event_type": "book", "asset_id": "tokY", "bids": [{"price": "0.48", "size": "10"}],
               "asks": [{"price": "0.52", "size": "5"}]}])
    assert b.quotes()["m1"]["mid"] == 0.5
    b.handle({"event_type": "price_change", "price_changes": [
        {"asset_id": "tokY", "price": "0.50", "size": "7", "side": "BUY", "best_bid": "0.50", "best_ask": "0.52"}]})
    assert b.quotes()["m1"]["bid"] == 0.5
    b.handle({"event_type": "price_change", "asset_id": "tokY", "changes": [{"price": "0.52", "side": "SELL", "size": "0"}]})
    assert b.quotes()["m1"]["ask"] is None


# ---------------------------------------------------------------- robustness (18 Oct review)
def closed_race(det, name="Georgia Senate", lead=0.85):
    """Polls close with the leader at `lead` (not contested), venues both quoting."""
    det.update({"1": {"kalshi": lead, "polymarket": lead}}, NIGHT0 + timedelta(seconds=1))
    return NIGHT0 + timedelta(minutes=45)


def test_one_venue_needs_long_hold_and_capped_conf():
    """A race only one venue quotes (Polymarket-only House seats, or Kalshi stale): the NY-04 spike case."""
    det = CallDetector({"Georgia Senate": race()})
    t = closed_race(det)
    q = {"1": {"kalshi": None, "polymarket": 0.992}}
    assert det.update(q, t) == [] and det.update(q, t + timedelta(seconds=61)) == []
    assert det.update(q, t + timedelta(seconds=299)) == []
    ev = det.update(q, t + timedelta(seconds=301))
    assert ev and ev[0]["source"] == "single" and ev[0]["conf"] <= 0.985
    r = det.races["Georgia Senate"]
    det.update({"1": {"kalshi": None, "polymarket": 0.995}}, t + timedelta(seconds=320))
    assert r.conf <= 0.985                         # one venue alone never raises it to the 25% / 0.97 tier
    det.update({"1": {"kalshi": 0.995, "polymarket": 0.995}}, t + timedelta(seconds=330))
    assert r.conf == 0.995                         # both venues: full confidence


def test_one_venue_call_is_sized_as_uncertain():
    books = {"1": {"bids": [], "asks": [(0.90, 100000)]}, "2": {"bids": [], "asks": []}}
    q = {"1": {"kalshi": None, "polymarket": 0.992, "fair": {"p": 0.992, "buffer": 0.005}}}
    e, v = engine(books, q, cash=100000.0)
    t = closed_race(e.det)
    for s in (0, 120, 301):
        e.step(t + timedelta(seconds=s))
    assert e.races["Georgia Senate"].status == "CALLED"
    c = [o for o in v.taken if o["role"] == "C"]
    assert c and all(o["price"] <= 0.935 for o in c)              # margin 0.05, not 0.02
    assert sum(o["cost"] for o in c) <= 0.10 * 100000 + 1          # 10% per-race cap, not 25%


def test_hard_call_needs_both_venues():
    det = CallDetector({"Georgia Senate": race()})
    t = closed_race(det)
    q = {"1": {"kalshi": 0.995, "polymarket": None}}
    det.update(q, t)
    assert det.update(q, t + timedelta(seconds=61)) == []


class Failing(Venue):
    def __init__(self, *x, fail=("account",), **k):
        super().__init__(*x, **k)
        self.fail = set(fail)

    def account(self):
        if "account" in self.fail:
            raise TimeoutError("GET /tournaments slow")
        return super().account()

    def open_orders(self):
        if "orders" in self.fail:
            raise TimeoutError("GET /orders slow")
        return super().open_orders()


def failing_engine(books, quotes, fail):
    states = {"Georgia Senate": race()}
    markets = {"1": {"party": "D", "race": "Georgia Senate"}, "2": {"party": "R", "race": "Georgia Senate"}}
    v = Failing(books, cash=100000.0, fail=fail)
    return Engine(v, Outside(quotes), markets, states, Params(), log=lambda e: None), v


SWEEP_BOOKS = {"1": {"bids": [(0.55, 1000)], "asks": [(0.60, 2000)]}, "2": {"bids": [(0.40, 3000)], "asks": [(0.45, 1000)]}}
SWEEP_Q = {"1": {"kalshi": 0.995, "polymarket": 0.99, "fair": {"p": 0.99, "buffer": 0}},
           "2": {"kalshi": 0.005, "polymarket": 0.01, "fair": {"p": 0.01, "buffer": 0}}}


def test_account_error_keeps_last_snapshot_and_takes():
    e, v = failing_engine(SWEEP_BOOKS, SWEEP_Q, fail=())
    t = NIGHT0 + timedelta(minutes=5)
    e.det.manual("Georgia Senate", "D", t)
    e.step(t - timedelta(seconds=30))                 # good snapshot
    v.fail.add("account")
    v.taken.clear()
    e.step(t, refresh_account=True)
    assert e.cash == 100000.0 and v.taken and e.stats["account_errors"] == 1


def test_risk_off_error_does_not_block_takes_and_retries():
    e, v = failing_engine(SWEEP_BOOKS, SWEEP_Q, fail=("orders",))
    t = NIGHT0 + timedelta(minutes=5)
    e.det.manual("Georgia Senate", "D", t)
    e.step(t)
    assert v.taken and "GA" not in e.risk_off_done and e.stats["risk_off_errors"] == 1
    v.fail.clear()
    e.step(t + timedelta(seconds=2))
    assert "GA" in e.risk_off_done


def test_bold_async_does_not_block_the_step():
    import time as _t

    class SlowBold(Engine):
        def refresh_model(self, ext, now, force=False):
            pass

        def _bold(self, books, now, why):
            _t.sleep(1.5)
            self.bold_ran = True

    states = {"Georgia Senate": race()}
    markets = {"1": {"party": "D", "race": "Georgia Senate"}, "2": {"party": "R", "race": "Georgia Senate"}}
    p = Params()
    p.bold_async, p.bold_times, p.bold_adaptive = True, (NIGHT0,), 0.0
    e = SlowBold(Venue(SWEEP_BOOKS, cash=100000.0), Outside(SWEEP_Q), markets, states, p, model=object(),
                 log=lambda x: None)
    t0 = _t.perf_counter()
    e.step(NIGHT0 + timedelta(minutes=1))
    assert _t.perf_counter() - t0 < 0.5 and e._bold_thread is not None
    e.step(NIGHT0 + timedelta(minutes=1, seconds=2))   # plan still running: no second one
    e._bold_thread.join(5)
    assert getattr(e, "bold_ran", False)


def test_race_cap_counts_held_positions_and_recent_fills():
    """25% per race on a >= 0.99 call means 25% in total, not 25% per step (a deep stale book refills every 15 s)."""
    class Filling(Venue):
        def take(self, orders, expiry_s):
            self.taken += orders
            return [{"ex": o["ex"], "side": o["side"], "filled": o["qty"], "cost": o["cost"]} for o in orders]

    books = {"1": {"bids": [], "asks": [(0.90, 1e6)]}, "2": {"bids": [], "asks": []}}
    states = {"Georgia Senate": race()}
    markets = {"1": {"party": "D", "race": "Georgia Senate"}, "2": {"party": "R", "race": "Georgia Senate"}}
    v = Filling(books, cash=200000.0)
    e = Engine(v, Outside(SWEEP_Q), markets, states, Params(), log=lambda x: None)
    t = NIGHT0 + timedelta(minutes=5)
    e.det.manual("Georgia Senate", "D", t)
    e.step(t)
    first = sum(o["cost"] for o in v.taken)
    assert 49000 <= first <= 50000                        # 25% of the 200k bankroll
    v.taken.clear()
    e.step(t + timedelta(seconds=16), refresh_account=False)      # snapshot not updated yet: the fill still counts
    assert not v.taken
    v.cash, v.pos = 200000.0 - first, {"1": {"side": "yes", "qty": int(first / 0.9), "avg": 0.9}}
    e.step(t + timedelta(seconds=60))                     # snapshot now holds it
    assert sum(o["cost"] for o in v.taken) < 100          # the race is full
    v.pos = {"1": {"side": "yes", "qty": int(first / 0.9 / 2), "avg": 0.9}}   # half of it sold back (recycled)
    v.cash += first / 2
    v.taken.clear()
    e.step(t + timedelta(seconds=120))
    assert 20000 <= sum(o["cost"] for o in v.taken) <= 30000     # tops up to the cap again


def test_race_cap_sure_only_while_both_venues_hold_the_winner():
    books = {"1": {"bids": [], "asks": [(0.90, 1e6)]}, "2": {"bids": [], "asks": []}}
    t = NIGHT0 + timedelta(minutes=5)
    for q1, want in (({"kalshi": 0.995, "polymarket": 0.99}, 100000), ({"kalshi": 0.995, "polymarket": 0.985}, 50000)):
        quotes = {"1": {**q1, "fair": {"p": 0.99, "buffer": 0}}, "2": SWEEP_Q["2"]}
        e, v = engine(books, quotes, cash=200000.0)
        e.p.race_cap_sure = 0.5
        e.det.manual("Georgia Senate", "D", t)
        e.step(t)
        assert want - 1000 <= sum(o["cost"] for o in v.taken) <= want


def test_recycle_sells_called_winners_into_bids_when_takes_wait_for_cash():
    books = {"1": {"bids": [(0.98, 20000), (0.97, 5000)], "asks": [(0.99, 1000)]},
             "2": {"bids": [(0.30, 100000)], "asks": [(0.995, 1000)]}}         # R YES bid 0.30 = R NO at 0.70
    t = NIGHT0 + timedelta(minutes=5)
    for bid_min in (0.0, 0.975):
        e, v = engine(books, SWEEP_Q, cash=1000.0, pos={"1": {"side": "yes", "qty": 50000, "avg": 0.6}})
        e.p.recycle_bid_min = bid_min
        e.det.manual("Georgia Senate", "D", t)
        e.step(t)
        x = [o for o in v.taken if o["role"] == "X"]
        if not bid_min:
            assert not x                                    # off by default
        else:                                               # only the 0.98 level clears 0.975
            assert len(x) == 1 and x[0]["ex"] == "1" and x[0]["side"] == "no" and x[0]["qty"] == 20000
            assert abs(x[0]["price"] - 0.02) < 1e-9


def test_recycle_always_offers_winners_with_cash_to_spare():
    books = {"1": {"bids": [(0.96, 10)], "asks": [(0.99, 10)]}, "2": {"bids": [], "asks": []}}
    t = NIGHT0 + timedelta(minutes=5)
    for always in (0.0, 1.0):
        e, v = engine(books, SWEEP_Q, cash=150000.0, pos={"1": {"side": "yes", "qty": 8000, "avg": 0.6}})
        e.p.recycle_always = always
        e.det.manual("Georgia Senate", "D", t)
        e.step(t)
        assert bool([o for o in v.rested if o["role"] == "X"]) == bool(always)


# ---- election.py live venue, with a fake SIG client (no network)
class FakeClient:
    def __init__(self, depth=50000):
        self.calls, self.depth = [], depth

    def get(self, path, **params):
        self.calls.append(path)
        if path == "/exchanges/prices":
            return {"data": [{"exchangeId": i, "bestBid": 0.40, "bestAsk": 0.60} for i in params["ids"].split(",")]}
        if path.endswith("/orderbook"):
            return {"bids": [{"price": 0.40, "quantity": self.depth}, {"price": 0.45, "quantity": 10}],
                    "asks": [{"price": 0.60, "quantity": self.depth}, {"price": 0.65, "quantity": self.depth}]}
        if path.startswith("/tournaments/") and path.endswith("/positions"):
            return {"positions": []}
        if path.startswith("/tournaments/"):
            return {"myBalance": 50000}
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(path)
        if path == "/orders/batch":
            return {"results": [{"index": i, "ok": True, "data": {"orderId": f"o{i}", "quantityTraded": 1000}}
                                for i, _ in enumerate(body["orders"])]}
        return {}


def venue_args(**kw):
    from types import SimpleNamespace
    d = dict(feed=False, live=True, paper=False, account_every=10.0, rest_tops_every=6.0, fallback_depth=1000.0,
             fallback_book_reads=30.0, writers=1)
    return SimpleNamespace(**{**d, **kw})


def test_rest_fallback_reads_full_books_for_called_races():
    from election import SigVenue
    v = SigVenue(venue_args(fallback_book_reads=2), "t", [], lambda e: None, client=FakeClient(), threads=False)
    v.ids = ["1", "2", "3"]
    v.focus = ["1", "2", "3"]
    v._read_rest()
    b = v.books()
    assert b["1"]["asks"] == [(0.60, 50000), (0.65, 50000)] and b["1"]["bids"][0] == (0.45, 10)   # best first
    assert b["3"]["asks"] == [(0.60, 1000.0)]                  # over the read budget: top + assumed depth
    v._read_rest()                                             # budget used up within the minute
    assert sum(c.endswith("/orderbook") for c in v.c.calls) == 2


def test_fallback_depth_lets_the_sweep_size_up():
    from election import SigVenue
    v = SigVenue(venue_args(), "t", [], lambda e: None, client=FakeClient(), threads=False)
    v.ids, v.focus = ["1", "2"], ["1", "2"]
    v._read_rest()
    q = {"1": {"kalshi": 0.995, "polymarket": 0.995, "fair": {"p": 0.995, "buffer": 0}},
         "2": {"kalshi": 0.005, "polymarket": 0.005, "fair": {"p": 0.005, "buffer": 0}}}
    states = {"Georgia Senate": race()}
    markets = {"1": {"party": "D", "race": "Georgia Senate"}, "2": {"party": "R", "race": "Georgia Senate"}}
    e = Engine(Venue(v.books(), cash=1e6), Outside(q), markets, states, Params(), log=lambda x: None)
    e.det.manual("Georgia Senate", "D", NIGHT0 + timedelta(minutes=5))
    e.step(NIGHT0 + timedelta(minutes=5))
    assert max(o["qty"] for o in e.v.taken) > 1000             # the stale depth, not the 1,000 assumed


def test_engine_sets_focus_to_called_races_latest_first():
    e, v = engine(SWEEP_BOOKS, SWEEP_Q)
    v.focus = []
    e.det.manual("Georgia Senate", "D", NIGHT0 + timedelta(minutes=5))
    e.step(NIGHT0 + timedelta(minutes=5))
    assert v.focus == ["1", "2"]


def test_account_snapshot_nets_fills_after_the_read():
    import time as _t
    from election import SigVenue
    v = SigVenue(venue_args(), "t", [], lambda e: None, client=FakeClient(), threads=False)
    try:
        v.account()
        assert False, "no snapshot yet must raise (the engine keeps its last cash)"
    except RuntimeError:
        pass
    v._read_account()
    assert v.account()[0] == 50000
    v.trading = True
    v.take([{"ex": "1", "side": "yes", "price": 0.6, "qty": 1000, "role": "C"}], 60)
    assert abs(v.account()[0] - (50000 - 600)) < 1e-6          # filled after the snapshot: netted
    _t.sleep(0.01)
    v._read_account()                                          # a newer read holds it already
    assert v.account()[0] == 50000


def test_lease_keeper_clears_the_board_before_trading():
    from election import LeaseKeeper

    class L:
        def __init__(self):
            self.state, self.last, self.released = "held", {}, False

        def try_acquire(self):
            return self.state

        def release(self):
            self.released = True

    class V:
        trading = False
        seen = []

        def cancel_all(self, force=False):
            self.seen.append((force, self.trading))
            return True

    class E:
        live = False

    lease, v, e = L(), V(), E()
    k = LeaseKeeper(lease, v, e, lambda m: None, lambda d: None)
    k.check()
    assert v.seen == [(True, False)] and v.trading and e.live      # cancelled while not yet trading
    lease.state = "error"
    k.check()
    assert v.trading                                               # git down: the holder keeps trading
    lease.state = "other"
    k.check()
    assert not v.trading and not e.live
    lease.state = "held"
    k.check()
    k.start(0.05)
    import time as _t
    _t.sleep(0.2)
    k.stop()
    assert lease.released and not v.trading


def test_remote_stop_blocks_every_bot_and_survives_git_errors():
    from election import LeaseKeeper

    shared = {"owner": None}      # one lease, two bots: the holder (a) and the standby (b)

    class L:
        def __init__(self, me):
            self.me, self.last, self.tries, self.released = me, {}, 0, False

        def try_acquire(self):
            self.tries += 1
            if shared["owner"] in (None, self.me):
                shared["owner"] = self.me
                return "held"
            return "other"

        def release(self):
            if shared["owner"] == self.me:
                shared["owner"], self.released = None, True

    class V:
        def __init__(self):
            self.trading, self.seen = False, []

        def cancel_all(self, force=False):
            self.seen.append((force, self.trading))
            return True

    class E:
        live = False

    flag = {"v": False}
    la, lb, va, vb = L("a"), L("b"), V(), V()
    ka = LeaseKeeper(la, va, E(), lambda m: None, lambda d: None, remote_stop=lambda: flag["v"])
    kb = LeaseKeeper(lb, vb, E(), lambda m: None, lambda d: None, remote_stop=lambda: flag["v"])
    ka.check()
    kb.check()
    assert va.trading and not vb.trading and shared["owner"] == "a"
    va.seen.clear()
    vb.seen.clear()

    flag["v"] = True                                          # flipped on from a phone
    ka.check()
    kb.check()
    assert not va.trading and not ka.eng.live and la.released and shared["owner"] is None
    assert va.seen == [(True, False)] and vb.seen == [(True, False)]     # every bot clears the board
    tries = lb.tries
    for _ in range(3):
        kb.check()
        ka.check()
    assert not vb.trading and not va.trading and lb.tries == tries      # the standby never even tries the lease
    assert len(va.seen) == 2 and len(vb.seen) == 2            # a second sweep for in-flight takes, then quiet

    flag["v"] = None                                          # git unreachable: still stopped
    ka.check()
    kb.check()
    assert ka.stopped and kb.stopped and not va.trading and not vb.trading

    flag["v"] = False                                         # flipped off: one bot takes over, board cleared first
    kb.check()
    ka.check()
    assert vb.trading and not va.trading and shared["owner"] == "b" and vb.seen[-1] == (True, False)


def test_git_outage_primary_trades_without_the_lease_secondary_never():
    from election import LeaseKeeper

    clock = {"t": 0.0}

    class L:
        def __init__(self, state):
            self.state, self.last, self.released = state, {}, False

        def try_acquire(self):
            return self.state

        def release(self):
            self.released = True

    class V:
        def __init__(self):
            self.trading, self.seen = False, []

        def cancel_all(self, force=False):
            self.seen.append((force, self.trading))
            return True

    class E:
        live = False

    def keeper(lease, v, primary, stop=None):
        return LeaseKeeper(lease, v, E(), lambda m: None, lambda d: None, remote_stop=stop, primary=primary,
                           fail_open_s=90, step_down_s=60, clock=lambda: clock["t"])

    # GitHub down from the start: the primary waits ~ttl + 30 s, clears the board, then trades; a secondary never
    lp, vp, ls, vs = L("error"), V(), L("error"), V()
    kp, ks = keeper(lp, vp, True), keeper(ls, vs, False)
    for t in range(0, 90, 15):
        clock["t"] = t
        kp.check()
        ks.check()
        assert not vp.trading and not vs.trading
    clock["t"] = 90
    kp.check()
    ks.check()
    assert vp.trading and kp.eng.live and vp.seen == [(True, False)] and not vs.trading and vs.seen == []
    clock["t"] = 600
    kp.check()
    ks.check()
    assert vp.trading and not vs.trading and len(vp.seen) == 1          # no second clear while blind
    lp.state = "held"                                                   # GitHub back, lease free: keeps going
    kp.check()
    assert vp.trading and len(vp.seen) == 1
    lp.state = "other"                                                  # someone holds a live lease: step down
    kp.check()
    assert not vp.trading

    # the secondary holds the lease when GitHub goes down: it stops at the ttl, before the primary fails open
    clock["t"] = 1000.0
    lp, vp, ls, vs = L("other"), V(), L("held"), V()
    kp, ks = keeper(lp, vp, True), keeper(ls, vs, False)
    kp.check()
    ks.check()
    assert vs.trading and not vp.trading
    lp.state = ls.state = "error"
    both = []
    for dt in range(0, 200, 5):
        clock["t"] = 1000.0 + dt
        ks.check()
        kp.check()
        both.append(vs.trading and vp.trading)
        assert vs.trading == (dt < 60) and vp.trading == (dt >= 90)
    assert not any(both)                                                # never two bots trading blind

    # a short blip changes nothing: the primary holder keeps trading, a primary standby keeps waiting
    clock["t"] = 2000.0
    lp, vp = L("held"), V()
    kp = keeper(lp, vp, True)
    kp.check()
    lp.state = "error"
    clock["t"] = 2030.0
    kp.check()
    assert vp.trading

    # a remote stop seen before the outage still wins over failing open
    clock["t"] = 3000.0
    flag = {"v": True}
    lp, vp = L("held"), V()
    kp = keeper(lp, vp, True, stop=lambda: flag["v"])
    kp.check()
    flag["v"], lp.state = None, "error"
    clock["t"] = 3500.0
    kp.check()
    assert kp.stopped and not vp.trading


def test_remote_stop_reads_the_file_on_origin_main():
    import subprocess
    import tempfile
    from sigbot.lease import RemoteStop

    def git(cwd, *a):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)

    def commit(work, msg):
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", msg)
        git(work, "push", "-q", "origin", "HEAD:main")

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        origin, work, bot = d / "origin.git", d / "work", d / "bot"
        git(d, "init", "-q", "--bare", str(origin))
        git(d, "clone", "-q", str(origin), str(work))
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git(work, "config", k, v)
        (work / "README").write_text("x")
        commit(work, "init")
        git(d, "clone", "-q", "-b", "main", str(origin), str(bot))
        rs = RemoteStop(bot)
        assert rs.check() is False
        (work / "config").mkdir()
        (work / "config" / "STOP_ELECTION").write_text("stop")
        commit(work, "stop")
        assert rs.check() is True
        assert not (bot / "config" / "STOP_ELECTION").exists()          # the bot's checkout is never touched
        (work / "config" / "STOP_ELECTION").unlink()
        commit(work, "go")
        assert rs.check() is False
        assert RemoteStop(d / "nowhere").check() is None                 # not a repo / git unreachable

        from stop_election import flip                                  # the user's on/off command
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git(bot, "config", k, v)
        (work / "later").write_text("y")
        commit(work, "someone else pushed main")                        # flip builds on the newest main
        assert flip(True, root=bot) and rs.check() is True
        assert flip(True, root=bot)                                     # already on: no new commit needed
        assert not (bot / "config").exists() and not (bot / "later").exists()   # checkout untouched
        git(work, "pull", "-q", "origin", "main")
        assert (work / "config" / "STOP_ELECTION").exists() and (work / "later").exists()
        assert flip(False, root=bot) and rs.check() is False
        git(work, "pull", "-q", "origin", "main")
        assert not (work / "config" / "STOP_ELECTION").exists() and (work / "README").exists()


def test_poll_close_times_and_risk_off_at_first_close():
    from sigbot.calls import first_close, poll_close
    from sigbot.swing import POLL_CLOSE_H, POLL_FIRST_H
    assert POLL_CLOSE_H["MI"] == 2.0 and POLL_CLOSE_H["IA"] == 2.0 and POLL_CLOSE_H["NE"] == 2.0
    assert all(POLL_FIRST_H[s] < POLL_CLOSE_H[s] for s in POLL_FIRST_H)
    assert first_close("MI") == NIGHT0 + timedelta(hours=1) and poll_close("MI") == NIGHT0 + timedelta(hours=2)
    assert first_close("GA") == poll_close("GA") == NIGHT0
    det = CallDetector({"Michigan Senate": race("Michigan Senate", "MI")})
    det.update({"1": {"kalshi": 0.99, "polymarket": 0.99}}, NIGHT0 + timedelta(minutes=90))
    assert det.races["Michigan Senate"].status == "PRE"                 # UP precincts still open: no call yet
    states = {"Michigan Senate": race("Michigan Senate", "MI")}
    markets = {"1": {"party": "D", "race": "Michigan Senate"}, "2": {"party": "R", "race": "Michigan Senate"}}
    v = Venue({}, orders=[{"id": "a", "exchangeId": "1"}])
    e = Engine(v, Outside({}), markets, states, Params(), log=lambda x: None)
    e.step(NIGHT0 + timedelta(minutes=50))
    assert v.cancelled == []
    e.step(NIGHT0 + timedelta(minutes=56))                              # Eastern MI closes at 01:00Z
    assert v.cancelled == ["a"]


# ---- adaptive bold (31 Oct)
def many_races(n):
    states = {f"R{i}": RaceState(f"R{i}", "GA", {"D": str(2 * i), "R": str(2 * i + 1)}) for i in range(n)}
    markets = {str(2 * i + k): {"party": "DR"[k], "race": f"R{i}"} for i in range(n) for k in (0, 1)}
    return states, markets


def test_adaptive_bold_waits_for_calls_then_plans_once():
    class Plan(Engine):
        def refresh_model(self, ext, now, force=False):
            pass

        def _bold(self, books, now, why):
            self.plans.append(why)

    states, markets = many_races(12)
    p = Params()
    p.bold_times = (NIGHT0 - timedelta(minutes=30),)
    e = Plan(Venue({}, cash=100000.0), Outside({}), markets, states, p, model=object(), log=lambda x: None)
    e.plans = []
    t = NIGHT0 + timedelta(minutes=5)
    for i in range(7):
        e.det.manual(f"R{i}", "D", t)
    e.step(t)
    assert e.plans == []                                   # 7 calls < bold_min_calls: the 23:30Z plan waits too
    e.det.manual("R7", "D", t)
    e.step(t + timedelta(seconds=2))
    assert len(e.plans) == 1 and e.plans[0].startswith("first plan after 8")
    e.step(t + timedelta(seconds=4))
    assert len(e.plans) == 1                               # scheduled time consumed, next plan needs new calls
    p2 = Params()
    p2.bold_on = False                                     # --no-bold
    e2 = Plan(Venue({}, cash=100000.0), Outside({}), markets, states, p2, model=object(), log=lambda x: None)
    e2.plans = []
    e2.step(t + timedelta(seconds=6))
    assert e2.plans == []


def test_night_gain_follows_the_measured_sweeps():
    states, markets = many_races(40)
    e = Engine(Venue({}, cash=100000.0), Outside({}), markets, states, Params(), log=lambda x: None)
    e.cash = 101000.0                                      # 100k kept after the reserve
    now = NIGHT0 + timedelta(hours=1)
    assert e.night_gain_now(now) == Params().night_gain    # nothing measured: the prior
    for i in range(20):                                    # 20 calls in the last hour, 20 left
        states[f"R{i}"].status = "CALLED"
        states[f"R{i}"].called_at = (now - timedelta(minutes=30)).isoformat()
    e.note_calls()
    e.ledger["edge"] = 200000.0                            # 10k per call: 20 more calls = 200k on 100k = 2.0
    m, s = e.night_gain_now(now)
    w = 20 / 30
    assert abs(m - (w * 2.0 + (1 - w) * 0.15)) < 1e-6 and abs(s - (w * 1.0 + (1 - w) * 0.25)) < 1e-6
    e.ledger["edge"] = 2000.0                              # thin books: 100 per call -> 0.02
    assert e.night_gain_now(now)[0] < 0.15
    e.ledger["edge"] = 200000.0
    late = NIGHT0 + timedelta(hours=10)                    # no call in the last 2 h: nothing more expected
    assert e.night_gain_now(late)[0] == (1 - w) * 0.15
    e.p.bold_adaptive = 0.0
    assert e.night_gain_now(now) == Params().night_gain


def test_ledger_counts_speed_fills_and_merges_across_runs():
    states, markets = many_races(3)
    e = Engine(Venue({}, cash=100000.0), Outside({}), markets, states, Params(), log=lambda x: None)
    opps = [{"ex": "0", "side": "yes", "qty": 1000, "edge": 100.0, "role": "C"},
            {"ex": "1", "side": "no", "qty": 1000, "edge": 50.0, "role": "B"}]
    e.note_fills(opps, [{"ex": "0", "side": "yes", "filled": 400, "cost": 380.0},
                        {"ex": "1", "side": "no", "filled": 1000, "cost": 900.0}])
    assert e.ledger["edge"] == 40.0 and e.ledger["cost"] == 380.0 and e.ledger["fills"] == 1   # bold fills excluded
    e.ledger["called"] = {"R0": "2026-11-04T00:10:00+00:00"}
    e.adopt_ledger({"edge": 900.0, "cost": 5000.0, "fills": 7,
                    "called": {"R0": "2026-11-04T00:05:00+00:00", "R1": "2026-11-04T00:20:00+00:00"}})
    assert e.ledger["edge"] == 900.0 and e.ledger["fills"] == 7
    assert e.ledger["called"] == {"R0": "2026-11-04T00:05:00+00:00", "R1": "2026-11-04T00:20:00+00:00"}
    e.adopt_ledger({"edge": 10.0, "called": {}})            # an older, smaller ledger never rolls ours back
    e.adopt_ledger(None)
    assert e.ledger["edge"] == 900.0
    st = e.ledger_state()
    st["called"]["R9"] = "x"
    assert "R9" not in e.ledger["called"]                  # a copy: the lease thread can't race the step


def test_lease_keeper_carries_the_ledger():
    from election import LeaseKeeper

    class L:
        def __init__(self, last):
            self.state, self.last, self.extra, self.writes = "held", last, {}, []

        def try_acquire(self):
            self.writes.append(dict(self.extra))
            return self.state

        def release(self):
            self.writes.append(dict(self.extra))

    class V:
        trading = False

        def cancel_all(self, force=False):
            return True

    states, markets = many_races(2)
    e = Engine(V(), Outside({}), markets, states, Params(), log=lambda x: None)
    lease = L({"owner": "old", "ledger": {"edge": 5000.0, "cost": 1.0, "fills": 3, "called": {"R0": "t0"}}})
    k = LeaseKeeper(lease, e.v, e, lambda m: None, lambda d: None)
    k.check()                                              # take-over: adopts the dead run's ledger
    assert e.ledger["edge"] == 5000.0 and e.ledger["called"] == {"R0": "t0"} and lease.writes[0] == {}
    e.ledger["edge"] += 100
    k.check()                                              # holder: writes its ledger with the renewal
    assert lease.writes[1]["ledger"]["edge"] == 5100.0
    e.ledger["edge"] += 1
    k.stop()
    assert lease.writes[-1]["ledger"]["edge"] == 5101.0

    class Broken:                                          # a broken ledger never costs the lease
        live = False

    k2 = LeaseKeeper(L({"ledger": {"edge": 1}}), V(), Broken(), lambda m: None, lambda d: None)
    k2.check()
    k2.check()
    assert k2.holder


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if fails else 0)
