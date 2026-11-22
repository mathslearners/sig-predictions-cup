"""Who trades what: match the leaders' 1d-leaderboard steps to the realtime tape. READ-ONLY, places nothing.

  python leader_study.py                         # analyse every maker log on run-logs that carries tracker data
  python leader_study.py --since 2026-10-08T16   # ... only from this UTC time
  python leader_study.py --record 10             # no maker running: record 10 min locally (feed + 8 reads/min)
  python leader_study.py --user merry-coral-9766 --show 30   # one account's matched trades

The live maker logs {"kind": "lb"} (accounts whose tradesCount/volume moved since the last 15 s poll) and
{"kind": "tape"} (every print with the YES book top just before it), see sigbot/lbtrack.py. Leaderboard volume
is shares x price paid for the side bought, so an account's volume step must equal a subset of the prints in its
window, each worth q*p (bought YES) or q*(1-p) (bought NO). A step is attributed only when exactly one subset fits
(within --tol); then the side bought and the book top tell whether the account took liquidity (paid the ask /
hit the bid) or rested an order that got hit.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from sigbot.client import ROOT

TOL = 0.02


def short(t: str) -> str:
    """'Will the Republican Party win the 2026 Ohio Governor race?' -> 'R Ohio Governor'."""
    m = re.match(r"Will (?:the )?(\w+)[^?]*? win (?:the )?(?:2026 )?(.*?)(?: race| election)?\??$", t)
    return f"{m.group(1)[0]} {m.group(2)}" if m else t


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ------------------------------------------------------------------ data
def run_logs_events(since: str | None) -> list[dict]:
    if subprocess.run(["git", "fetch", "-q", "origin", "run-logs"], cwd=ROOT).returncode:
        raise SystemExit("git fetch run-logs failed")
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", "FETCH_HEAD", "maker_logs/"], cwd=ROOT,
                           capture_output=True, text=True).stdout.split()
    out = []
    for n in sorted(x for x in names if x.endswith(".jsonl.gz")):
        stamp = n.split("maker_")[-1][:8]   # the run's start date, 20261008
        if since and stamp < since.replace("-", "")[:8]:
            continue
        raw = subprocess.run(["git", "show", f"FETCH_HEAD:{n}"], cwd=ROOT, capture_output=True).stdout
        try:
            txt = gzip.decompress(raw).decode("utf-8")
        except OSError:
            continue
        if '"kind": "lb"' not in txt:
            continue
        out += [json.loads(l) for l in txt.splitlines() if '"kind": "lb"' in l or '"kind": "tape"' in l]
    return out


def local_events() -> list[dict]:
    out = []
    for f in sorted((ROOT / "data" / "lbtrack").glob("rec_*.jsonl")):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
                if '"kind": "lb"' in l or '"kind": "tape"' in l]
    return out


def record(minutes: float) -> None:
    """Stand-alone recorder (same classes as the maker) for when no live maker is running."""
    import threading
    from sigbot.books import BookCache
    from sigbot.client import RateLimiter, SigClient
    from sigbot.lbtrack import LeaderPoller, Tape
    from sigbot.realtime import RealtimeFeed
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    out = ROOT / "data" / "lbtrack"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"rec_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    lock = threading.Lock()

    def emit(d):
        with lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **d}, default=str) + "\n")

    books, tape = BookCache(), Tape()

    def on_batch(topic, event, payload):
        if topic.startswith("leaderboard"):
            emit({"kind": "lb_push", "event": event, "payload": json.dumps(payload)[:4000]})
            return
        if payload.get("trades"):
            tape.add(payload["trades"], {str(t.get("exchangeId")): books.top(str(t.get("exchangeId")))
                                         for t in payload["trades"]})
        books.apply(payload)

    feed = RealtimeFeed(SigClient(log=False), u["tournamentId"], sorted({m["id"] for m in u["markets"]}), on_batch,
                        extra_topics=["leaderboard:all"])
    feed.start()
    lc = SigClient(log=False)
    lc.read_limiter = RateLimiter(8)
    poller = LeaderPoller(lc, emit, active=books.healthy)
    poller.start()
    t_end = time.time() + 60 * minutes
    while time.time() < t_end:
        time.sleep(15)
        t = tape.flush()
        t and emit(t)
        print(f"{datetime.now(timezone.utc):%H:%M:%S}Z polls {poller.stats} feed {feed.stats.get('batches', 0)} batches",
              flush=True)
    poller.halt.set()
    feed.stop()
    t = tape.flush()
    t and emit(t)
    print(f"recorded {path.relative_to(ROOT)}")


# ------------------------------------------------------------------ matching
def subset_match(items: list[tuple], target: float, tol: float, cap: int = 20_000):
    """items: (value, key). Returns (n_solutions capped at 2, one solution as a list of keys) or None if too big."""
    t = round(target * 1000)
    tl = round(tol * 1000)
    items = [(round(v * 1000), k) for v, k in items if 0 < v <= target + tol]
    states: dict[int, tuple] = {0: (1, ())}   # sum -> (solutions capped at 2, one of them)
    for v, k in items:
        add = []
        for s, (c, sol) in states.items():
            if s + v <= t + tl and all(kk[0] != k[0] for kk in sol):   # one print is bought on one side only
                add.append((s + v, c, sol + (k,)))
        for s2, c, sol in add:
            states[s2] = (min(2, states[s2][0] + c), states[s2][1]) if s2 in states else (c, sol)
        if len(states) > cap:
            return None
    hits = [(c, sol) for s, (c, sol) in states.items() if abs(s - t) <= tl and s > 0]
    if not hits:
        return 0, []
    return min(2, sum(c for c, _ in hits)), hits[0][1]


def candidates(window: list[dict], used: set, dv: float, tol: float) -> list[tuple]:
    """(value, (print id, side)) for every unused print side small enough to be part of a dv step."""
    out = []
    for w in window:
        for side, v in (("yes", w["qty"] * w["price"]), ("no", w["qty"] * (1 - w["price"]))):
            if 0 < v <= dv + tol and (w["id"], side) not in used:
                out.append((v, (w["id"], side)))
    return out


def quick_match(window: list[dict], used: set, dv: float, tol: float, prints: dict):
    """One print, or one sweep (every print of one exchange at one instant, one side). Several hits that are the same
    trade in kind (exchange, side, price, size) count as one answer: take the earliest."""
    hits = [[k] for v, k in candidates(window, used, dv, tol) if abs(v - dv) <= tol]
    groups = defaultdict(list)
    for w in window:
        groups[(w["ex"], w["at"])].append(w)
    for g in groups.values():
        if len(g) > 1:
            for side in ("yes", "no"):
                if any((w["id"], side) in used for w in g):
                    continue
                if abs(sum(w["qty"] * (w["price"] if side == "yes" else 1 - w["price"]) for w in g) - dv) <= tol:
                    hits.append([(w["id"], side) for w in g])
    if not hits:
        return 0, []
    kinds = {tuple(sorted((prints[i]["ex"], sd, prints[i]["price"], prints[i]["qty"]) for i, sd in h)) for h in hits}
    if len(kinds) > 1:
        return 2, []
    return 1, min(hits, key=lambda h: min(prints[i]["at"] for i, _ in h))


def classify(w: dict, side: str) -> dict:
    px = w["price"] if side == "yes" else 1 - w["price"]
    bid, ask = w.get("bid"), w.get("ask")
    if side == "yes":
        role = "taker" if ask is not None and w["price"] >= ask - 1e-9 else             "maker" if bid is not None and w["price"] <= bid + 1e-9 else "inside"
    else:
        role = "taker" if bid is not None and w["price"] <= bid + 1e-9 else             "maker" if ask is not None and w["price"] >= ask - 1e-9 else "inside"
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    return {"at": w["at"], "ex": w["ex"], "side": side, "px": px, "qty": w["qty"], "role": role,
            "spread": round(ask - bid, 3) if mid is not None else None,
            "vs_mid": round((mid if side == "yes" else 1 - mid) - px, 4) if mid is not None else None}


def analyse(events: list[dict], a) -> None:
    polls = sorted((e for e in events if e["kind"] == "lb"), key=lambda e: e["t0"])
    prints = {}
    for e in events:
        if e["kind"] != "tape":
            continue
        for r in e["trades"]:
            prints[r[4]] = dict(zip(e["cols"], r))
    tape = sorted(prints.values(), key=lambda r: r["at"])
    if not polls or not tape:
        raise SystemExit(f"no tracker data ({len(polls)} polls, {len(tape)} prints)")
    tt = [ts(r["at"]) for r in tape]
    print(f"{len(polls)} polls {polls[0]['t0'][:16]} -> {polls[-1]['t1'][:16]}Z, {len(tape)} prints, "
          f"{sum(r['qty'] for r in tape):,.0f} shares")

    import bisect
    steps = defaultdict(lambda: {"steps": 0, "dv": 0.0, "dn": 0, "dpnl": 0.0, "matched": [], "amb": 0, "none": 0,
                                 "big": 0, "gap": 0, "pnl": 0.0, "v": 0.0})
    todo = []   # (user, dv, window)
    # Steps are taken between an account's consecutive logged levels (the poller also logs a level, without dv,
    # whenever it restarts, so its own dv misses ~13% of volume). 9 Oct calibration on our own fills: the board shows
    # a fill within 0-30 s (no long bursts, but it can trail by up to ~90 s), so a step covers the prints from the previous poll's start (- lag) to
    # this poll's end. --burst restores the 8 Oct window (since the poll before the previous burst).
    last: dict = {}
    prev_quiet_t1 = None   # t1 of the last poll before the previous burst (--burst)
    last_t1 = None
    prev_t0 = None
    for p in polls:
        t0, t1 = ts(p["t0"]), ts(p["t1"])
        if a.burst:
            burst = any(r.get("dv", 0) > 0 for r in p["rows"])
            lo = (prev_quiet_t1 if prev_quiet_t1 else t0 - 120) - a.lag
            if burst:
                prev_quiet_t1 = last_t1
            last_t1 = t1
        else:
            lo = (prev_t0 if prev_t0 else t0 - 120) - a.lag
            prev_t0 = t0
        window = tape[bisect.bisect_left(tt, lo):bisect.bisect_right(tt, t1)]
        for r in p["rows"]:
            s = steps[r["u"]]
            s["pnl"], s["v"] = r["pnl"], r["v"]
            old = last.get(r["u"])
            last[r["u"]] = (r["n"], r["v"], r["pnl"], t1)
            if old is None:
                continue
            dv, dn = round(r["v"] - old[1], 3), r["n"] - old[0]
            if dv <= 0 or dn < 0:   # new UTC day (the 1d board resets at 00:00Z)
                continue
            s["steps"] += 1
            s["dv"] += dv
            s["dn"] += dn
            s["dpnl"] += r["pnl"] - old[2]
            # An account that dropped off the polled pages (top 200 by 1d pnl) comes back with hours of volume in
            # one step, which the window can't hold (10 Oct: all 4 of our steps were like that): don't match it.
            if t0 - old[3] > a.max_gap:
                s["gap"] += 1
                continue
            todo.append((r["u"], dv, window))
    used: set = set()
    # Pass 1: single prints and sweeps, in time order, each print side goes to one account only.
    rest = []
    for u, dv, window in todo:
        n, sol = quick_match(window, used, dv, a.tol, prints)
        if n == 1:
            used.update(sol)
            steps[u]["matched"] += [classify(prints[i], sd) for i, sd in sol]
        else:
            rest.append((u, dv, window, n))
    # Pass 2: subset search over what is left, where the window is small enough.
    for u, dv, window, n in rest:
        items = candidates(window, used, dv, a.tol)
        m = subset_match(items, dv, a.tol) if len(items) <= 2 * a.max_prints else None
        if m is None or m[0] == 0:   # too many prints, or none fits: one market at a time (an account's step is
            per = defaultdict(list)  # usually one order, sweeping or resting, on one exchange)
            for it in items:
                per[prints[it[1][0]]["ex"]].append(it)
            sols, big = [], False
            for ex_items in per.values():
                if len(ex_items) > 2 * a.max_prints_ex:
                    big = True
                    continue
                mm = subset_match(ex_items, dv, a.tol)
                if mm is None:
                    big = True
                elif mm[0]:
                    sols.append(mm)
            m = (sols[0] if len(sols) == 1 else (2, []) if sols else None if big else (0, []))
        if m is None:
            steps[u]["big"] += 1
        elif m[0] == 0:
            steps[u]["amb" if n == 2 else "none"] += 1
        elif m[0] > 1:
            steps[u]["amb"] += 1
        else:
            used.update(m[1])
            steps[u]["matched"] += [classify(prints[i], sd) for i, sd in m[1]]
    title = {}
    try:
        u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
        title = {str(e["id"]): short(m["title"]) for m in u["markets"] for e in m["exchanges"]}
    except Exception:  # noqa: BLE001
        pass

    rows = sorted(steps.items(), key=lambda kv: -kv[1]["dv"])
    print(f"\n{'account':22} {'vol seen':>9} {'steps':>5} {'matched':>8} {'amb/none/big/gap':>16} {'maker':>6} "
          f"{'taker':>6} {'avg sz':>7} {'px<.2':>6} {'px>.8':>6} {'edge vs mid':>11} top markets")
    for name, s in rows[:a.top]:
        mt = s["matched"]
        mv = sum(x["qty"] * x["px"] for x in mt)
        roles = Counter()
        for x in mt:
            roles[x["role"]] += x["qty"] * x["px"]
        edge = sum((x["vs_mid"] or 0) * x["qty"] for x in mt if x["vs_mid"] is not None)
        sh = sum(x["qty"] for x in mt if x["vs_mid"] is not None)
        exv = Counter()
        for x in mt:
            exv[x["ex"]] += x["qty"] * x["px"]
        tops = ", ".join(f"{title.get(e, e)[:30]} {v / 1e3:.1f}k" for e, v in exv.most_common(3))
        print(f"{name[:22]:22} {s['dv'] / 1e3:8.0f}k {s['steps']:5} {mv / max(s['dv'], 1) * 100:7.0f}% "
              f"{s['amb']:>4}/{s['none']}/{s['big']}/{s['gap']:<5} {roles['maker'] / max(mv, 1) * 100:5.0f}% "
              f"{roles['taker'] / max(mv, 1) * 100:5.0f}% {mv / max(len(mt), 1):7.0f} "
              f"{sum(x['qty'] * x['px'] for x in mt if x['px'] < .2) / max(mv, 1) * 100:5.0f}% "
              f"{sum(x['qty'] * x['px'] for x in mt if x['px'] > .8) / max(mv, 1) * 100:5.0f}% "
              f"{(edge / sh * 100 if sh else 0):+9.2f}c  {tops}")
    if a.user:
        s = steps.get(a.user)
        if not s:
            raise SystemExit(f"{a.user}: no steps")
        print(f"\n{a.user}: {len(s['matched'])} matched prints")
        for x in s["matched"][-a.show:]:
            print(f"  {x['at'][11:19]} {title.get(x['ex'], x['ex'])[:44]:44} {x['side']:3} {x['qty']:>8,.0f} @ "
                  f"{x['px']:.3f} {x['role']:6} spread {x['spread']} vs mid {x['vs_mid']}")
    return steps, polls, prints


def own_fills(lo: float, hi: float, reads: int = 8) -> list[dict]:
    """Our own fills (GET .../portfolio/fills, newest first) between lo - 120 s and hi."""
    from sigbot.client import RateLimiter, SigClient
    from sigbot.markets import TOURNAMENT_SLUG
    c = SigClient(log=False)
    c.read_limiter = RateLimiter(reads)
    fills = []
    for f in c.paginate(f"/tournaments/{TOURNAMENT_SLUG}/portfolio/fills", limit=200):
        t = ts(f["filledAt"])
        if t < lo - 120:
            break
        if t <= hi:
            fills.append(f)
    return fills


def inject_synthetic(events: list[dict], fills: list[dict], name: str, delay: float) -> None:
    """Replace NAME's board rows by a synthetic account whose level is its real cumulative fill volume DELAY s
    before each poll's end (10 Oct: we sat off the polled top-200 pages nearly all day, so the real rows can't
    calibrate anything)."""
    ft = sorted((ts(f["filledAt"]), abs(float(f["quantity"])) * float(f["price"])) for f in fills)
    last = None
    for p in sorted((e for e in events if e["kind"] == "lb"), key=lambda e: e["t0"]):
        cut = ts(p["t1"]) - delay
        done = [v for t, v in ft if t <= cut]
        p["rows"] = [r for r in p["rows"] if r["u"] != name]
        if len(done) != last:
            p["rows"].append({"u": SYN, "n": len(done), "v": sum(done), "pnl": 0.0})
            last = len(done)


SYN = "SYNTHETIC"


def calibrate(steps, polls, prints, name: str, fills: list[dict] | None = None) -> None:
    """Score the matcher on our own account: its real fills (GET .../portfolio/fills, ours only) over the tracked
    window vs what the matcher attributed to it. Volume recovered, attributed prints that are not ours, and the
    share of our fills the tape carries at all (a print the feed dropped can never be matched)."""
    lo, hi = ts(polls[0]["t0"]), ts(polls[-1]["t1"])
    if fills is None:
        fills = own_fills(lo, hi)
    fills = [f for f in fills if lo - 120 <= ts(f["filledAt"]) <= hi]
    real_v = sum(abs(float(f["quantity"])) * float(f["price"]) for f in fills)
    # our fill <-> tape print: same exchange, same YES price, same size, within 5 s
    by_key = defaultdict(list)
    for w in prints.values():
        by_key[(w["ex"], round(w["price"], 3), round(w["qty"]))].append(ts(w["at"]))
    on_tape = 0.0
    for f in fills:
        side, px = f["side"], float(f["price"])
        ypx = px if side == "yes" else 1 - px
        if any(abs(t - ts(f["filledAt"])) <= 5 for t in by_key.get((str(f["exchangeId"]), round(ypx, 3),
                                                                    round(abs(float(f["quantity"])))), [])):
            on_tape += abs(float(f["quantity"])) * px
    mine = {(str(f["exchangeId"]), f["side"], round(abs(float(f["quantity"])))) for f in fills}
    s = steps.get(name, {"matched": [], "dv": 0.0})
    good = bad = 0.0
    wrong = []
    for x in s["matched"]:
        v = x["qty"] * x["px"]
        if (x["ex"], x["side"], round(x["qty"])) in mine:
            good += v
        else:
            bad += v
            wrong.append(x)
    others_ours = 0.0   # our prints given to someone else
    for u, st in steps.items():
        if u == name:
            continue
        others_ours += sum(x["qty"] * x["px"] for x in st["matched"]
                           if (x["ex"], x["side"], round(x["qty"])) in mine)
    pct = lambda x: f"{x / max(real_v, 1) * 100:.0f}%"
    print(f"\nCALIBRATION {name}: {len(fills)} real fills, volume {real_v:,.0f} (board steps {s['dv']:,.0f}); "
          f"on tape {on_tape:,.0f} ({pct(on_tape)}); matched to us correctly {good:,.0f} ({pct(good)}), "
          f"wrongly {bad:,.0f}; our prints given to others {others_ours:,.0f}")
    for x in wrong[:10]:
        print(f"  wrong: {x['at'][11:19]} ex {x['ex']} {x['side']} {x['qty']:,.0f} @ {x['px']:.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", type=float, help="record this many minutes locally instead of analysing")
    ap.add_argument("--local", action="store_true", help="analyse data/lbtrack/ and logs/ instead of run-logs")
    ap.add_argument("--since", help="UTC start, e.g. 2026-10-08T16")
    ap.add_argument("--lag", type=float, default=90,
                    help="slack (s) before the previous poll's start (9 Oct calibration: 5 -> 5%%, 30 -> 51%%, 90 -> 64%%)")
    ap.add_argument("--max-gap", type=float, default=120,
                    help="skip steps whose previous level is older than this (s): the account was off the polled pages")
    ap.add_argument("--synthetic", type=float, metavar="DELAY",
                    help="with --calibrate: also inject NAME's real fills as a synthetic account that the board shows "
                         "DELAY s after each fill (for when NAME is rarely on the polled pages)")
    ap.add_argument("--burst", action="store_true", help="8 Oct windows: since the poll before the previous burst")
    ap.add_argument("--tol", type=float, default=TOL)
    ap.add_argument("--max-prints", type=int, default=12,
                    help="full subset search only over this many candidate prints (else per market)")
    ap.add_argument("--max-prints-ex", type=int, default=16, help="per-market subset search limit")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--user")
    ap.add_argument("--show", type=int, default=40)
    ap.add_argument("--calibrate", metavar="NAME",
                    help="score the matcher on our own account NAME against its real fills (8 reads/min)")
    a = ap.parse_args()
    if a.record:
        record(a.record)
        return
    ev = local_events() if a.local else run_logs_events(a.since)
    if a.since:
        ev = [e for e in ev if (e.get("t0") or e.get("ts", "")) >= a.since]
    fills = None
    if a.calibrate and a.synthetic is not None:
        lb = [e for e in ev if e["kind"] == "lb"]
        fills = own_fills(ts(min(e["t0"] for e in lb)), ts(max(e["t1"] for e in lb)))
        inject_synthetic(ev, fills, a.calibrate, a.synthetic)
    out = analyse(ev, a)
    if a.calibrate:
        calibrate(*out, SYN if a.synthetic is not None else a.calibrate, fills)


if __name__ == "__main__":
    main()
