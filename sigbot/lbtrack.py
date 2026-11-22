"""Leaderboard tracker: which accounts trade what, from public data only (READ-ONLY, places nothing).

SIG's trade prints carry no account, but GET /leaderboards?period=1d updates within seconds of a trade and gives
every account's tradesCount and volume for the UTC day. Volume is shares x the price paid for the side bought
(checked 8 Oct against our own fills: 20,955.8 to the cent), so an account's volume step between two polls has to
equal a subset of the prints in that window, each worth q x p (bought YES) or q x (1 - p) (bought NO). Matching
the steps to the prints (`leader_study.py`) shows each leader's markets, side, size and whether it took liquidity
or rested, from the top of book just before the print.

Two halves, both appended to the caller's event log:
  - `LeaderPoller` (thread): polls the 1d leaderboard every `every_s` and logs {"kind": "lb"} with the rows that
    changed since the previous poll (username, dTrades, dVolume, dPnl, trades, volume, pnl).
  - `Tape`: the caller feeds it realtime trade prints with the book top just before them; `flush()` returns them
    as {"kind": "tape"} for the log.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


class LeaderPoller(threading.Thread):
    def __init__(self, client, emit, every_s: float = 15, pages: int = 2, active=lambda: True):
        super().__init__(daemon=True, name="lbtrack")
        self.c, self.emit, self.every_s, self.pages, self.active = client, emit, every_s, pages, active
        self.halt = threading.Event()
        self.prev: dict[str, tuple] = {}
        self.names: set = set()
        self.stats = {"polls": 0, "errors": 0, "rows": 0}

    def poll(self) -> None:
        t0 = time.time()
        rows = []
        for page in range(self.pages):
            r = self.c.get("/leaderboards", period="1d", limit=100, offset=100 * page)
            rows += r.get("leaderboard") or []
            if len(r.get("leaderboard") or []) < 100:
                break
        t1 = time.time()
        out = []
        for x in rows:
            pid, n, v, p = x.get("profileId"), int(x.get("tradesCount") or 0), float(x.get("volume") or 0), \
                float(x.get("pnl") or 0)
            old = self.prev.get(pid)
            self.prev[pid] = (n, v, p)
            if old is None:
                d = None   # first sight (or new on the page): logged as a level, not a step
            elif (n, v) == old[:2]:
                continue
            else:
                d = (n - old[0], round(v - old[1], 3), round(p - old[2], 2))
            name = x.get("username")
            row = {"u": name, "n": n, "v": round(v, 3), "pnl": round(p, 2), "rank": x.get("rank")}
            if d is not None:
                row.update(dn=d[0], dv=d[1], dpnl=d[2])
            if name not in self.names:
                self.names.add(name)
                row["pid"] = pid
            out.append(row)
        self.stats["polls"] += 1
        self.stats["rows"] += len(out)
        self.emit({"kind": "lb", "t0": _iso(t0), "t1": _iso(t1), "rows": out})

    def run(self):
        while not self.halt.is_set():
            t = time.time()
            try:
                if self.active():   # no tape without the feed, so no point spending reads
                    self.poll()
                else:
                    self.prev.clear()   # the next poll after a gap is a level, not a step
            except Exception as e:  # noqa: BLE001 - a missed poll only widens the next window
                self.stats["errors"] += 1
                self.emit({"kind": "lb_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            self.halt.wait(max(1.0, self.every_s - (time.time() - t)))


class Tape:
    """Realtime trade prints, deduped on id, each with the YES book top just before its batch was applied."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows: list = []
        self.seen: set = set()

    def add(self, trades: list, tops: dict) -> None:
        with self.lock:
            for t in trades:
                tid = t.get("id")
                if tid is None or tid in self.seen:
                    continue
                self.seen.add(tid)
                ex = str(t.get("exchangeId"))
                bid, ask = tops.get(ex) or (None, None)
                self.rows.append([t.get("executedAt"), ex, float(t.get("price") or 0), float(t.get("quantity") or 0),
                                  tid, bid, ask, _iso(time.time())])

    def flush(self) -> dict | None:
        with self.lock:
            rows, self.rows = self.rows, []
        return {"kind": "tape", "cols": ["at", "ex", "price", "qty", "id", "bid", "ask", "seen"], "trades": rows} \
            if rows else None
