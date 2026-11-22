"""Polymarket quotes by market id (Gamma API, public, no key): one batched request per 50 ids.

The exchange -> Polymarket market id map lives in config/universe.json under "polymarket". Each id is a
candidate's "<name> (D) - AL-02 House Election Winner" market, i.e. that party's nominee; built 4 Oct 2026 from the
229-contract map in github.com/grissou/prediction-market-maker (ref_map.json), checked against Kalshi mids (median
gap 0.0095 over the 189 exchanges both list, max 0.05).
"""
from __future__ import annotations

import requests

GAMMA = "https://gamma-api.polymarket.com"


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Polymarket:
    def __init__(self, timeout: float = 10.0):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "sigbot"
        self.timeout = timeout

    def quotes(self, ids: list[str]) -> dict[str, dict]:
        """{market id: {"bid", "ask", "mid", "active", "volume"}}; mid is None for a one-sided book."""
        out = {}
        ids = sorted(set(ids))
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            r = self.s.get(f"{GAMMA}/markets", params=[("id", x) for x in chunk] + [("limit", len(chunk))],
                           timeout=self.timeout)
            r.raise_for_status()
            for m in r.json() or []:
                bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
                ok = bid is not None and ask is not None and 0 < bid <= ask < 1
                out[str(m["id"])] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2 if ok else None,
                                     "active": bool(m.get("active")) and not m.get("closed"),
                                     "volume": _f(m.get("volumeNum"))}
        return out
