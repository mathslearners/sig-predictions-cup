"""Kalshi public market data as the external fair-value reference.

SIG's pre-open seed quotes were Kalshi mids +/- 0.04 (e.g. Maine Senate D: Kalshi 0.59/0.60, SIG 0.555/0.635),
so a live Kalshi mid is the best available estimate of where SIG's quotes *should* be.

Ticker scheme (verified 30 Sep 2026): SENATE{ST}-26, GOVPARTY{ST}-26, HOUSE{ST}{N}-26 (no zero pad),
CONTROLH-2026 / CONTROLS-2026; party markets end in -D / -R / -I. No auth needed.
"""
from __future__ import annotations

import re
import time

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}
_HOUSE = re.compile(r"^([A-Z]{2})-(\d+) House$")
# Races whose Kalshi ticker breaks the pattern (checked 30 Sep 2026). "-27" = term starting 2027.
EVENT_OVERRIDES = {"Florida Senate": "SENATEFLS-26", "Kansas Governor": "GOVPARTYKS-27"}
# No Kalshi event found for: AK/NH Governor, KY Senate and 17 House seats -> fall back to SIG's seed mid.


def event_ticker(race: str, race_type: str) -> str | None:
    """SIG race name -> Kalshi event ticker, or None if the race can't be mapped."""
    if race in EVENT_OVERRIDES:
        return EVENT_OVERRIDES[race]
    if race_type == "Chamber":
        return {"U.S. House": "CONTROLH-2026", "U.S. Senate": "CONTROLS-2026"}.get(race)
    if race_type == "House":
        m = _HOUSE.match(race)
        return f"HOUSE{m.group(1)}{int(m.group(2))}-26" if m else None
    state = race.rsplit(" ", 1)[0]
    st = STATES.get(state)
    if not st:
        return None
    return {"Senate": f"SENATE{st}-26", "Governor": f"GOVPARTY{st}-26"}.get(race_type)


def _num(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


class Kalshi:
    def __init__(self, timeout: float = 15.0):
        self.s = requests.Session()
        self.timeout = timeout

    def event_markets(self, ticker: str) -> list[dict]:
        for attempt in range(4):
            r = self.s.get(f"{BASE}/markets", params={"event_ticker": ticker, "limit": 100}, timeout=self.timeout)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json().get("markets", [])
        return []

    def party_quotes(self, ticker: str) -> dict[str, dict]:
        """{party: {bid, ask, mid, last, sub_title, ticker}} for one event. Party taken from the ticker suffix."""
        out, others = {}, []
        for m in self.event_markets(ticker):
            suffix = m["ticker"].rsplit("-", 1)[-1]
            if suffix in ("D", "R", "I"):
                out[suffix] = quote_of(m)
            else:
                others.append(quote_of(m))
        # Independents are keyed by candidate (NE Senate "DOSB" = Osborn, RI Governor "KBLO" = Block).
        if len(others) == 1 and "I" not in out:
            out["I"] = others[0]
        return out

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        """{market_ticker: quote} via GET /markets?tickers=a,b,c (chunks of 50). Missing tickers are omitted."""
        out = {}
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            for attempt in range(4):
                r = self.s.get(f"{BASE}/markets", params={"tickers": ",".join(chunk), "limit": 100},
                               timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1 + attempt)
                    continue
                r.raise_for_status()
                for m in r.json().get("markets", []):
                    out[m["ticker"]] = quote_of(m)
                break
        return out


def quote_of(m: dict) -> dict:
    bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
    # Kalshi reports an empty side as 0 bid / 1 ask; treat a one-sided book as having no mid.
    mid = (bid + ask) / 2 if bid is not None and ask is not None and 0 < bid <= ask < 1 else None
    return {"bid": bid, "ask": ask, "mid": mid, "last": _num(m.get("last_price_dollars")),
            "sub_title": m.get("yes_sub_title"), "ticker": m["ticker"], "status": m.get("status")}
