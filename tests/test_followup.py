"""Offline (no network) check of maker.py's dump follow-ups on a Hawaii-like dump."""
import argparse, sys, time
from collections import deque
from datetime import timedelta
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

m = Maker(args); m.c = None; ev = []; m.event = ev.append
m.load_markets(utcnow() + timedelta(hours=1))
class Books:
    up = True
    def healthy(self): return self.up
    def side_bids(self, ex, side):   # YES bids on 991 = NO offers at 1 - p
        return [(0.11, 300000.0), (0.105, 50000.0)] if (ex, side) == ("991", "yes") else []
m.books = Books()
iso = lambda h: (utcnow() - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.0000000+00:00")
F = lambda p: {"p": p, "buffer": 0.005, "sources": {"kalshi": p}, "override": False, "at": utcnow()}
m.fair = {"991": F(0.017), "864": F(0.02), "946": F(0.011), "997": F(0.045), "947": F(0.958)}
now = time.time()
for ex, mid in (("991", 0.08), ("864", 0.045), ("946", 0.09), ("997", 0.05), ("947", 0.87)):
    m._mid_hist[ex] = deque((now - 60 * i, mid) for i in range(30, 0, -1))
tops = {"991": (0.11, 0.115),            # dumped: NO offered at 0.89 vs SIG level 0.92, fair 0.983
        "864": (0.04, 0.045),            # MD Gov R: NO bid 0.955, fair 0.98 -> ~2% left: sell it
        "946": (0.085, 0.09),            # CO Sen R: NO bid 0.91, fair 0.989 -> ~8% left: keep
        "997": (0.045, 0.05),            # MN Gov R: NO bid 0.95 (~1.9% left) but bought an hour ago: keep
        "947": (0.865, 0.87)}            # GA Sen D YES, a different race
pos = {"864": {"side": "no", "qty": 20000, "avg": 0.90, "opened": iso(48)},
       "946": {"side": "no", "qty": 17248, "avg": 0.902, "opened": iso(300)},
       "997": {"side": "no", "qty": 6395, "avg": 0.91, "opened": iso(1)}}

want = m.desired(tops, pos, 5000.0, ladder_on=True)
fu = [w for w in want if w.get("fu")]
on991 = [w for w in want if w["ex"] == "991"]
sells = {w["ex"]: w for w in want if w["role"] == "unwind" and w["price"] < 0.1}
print("follow-up:", fu)
print("sales:", {ex: (w["qty"], w["price"]) for ex, w in sells.items()})
assert len(fu) == 1 and fu[0]["side"] == "no" and fu[0]["price"] == 0.89 and on991 == fu
assert abs(fu[0]["qty"] * 0.89 - 40000) < 1
assert ev and ev[-1]["kind"] == "followup"
assert set(ev[-1]["sell"]) == {"864"}, "only the old, nearly-converged hold is sold"
assert sells["864"]["price"] == 0.045 and sells["864"]["qty"] == ev[-1]["sell"]["864"]["qty"] <= 15000 / 0.955 + 1

# budget: 5k cash -> the take is shrunk to cash above the 3k hard floor; after the sale lands it gets it all
b = m.budget(want, [], 5000.0)
f1 = [w for w in b if w.get("fu")][0]
assert abs(f1["qty"] * 0.89 - 2000) < 1, f1
b = m.budget(want, [], 45000.0)
assert [w for w in b if w.get("fu")][0]["qty"] == fu[0]["qty"]
# value buys share that cash after the follow-up
vb = {"ex": "947", "side": "yes", "price": 0.80, "qty": 50000, "role": "V", "prio": -0.1, "roc": 0.19,
      "lo": 0.79, "hi": 0.81}
b = m.budget(want + [vb], [], 45000.0)
v = [w for w in b if w["ex"] == "947" and w["role"] == "V"]
assert not v or v[0]["qty"] * 0.80 <= 45000 - 3000 - 40000 + 1, v

# scarce cash keeps the follow-up (roc 0.099 < --scarce-roc 0.12)
assert any(w.get("fu") for w in want if w["role"] == "unwind" or w["role"][0] in "SPDKQ" or w.get("fu") or
           w.get("roc", 0) >= args.scarce_roc)

# no dump, no sales; feed down, no follow-up; no-op memory blocks it; --no-followup
ev.clear()
assert not [w for w in m.desired({**tops, "991": (0.08, 0.085)}, pos, 5000.0, True) if w.get("fu")]
Books.up = False
assert not [w for w in m.desired(tops, pos, 5000.0, True) if w.get("fu")]
Books.up = True
m._v_noop[("991", "no")] = utcnow()
assert not [w for w in m.desired(tops, pos, 5000.0, True) if w.get("fu")]
m._v_noop.clear()
args.no_followup = True
assert not [w for w in m.desired(tops, pos, 5000.0, True) if w.get("fu")]
args.no_followup = False
# a dump returning less than hold + 0.04 sells nothing (but still takes from spare cash)
ev.clear()
want = m.desired({**tops, "864": (0.075, 0.08)}, {**pos, "864": {**pos["864"], "avg": 0.80}}, 5000.0, True)
assert [w for w in want if w.get("fu")] and ev[-1]["sell"] == {}, ev[-1]   # 864 left ~6.7%: < 9.9% - 4%? no sale
print("ALL OK")
