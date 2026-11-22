"""Offline checks for the news-desk call signal (sigbot/desk.py + CallDetector desk rules). Run: python tests/test_desk.py
Fixtures are trimmed copies of the real 2024 files (NBC firecracker national/state results, NYT results-*.json)."""
from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigbot.calls import NIGHT0, CallDetector, RaceState                    # noqa: E402
from sigbot.desk import DeskFeed, parse_nbc_national, parse_nbc_state, parse_nyt   # noqa: E402
from sigbot.night import Engine, Params                                     # noqa: E402


def cand(last, party, won):
    return {"lastName": last, "party": party, "callStatus": "P" if won else "", "isWinner": won}


def nbc_tip(*cands):
    return {"tooltip": {"callStatusCode": "P" if any(c["isWinner"] for c in cands) else "", "candidates": list(cands)}}


NBC_SENATE = {"mapData": {
    "AZ": nbc_tip(cand("Gallego", "Dem", True), cand("Lake", "Rep", False)),
    "NE": nbc_tip(cand("Fischer", "Rep", True), cand("Osborn", "Ind", False)),
    "NE~P": nbc_tip(cand("Ricketts", "Rep", False), cand("Love", "Dem", True)),   # special: regular NE wins
    "FL~P": nbc_tip(cand("Moody", "Rep", True), cand("X", "Dem", False)),         # 2026: FL's only race is a special
    "PA": nbc_tip(cand("McCormick", "Rep", False), cand("Casey", "Dem", False)),  # listed, not called
    "ME": nbc_tip(cand("King", "Ind", True), cand("Kouzounas", "Rep", False)),
    "US": {"tooltip": {}},
}}
NBC_HOUSE = {"mapData": {"4207": nbc_tip(cand("Mackenzie", "Rep", True), cand("Wild", "Dem", False)),
                         "1001": nbc_tip(cand("McBride", "Dem", True), cand("Whalen", "Rep", False)),
                         "3001": nbc_tip(cand("Zinke", "Rep", False), cand("Tranel", "Dem", False))}}
NBC_STATE_ME = {"stateAbbr": "ME", "stateRaceResults": {
    "discreteOffices": [{"officeCode": "P", "candidates": [cand("Harris", "dem", True)]},
                        {"officeCode": "S", "candidates": [cand("King", "ind", True), cand("Costello", "dem", False)]},
                        {"officeCode": "G", "candidates": [cand("A", "gop", False), cand("B", "dem", False)]}],
    "house": [{"district": 2, "candidates": [cand("Golden", "dem", True), cand("Theriault", "gop", False)]},
              {"district": 2, "candidates": [cand("Golden", "dem", False), cand("Theriault", "gop", False)]},  # RCV
              {"district": 1, "candidates": [cand("Pingree", "dem", False), cand("Russell", "gop", False)]}]}}


def nyt_race(st, office, typ, won, parties, seat=None):
    return {"office_id": office, "type": typ, "seat_number": seat, "reporting_units": [{"state_postal": st}],
            "outcome": {"won": won, "called_by": ["ap"]},
            "candidate_metadata": {k: {"party": {"nyt_id": v}} for k, v in parties.items()}}


NYT = {"races": [
    nyt_race("AZ", "S", "General", ["gallego-r"], {"gallego-r": "DEM", "lake-k": "GOP"}),
    nyt_race("NE", "S", "Special General", ["love-p"], {"ricketts-p": "GOP", "love-p": "DEM"}),
    nyt_race("NE", "S", "General", ["fischer-d"], {"fischer-d": "GOP", "osborn-d": "IND"}),
    nyt_race("PA", "H", "General", ["mackenzie-r"], {"mackenzie-r": "GOP", "wild-s": "DEM"}, seat="7"),
    nyt_race("LA", "H", "Non-Partisan Election", ["scalise-s"], {"scalise-s": "GOP"}, seat="1"),
    nyt_race("GA", "S", "Runoff", ["x"], {"x": "DEM"}),
    nyt_race("MI", "S", "General", [], {"slotkin-e": "DEM", "rogers-m": "GOP"}),
]}


def test_parse_nbc_national():
    s = parse_nbc_national(NBC_SENATE, "senate")
    assert s["Arizona Senate"] == "D" and s["Nebraska Senate"] == "R" and s["Florida Senate"] == "R"
    assert s["Pennsylvania Senate"] is None and s["Maine Senate"] == "I" and len(s) == 5
    h = parse_nbc_national(NBC_HOUSE, "house")
    assert h == {"PA-07 House": "R", "DE-01 House": "D", "MT-01 House": None}
    g = parse_nbc_national({"mapData": {"OH": nbc_tip(cand("R", "Rep", True))}}, "governor")
    assert g == {"Ohio Governor": "R"}


def test_parse_nbc_state_duplicates():
    s = parse_nbc_state(NBC_STATE_ME)
    assert s == {"Maine Senate": "I", "Maine Governor": None, "ME-02 House": "D", "ME-01 House": None}


def test_parse_nyt():
    y = parse_nyt(NYT)
    assert y["Arizona Senate"] == "D" and y["Nebraska Senate"] == "R" and y["PA-07 House"] == "R"
    assert y["LA-01 House"] == "R" and y["Michigan Senate"] is None and "Georgia Senate" not in y


def test_parse_ignores_garbage():
    assert parse_nbc_national({}, "senate") == {} and parse_nbc_state({"stateAbbr": "XX"}) == {}
    assert parse_nyt({"races": [{"office_id": "S"}]}) == {}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        for frag, r in self.routes.items():
            if frag in url:
                if isinstance(r, Exception):
                    raise r
                return r
        return Resp(404)


RACES = {"Arizona Senate", "Nebraska Senate", "Pennsylvania Senate", "PA-07 House", "Maine Senate", "Florida Senate"}


def test_feed_merges_sources_and_survives_failures():
    logs = []
    s = Session({"national-results/2026-elections/senate": Resp(200, NBC_SENATE),
                 "national-results/2026-elections/house": TimeoutError("slow"),
                 "national-results/2026-elections/governor": Resp(500),
                 "results-senate.json": Resp(200, ValueError("bad json")),
                 "results-house.json": Resp(200, NYT)})
    f = DeskFeed(RACES, session=s, log=logs.append)
    f.poll_once()
    c = f.calls()
    assert c["Arizona Senate"] == {"nbc": "D", "nyt": "D"} and c["PA-07 House"] == {"nyt": "R"}   # (NYT fixture: mixed)
    assert c["Pennsylvania Senate"] == {"nbc": None}
    assert f.stats["nbc_house_errors"] == 1 and f.stats["nbc_governor_errors"] == 1 and f.stats["nyt_senate_errors"] == 1
    assert any(e["kind"] == "desk" and e["race"] == "Arizona Senate" for e in logs)
    assert not any(e["kind"] == "desk" and e["race"] == "Pennsylvania Senate" for e in logs)   # no call, no log
    f.views = {k: (v, t - 1000) for k, (v, t) in f.views.items()}      # older than stale_s: ignored
    assert f.calls() == {}


def test_feed_falls_back_to_state_files_when_national_404s():
    s = Session({"state-results/2026-elections/maine-results": Resp(200, NBC_STATE_ME)})
    f = DeskFeed(RACES | {"ME-02 House"}, sources=("nbc",), session=s, due_states=lambda: ["ME", "ZZ"], log=lambda e: None)
    f.poll_once()
    assert f.national_missing == {"senate", "governor", "house"}
    assert f.calls() == {"Maine Senate": {"nbc": "I"}, "ME-02 House": {"nbc": "D"}}
    n = len(s.urls)
    f.poll_once()                                   # missing national files are re-checked only every 30 cycles
    assert not any("national" in u for u in s.urls[n:])


def test_feed_thread_never_dies():
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("network down")
    f = DeskFeed(RACES, session=Boom(), every=1.0, log=lambda e: None)
    f.start()
    time.sleep(0.3)
    assert f.thread.is_alive() and f.calls() == {}
    f.stop()


# ---------------------------------------------------------------------- detector
def race(name="Georgia Senate", st="GA", d="1", r="2"):
    return RaceState(name, st, {"D": d, "R": r})


def det1():
    return CallDetector({"Georgia Senate": race()})


T = NIGHT0 + timedelta(minutes=10)


def test_desk_calls_at_once_with_one_venue_at_095():
    det = det1()
    ev = det.update({"1": {"kalshi": 0.955, "polymarket": 0.90}}, T, desk={"Georgia Senate": {"nbc": "D"}})
    assert ev and ev[0]["source"] == "desk" and ev[0]["winner"] == "D" and ev[0]["conf"] == 0.985
    det = det1()   # same prices without the desk: nothing (needs both >= 0.97 for 60 s)
    assert det.update({"1": {"kalshi": 0.955, "polymarket": 0.90}}, T) == []


def test_desk_needs_a_venue_and_the_first_close():
    det = det1()
    assert det.update({"1": {"kalshi": 0.94, "polymarket": 0.93}}, T, desk={"Georgia Senate": {"nbc": "D"}}) == []
    assert det.update({"1": {"kalshi": 0.99, "polymarket": 0.99}}, NIGHT0 - timedelta(minutes=1),
                      desk={"Georgia Senate": {"nbc": "D", "nyt": "D"}}) == []
    det = CallDetector({"Michigan Senate": race("Michigan Senate", "MI")})   # first close 01:00Z, last 02:00Z
    ev = det.update({"1": {"kalshi": 0.96}}, NIGHT0 + timedelta(minutes=70), desk={"Michigan Senate": {"nyt": "D"}})
    assert ev and ev[0]["source"] == "desk"


def test_two_desks_or_venue_hi_give_full_conf():
    det = det1()
    ev = det.update({"1": {"kalshi": 0.955}}, T, desk={"Georgia Senate": {"nbc": "D", "nyt": "D"}})
    assert ev[0]["conf"] == 0.99
    det = det1()
    ev = det.update({"1": {"kalshi": 0.975, "polymarket": 0.90}}, T, desk={"Georgia Senate": {"nyt": "D"}})
    assert ev[0]["conf"] == 0.99


def test_desks_disagreeing_is_no_desk_call_but_venues_still_call():
    det = det1()
    desk = {"Georgia Senate": {"nbc": "D", "nyt": "R"}}
    q = {"1": {"kalshi": 0.98, "polymarket": 0.98}}
    assert det.update(q, T, desk=desk) == []
    ev = det.update(q, T + timedelta(seconds=61), desk=desk)
    assert ev and ev[0]["source"] == "both"


def test_desk_blocks_a_weak_venue_call_for_another_party_and_retracts_one():
    det = det1()
    q = {"2": {"kalshi": 0.992, "polymarket": 0.955}}           # "hard" R call; the desk says D
    desk = {"Georgia Senate": {"nbc": "D"}}
    det.update(q, T, desk=desk)
    assert det.update(q, T + timedelta(seconds=61), desk=desk) == []
    det = det1()
    det.update(q, T)
    assert det.update(q, T + timedelta(seconds=61))[0]["winner"] == "R"
    ev = det.update(q, T + timedelta(seconds=70), desk=desk)
    assert ev[0]["kind"] == "retract" and det.races["Georgia Senate"].status == "CLOSED"


def test_both_venues_at_hi_beat_a_contradicting_desk():
    """A desk naming the side both venues price <= 0.03 is far likelier a feed / mapping fault than a real call."""
    det = det1()
    q = {"2": {"kalshi": 0.98, "polymarket": 0.98}}
    desk = {"Georgia Senate": {"nbc": "D"}}
    det.update(q, T, desk=desk)
    ev = det.update(q, T + timedelta(seconds=61), desk=desk)
    assert ev and ev[0]["winner"] == "R" and ev[0]["source"] == "both"
    assert det.update(q, T + timedelta(seconds=70), desk=desk) == []     # no retraction either


def test_two_desks_need_only_090():
    det = det1()
    assert det.update({"1": {"kalshi": 0.91}}, T, desk={"Georgia Senate": {"nbc": "D"}}) == []
    ev = det.update({"1": {"kalshi": 0.91}}, T, desk={"Georgia Senate": {"nbc": "D", "nyt": "D"}})
    assert ev and ev[0]["conf"] == 0.99
    det = det1()
    assert det.update({"1": {"kalshi": 0.89}}, T, desk={"Georgia Senate": {"nbc": "D", "nyt": "D"}}) == []


def test_withdrawn_desk_call_is_retracted_unless_venues_hold_it():
    det = det1()
    det.update({"1": {"kalshi": 0.96, "polymarket": 0.93}}, T, desk={"Georgia Senate": {"nbc": "D"}})
    assert det.races["Georgia Senate"].status == "CALLED"
    assert det.update({"1": {"kalshi": 0.96, "polymarket": 0.93}}, T + timedelta(seconds=10)) == []   # feed silent
    ev = det.update({"1": {"kalshi": 0.96, "polymarket": 0.93}}, T + timedelta(seconds=20),
                    desk={"Georgia Senate": {"nbc": None}})
    assert ev and ev[0]["kind"] == "retract"
    det = det1()
    det.update({"1": {"kalshi": 0.99, "polymarket": 0.98}}, T, desk={"Georgia Senate": {"nbc": "D"}})
    assert det.update({"1": {"kalshi": 0.99, "polymarket": 0.98}}, T + timedelta(seconds=20),
                      desk={"Georgia Senate": {"nbc": None}}) == []


def test_desk_retract_before_last_close_goes_back_to_pre():
    det = CallDetector({"Michigan Senate": race("Michigan Senate", "MI")})
    t = NIGHT0 + timedelta(minutes=70)
    det.update({"1": {"kalshi": 0.96}}, t, desk={"Michigan Senate": {"nyt": "D"}})
    det.update({"1": {"kalshi": 0.96}}, t + timedelta(seconds=5), desk={"Michigan Senate": {"nyt": None}})
    assert det.races["Michigan Senate"].status == "PRE"


class Venue:
    def __init__(self, books):
        self._b, self.taken = books, []

    def books(self):
        return self._b

    def account(self):
        return 100000.0, {}

    def open_orders(self):
        return []

    def take(self, orders, expiry_s):
        self.taken += orders
        return []

    def rest(self, orders, expiry_s):
        return []

    def cancel(self, ids):
        return len(ids)

    def cancel_all(self):
        return True

    def third_balance(self):
        return None


class Quotes:
    def __init__(self, q):
        self.q = q

    def quotes(self):
        return self.q


class Desk:
    def __init__(self, c=None, boom=False):
        self.c, self.boom = c or {}, boom

    def calls(self):
        if self.boom:
            raise RuntimeError("desk broke")
        return self.c


def engine(desk):
    states = {"Georgia Senate": race()}
    markets = {"1": {"party": "D", "race": "Georgia Senate"}, "2": {"party": "R", "race": "Georgia Senate"}}
    books = {"1": {"bids": [(0.80, 1000)], "asks": [(0.85, 2000)]}, "2": {"bids": [(0.15, 3000)], "asks": [(0.20, 500)]}}
    q = {"1": {"kalshi": 0.96, "polymarket": 0.94, "fair": {"p": 0.95, "buffer": 0.01}},
         "2": {"kalshi": 0.04, "polymarket": 0.06, "fair": {"p": 0.05, "buffer": 0.01}}}
    v = Venue(books)
    return Engine(v, Quotes(q), markets, states, Params(), log=lambda e: None, desk=desk), v


def test_engine_sweeps_on_a_desk_call():
    e, v = engine(Desk({"Georgia Senate": {"nbc": "D"}}))
    r = e.step(T)
    assert r["events"] and r["events"][0]["source"] == "desk"
    assert {(o["ex"], o["side"], o["role"]) for o in v.taken} >= {("1", "yes", "C"), ("2", "no", "C")}


def test_engine_survives_a_broken_desk():
    e, v = engine(Desk(boom=True))
    r = e.step(T)
    assert r["events"] == [] and e.stats["desk_errors"] == 1


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
