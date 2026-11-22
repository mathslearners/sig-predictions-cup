"""External fair values for SIG markets.

Sources (each optional; a failing source is skipped, never fatal):
  kalshi      public API, party-level markets (e.g. SENATEME-26-R)
  polymarket  gamma API, "<State> Senate Election Winner"-style events
  overrides   config/fair_overrides.csv, your own probabilities (win over everything)

Result: {(race_key, party): Quote(p, sources, spread)}.
Both external domains must be on the cloud environment's network allowlist:
  api.elections.kalshi.com, gamma-api.polymarket.com
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field

import requests

from .client import ROOT
from .races import Key, key_from_kalshi_series, key_from_text, party_of

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
OVERRIDES = ROOT / "config" / "fair_overrides.csv"
MAX_EXT_SPREAD = 0.15   # ignore an external quote wider than this
UA = {"User-Agent": "sig-predictions-cup-bot/0.1", "Accept": "application/json"}


@dataclass
class Quote:
    p: float
    sources: dict = field(default_factory=dict)   # source -> mid
    override: bool = False

    @property
    def disagreement(self) -> float:
        v = list(self.sources.values())
        return max(v) - min(v) if len(v) > 1 else 0.0


def _num(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v / 100 if v > 1 else v   # Kalshi legacy fields are in cents


def _mid(bid, ask, fallback=None) -> float | None:
    b, a = _num(bid), _num(ask)
    if b is not None and a is not None and 0 < b <= a < 1 and a - b <= MAX_EXT_SPREAD:
        return (a + b) / 2
    f = _num(fallback)
    return f if f is not None and 0 < f < 1 else None


def _get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


# --------------------------------------------------------------------------- Kalshi
def kalshi_quotes(max_pages: int = 80) -> dict[tuple[Key, str], float]:
    out, cursor = {}, None
    for _ in range(max_pages):
        page = _get(f"{KALSHI}/events", {"status": "open", "with_nested_markets": "true",
                                        "limit": 200, "cursor": cursor})
        for ev in page.get("events", []):
            if "26" not in ev.get("event_ticker", ""):
                continue
            key = key_from_kalshi_series(ev.get("series_ticker", ""))
            if not key:
                continue
            for m in ev.get("markets", []):
                if m.get("status") not in (None, "active", "open"):
                    continue
                party = (party_of(m.get("subtitle") or "") or party_of(m.get("title") or "")
                         or {"R": "R", "D": "D", "I": "I"}.get(m.get("ticker", "").rsplit("-", 1)[-1]))
                if not party:
                    continue
                p = _mid(m.get("yes_bid_dollars", m.get("yes_bid")),
                         m.get("yes_ask_dollars", m.get("yes_ask")),
                         m.get("last_price_dollars", m.get("last_price")))
                if p is not None:
                    out[(key, party)] = p
        cursor = page.get("cursor")
        if not cursor:
            break
    return out


# --------------------------------------------------------------------------- Polymarket
_PM_TITLE = re.compile(r"(senate|governor|gubernatorial|house|district|[A-Z]{2}-\d{1,2})", re.I)


def polymarket_quotes(max_pages: int = 40) -> dict[tuple[Key, str], float]:
    out = {}
    for page in range(max_pages):
        evs = _get(f"{GAMMA}/events", {"closed": "false", "active": "true",
                                       "limit": 500, "offset": page * 500})
        if not evs:
            break
        for ev in evs:
            title = ev.get("title") or ""
            low = title.lower()
            if not _PM_TITLE.search(title) or "primary" in low or "nominee" in low:
                continue
            if not ((ev.get("endDate") or "").startswith("2026") or "2026" in title):
                continue  # other election cycles
            key = key_from_text(title)
            if not key:
                continue
            for m in ev.get("markets", []):
                if not m.get("active") or m.get("closed"):
                    continue
                party = party_of(m.get("question") or "") or party_of(m.get("groupItemTitle") or "")
                if not party:
                    continue
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    prices = re.findall(r"[\d.]+", prices)
                last = prices[0] if prices else None
                p = _mid(m.get("bestBid"), m.get("bestAsk"), last)
                if p is not None:
                    out[(key, party)] = p
        if len(evs) < 500:
            break
    return out


# --------------------------------------------------------------------------- overrides
def override_quotes() -> dict[tuple[Key, str], float]:
    """config/fair_overrides.csv: race,party,p   e.g.  Maine Senate,R,0.42"""
    out = {}
    if not OVERRIDES.exists():
        return out
    with OVERRIDES.open(encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            key = key_from_text(r.get("race", ""))
            p = _num(r.get("p"))
            party = (r.get("party") or "").strip().upper()[:1]
            if key and p is not None and party in "RDI" and party:
                out[(key, party)] = p
    return out


def fair_values(use_kalshi=True, use_polymarket=True, log=print) -> dict[tuple[Key, str], Quote]:
    merged: dict[tuple[Key, str], Quote] = {}
    for name, enabled, fn in (("kalshi", use_kalshi, kalshi_quotes),
                              ("polymarket", use_polymarket, polymarket_quotes)):
        if not enabled:
            continue
        try:
            qs = fn()
            log(f"fair values: {name} -> {len(qs)} race/party quotes")
        except Exception as e:  # noqa: BLE001 - a dead source must never stop the run
            log(f"fair values: {name} FAILED ({type(e).__name__}: {e}); skipping")
            continue
        for k, p in qs.items():
            merged.setdefault(k, Quote(p=0.0)).sources[name] = p
    for k, q in merged.items():
        q.p = sum(q.sources.values()) / len(q.sources)
    ov = override_quotes()
    if ov:
        log(f"fair values: overrides -> {len(ov)}")
    for k, p in ov.items():
        merged[k] = Quote(p=p, sources={"override": p}, override=True)
    return merged
