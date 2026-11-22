"""Daily profit report for the SIG Predictions Cup bot. READ-ONLY: places and cancels nothing.

  python daily_report.py              # last 24 h -> reports/daily_<UTC date>.md + one line in reports/daily.jsonl

Splits the window's fills into
  tight    opening fills of maker.py's ladder (edge at fill < --catcher-edge)
  catcher  opening fills at least --catcher-edge under fair (the sweep catchers)
  snipe    fills of sniper orders (order ids from the sniper events in run-logs' maker_logs)
  exit     fills that closed an opposite position (giving back edge for fast turnover)
and values each against Kalshi fair: at the fill (1-min history) and now. Adds account value (cash + positions at
Kalshi fair, SIG mid where Kalshi has none), the leaderboard, and a projection to the close from the measured
daily rates. Earlier reports (run-logs:reports/daily.jsonl) give day-over-day decay once two days exist.

Rate limits are per key and the live maker uses ~90 reads/min, so this paces itself at 8 reads/min.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from sigbot.client import ROOT, RateLimiter, SigClient
from sigbot.kalshi import BASE as KALSHI
from sigbot.markets import TOURNAMENT_SLUG as T

LADDER_END = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
OUT = ROOT / "reports"
COMPONENTS = ("tight", "catcher", "snipe", "exit", "unpriced")


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def git(*args) -> str | None:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def run_logs() -> tuple[set, list[dict]]:
    """(sniper order ids, earlier daily report lines) from the run-logs branch; empty if unreachable."""
    if git("fetch", "-q", "origin", "run-logs") is None:
        return set(), []
    snipe_ids = set()
    for name in (git("ls-tree", "--name-only", "FETCH_HEAD", "maker_logs/") or "").split():
        if name.endswith(".json"):
            try:
                for e in json.loads(git("show", f"FETCH_HEAD:{name}") or "{}").get("snipes") or []:
                    if e.get("orderId") is not None:
                        snipe_ids.add(e["orderId"])
            except ValueError:
                pass
    prev = []
    for line in (git("show", "FETCH_HEAD:reports/daily.jsonl") or "").splitlines():
        try:
            prev.append(json.loads(line))
        except ValueError:
            pass
    return snipe_ids, prev


def kalshi_history(tickers: set, start: int, end: int) -> dict[str, tuple[list, list]]:
    """ticker -> (minute timestamps, mids); only tight two-sided minutes."""
    s, out = requests.Session(), {}
    for tk in sorted(tickers):
        pts = []
        for a in range(start, end, 4000 * 60):
            for attempt in range(5):
                r = s.get(f"{KALSHI}/series/{tk.split('-')[0]}/markets/{tk}/candlesticks",
                          params={"start_ts": a, "end_ts": min(end, a + 4000 * 60), "period_interval": 1}, timeout=20)
                if r.status_code != 429:
                    break
                time.sleep(1 + attempt)
            if r.status_code != 200:
                break
            for k in r.json().get("candlesticks", []):
                try:
                    b, x = float(k["yes_bid"]["close_dollars"]), float(k["yes_ask"]["close_dollars"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 < b <= x < 1 and x - b <= 0.05:
                    pts.append((k["end_period_ts"], (b + x) / 2))
        if pts:
            out[tk] = ([p[0] for p in pts], [p[1] for p in pts])
    return out


def at(series, t: float) -> float | None:
    if not series:
        return None
    i = bisect.bisect_right(series[0], t) - 1
    return series[1][i] if i >= 0 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--catcher-edge", type=float, default=0.11, help="opening fills this far under fair = catcher")
    ap.add_argument("--decay", type=float, default=0.5 ** (1 / 7), help="assumed daily decay until 2 days are measured")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=a.hours)
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    tid, kmap = u["tournamentId"], u["kalshi"]
    title = {e["id"]: m["title"] for m in u["markets"] for e in m["exchanges"]}

    c = SigClient()
    c.read_limiter = RateLimiter(8)    # share the key politely with the live maker + sniper (~90 reads/min)
    c.write_limiter = RateLimiter(1)
    snipe_ids, prev = run_logs()

    fills = []
    for f in c.paginate(f"/tournaments/{T}/portfolio/fills", limit=200):
        if f["filledAt"] < start.isoformat()[:19]:
            break
        fills.append(f)
    fills.sort(key=lambda f: (f["filledAt"], f["id"]))
    pos = {str(p["exchangeId"]): float(p["quantity"])
           for p in c.get(f"/tournaments/{T}/portfolio/positions").get("positions", []) if not p.get("settled")}
    cash = float(c.get(f"/tournaments/{T}")["myBalance"])
    tops = {}
    ids = list(title)
    for i in range(0, len(ids), 100):
        r = c.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=tid)
        for x in (r.get("data") if isinstance(r, dict) else r) or []:
            if x.get("bestBid") is not None and x.get("bestAsk") is not None:
                tops[str(x["exchangeId"])] = (x["bestBid"] + x["bestAsk"]) / 2
    lb = c.get("/leaderboards").get("leaderboard", [])
    me = None
    try:   # our own row, matched by the profile id the realtime token names (no account/profile endpoint)
        pid = c.post("/realtime/token", {})["channels"]["user"].split(":")[-1]
        me = next((r for r in lb if r.get("profileId") == pid), None)
    except Exception:  # noqa: BLE001
        pass

    hist = kalshi_history({kmap[f["exchangeId"]] for f in fills if f["exchangeId"] in kmap} |
                          {kmap[e] for e in pos if e in kmap}, int(start.timestamp()) - 3600, int(now.timestamp()))

    def fair_yes(ex, t):
        v = at(hist.get(kmap.get(ex)), t)
        return v if v is not None else None

    # Net position at the window start, backed out from positions now, so each fill can be called open or close.
    net = defaultdict(float, pos)
    for f in fills:
        net[f["exchangeId"]] -= f["quantity"]
    comp = {k: defaultdict(float) for k in COMPONENTS}
    best = []
    for f in fills:
        ex, q, px, t = f["exchangeId"], f["quantity"], f["price"], ts(f["filledAt"])
        before = net[ex]
        net[ex] += q
        sh = abs(q)
        f0, fn = fair_yes(ex, t), fair_yes(ex, now.timestamp())
        if f0 is None or fn is None:
            k = "unpriced"
            e0 = en = 0.0
        else:
            conv = (lambda p: p) if f["side"] == "yes" else (lambda p: 1 - p)
            e0, en = conv(f0) - px, conv(fn) - px
            if f.get("orderId") in snipe_ids:
                k = "snipe"
            elif before * q < 0:
                k = "exit"
            else:
                k = "catcher" if e0 >= a.catcher_edge else "tight"
        d = comp[k]
        d["fills"] += 1
        d["shares"] += sh
        d["edge_at_fill"] += sh * e0
        d["value_now"] += sh * en
        if k in ("catcher", "snipe"):
            best.append((sh * en, f["filledAt"][5:16], f["side"], px, sh, title.get(ex, ex)[:50]))

    # Account value at Kalshi fair (SIG mid where Kalshi has no price).
    held = 0.0
    for ex, q in pos.items():
        fy = fair_yes(ex, now.timestamp())
        fy = fy if fy is not None else tops.get(ex)
        if fy is not None:
            held += q * fy if q > 0 else -q * (1 - fy)
    value = cash + held

    earned = sum(comp[k]["value_now"] for k in ("tight", "catcher", "snipe", "exit"))
    per_day = earned * 24 / a.hours
    days_left = max(0.0, (LADDER_END - now).total_seconds() / 86400)
    decay = a.decay
    last = prev[-1] if prev else None
    if last and last.get("earned_per_day"):
        decay = min(1.2, max(0.3, per_day / last["earned_per_day"])) ** 0.5 * a.decay ** 0.5   # blend measured + prior
    whole = int(days_left)
    remaining = per_day * (sum(decay ** k for k in range(1, whole + 1)) + (days_left - whole) * decay ** (whole + 1))
    projected = value + remaining

    rank = f"#{me['rank']} at {me['pnl']:+,.0f}" if me else f"outside the top {len(lb)}"
    top = ", ".join(f"#{r['rank']} {r['pnl']:+,.0f}" for r in lb[:3])
    third = lb[2]["pnl"] + 100000 if len(lb) >= 3 else None
    lines = [
        f"# SIG Cup daily report {now:%Y-%m-%d %H:%MZ}",
        "",
        f"- **Account value (Kalshi fair):** {value:,.0f}  (cash {cash:,.0f}, positions {held:,.0f}, {len(pos)} held)"
        + (f"  | 24 h change {value - last['value']:+,.0f}" if last else ""),
        f"- **Leaderboard:** you {rank}; top 3: {top}" + (f"  (3rd = balance {third:,.0f})" if third else ""),
        f"- **Earned in the last {a.hours:.0f} h (vs Kalshi fair now):** {earned:+,.0f}",
        "",
        "| Component | Fills | Shares | Edge at fill | Value now |",
        "|---|---:|---:|---:|---:|",
    ]
    for k in COMPONENTS:
        d = comp[k]
        lines.append(f"| {k} | {d['fills']:,.0f} | {d['shares']:,.0f} | {d['edge_at_fill']:+,.0f} | {d['value_now']:+,.0f} |")
    lines += [
        "",
        f"- **Projection to the ladder end (3 Nov 21:00Z, {days_left:.1f} days):** {per_day:+,.0f}/day now, "
        f"decay {decay:.2f}/day -> **~{projected:,.0f}** before election night"
        + ("" if last else " (decay assumed: halving weekly; measured from tomorrow)"),
        "- Caveats: values are marked at Kalshi fair, the final ranking uses settled results; 'unpriced' fills (no "
        "Kalshi market) are counted but not valued.",
    ]
    if best:
        lines += ["", "**Biggest catcher / sniper fills**", ""]
        for v, when, side, px, sh, name in sorted(best, reverse=True)[:8]:
            lines.append(f"- {when}Z {side.upper()} {sh:,.0f} @ {px:.3f}: {v:+,.0f}  {name}")
    report = "\n".join(lines) + "\n"

    OUT.mkdir(exist_ok=True)
    (OUT / f"daily_{now:%Y%m%d}.md").write_text(report, encoding="utf-8")
    row = {"at": now.isoformat(), "hours": a.hours, "value": round(value), "cash": round(cash),
           "earned": round(earned), "earned_per_day": round(per_day), "decay": round(decay, 3),
           "projected": round(projected), "rank": me["rank"] if me else None,
           "components": {k: {kk: round(vv) for kk, vv in comp[k].items()} for k in COMPONENTS}}
    # Rewritten whole from run-logs' copy + today, so a fresh cloud checkout keeps the history.
    (OUT / "daily.jsonl").write_text("".join(json.dumps(r) + "\n" for r in prev + [row]), encoding="utf-8")
    print(report)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            summary = [l.replace("**", "") for l in lines if l.startswith("- **")]
            requests.post(f"https://ntfy.sh/{topic}", data="\n".join(summary).encode(), timeout=10)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
