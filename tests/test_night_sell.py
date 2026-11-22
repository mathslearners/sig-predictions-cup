"""Offline (no network) check of maker.py --night-sell-all: on 3 Nov from 19:00Z every hold goes to cash at SIG's
bid (early safe seats too), but never far under cost or fair (an empty or stale book)."""
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maker
from maker import Maker, utcnow

class Got(Exception): pass
orig = argparse.ArgumentParser.parse_args
def grab(self, *a, **k): raise Got(orig(self, []))
argparse.ArgumentParser.parse_args = grab
try: maker.main()
except Got as g: args = g.args[0]
argparse.ArgumentParser.parse_args = orig

m = Maker(args); m.c = None; m.event = lambda e: None
F = lambda p: {"p": p, "buffer": 0.005, "sources": {"kalshi": p}, "override": False, "at": utcnow()}
m.fair = {"968": F(0.005), "1035": F(0.03), "946": F(0.011), "947": F(0.958)}
m.markets = {ex: {"key": ("NE", "Senate", "D")} for ex in m.fair}     # NE: polls close 02:00Z, an early seat
tops = {"968": (0.04, 0.045),     # NE Sen D: NO bid 0.955, fair 0.995, avg 0.945: early safe seat -> sold
        "1035": (0.05, 0.055),    # VA-07 R: NO bid 0.945 under avg 0.99 - 0.03 -> kept
        "946": (0.15, 0.16),      # CO Sen R: NO bid 0.84, fair 0.989 - 0.06 = 0.929 -> stale book, kept
        "947": (0.95, 0.955)}     # GA Sen D YES bid 0.95, fair 0.958, avg 0.87 -> sold
pos = {"968": {"side": "no", "qty": 5290, "avg": 0.945}, "1035": {"side": "no", "qty": 13600, "avg": 0.99},
       "946": {"side": "no", "qty": 7338, "avg": 0.902}, "947": {"side": "yes", "qty": 16949, "avg": 0.87}}

def at(t):
    maker.utcnow = lambda: t
    Maker.pre_night = staticmethod(lambda: maker.PRE_NIGHT <= t < maker.ELECTION_HANDOVER)

at(datetime(2026, 11, 3, 19, 30, tzinfo=timezone.utc))
m.a.night_sell_all = False
off = m.busy_prep(tops, pos, 50000.0)
assert "968" not in off and "947" not in off, off      # default: early safe seats are kept for the night
m.a.night_sell_all = True
on = m.busy_prep(tops, pos, 50000.0)
assert set(on) == {"968", "947"}, on
assert on["968"] == (5290, 0.955) and on["947"] == (16949, 0.95)
at(datetime(2026, 11, 3, 18, 30, tzinfo=timezone.utc))
assert "968" not in m.busy_prep(tops, pos, 50000.0)    # not before 19:00Z
print("ALL OK")
