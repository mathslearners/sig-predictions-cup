"""Market discovery, title parsing and orderbook summarising for the Midterm Elections tournament."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .client import SigClient

TOURNAMENT_SLUG = "midterm-elections"

_TITLE = re.compile(r"^Will the (Republican|Democratic|Independent) Party win (?:the )?(.+?)\??$")
_PARTY = {"Republican": "R", "Democratic": "D", "Independent": "I"}


@dataclass
class Tournament:
    id: str
    slug: str
    name: str


def resolve_tournament(markets: list[dict], slug: str = TOURNAMENT_SLUG) -> Tournament:
    """Pick the tournament from markets' contexts[] (must resolve to exactly one id)."""
    found = {(t["id"], t["slug"], t["name"])
             for m in markets for ctx in m.get("contexts", [])
             if (t := ctx.get("tournament")) and t.get("slug") == slug}
    if len(found) != 1:
        raise RuntimeError(f"Expected one tournament with slug {slug!r}, found {found}")
    return Tournament(*found.pop())


def parse_title(title: str) -> tuple[str | None, str | None, str | None]:
    """-> (race, race_type, party) e.g. ('Maine Senate', 'Senate', 'R')."""
    m = _TITLE.match(title.strip())
    if not m:
        return None, None, None
    party, race = _PARTY[m.group(1)], m.group(2)
    if race.startswith("U.S. "):
        race_type = "Chamber"
    elif race.endswith("House race"):
        race = race.removesuffix(" race")
        race_type = "House"
    elif race.endswith("Senate"):
        race_type = "Senate"
    elif race.endswith("Governor"):
        race_type = "Governor"
    else:
        race_type = "Other"
    return race, race_type, party


def fetch_markets(client: SigClient) -> tuple[Tournament, list[dict]]:
    """All markets attached to the Midterm Elections tournament."""
    markets = list(client.paginate("/markets"))
    t = resolve_tournament(markets)
    in_t = [m for m in markets
            if any((ctx.get("tournament") or {}).get("id") == t.id for ctx in m.get("contexts", []))]
    return t, in_t


def book_stats(book: dict) -> dict:
    """Top-of-book and depth figures from an /exchanges/{id}/orderbook response (YES-normalised)."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    bb, ba = (bids[0] if bids else None), (asks[0] if asks else None)
    best_bid = bb["price"] if bb else None
    best_ask = ba["price"] if ba else None
    two_sided = best_bid is not None and best_ask is not None
    return {
        "best_bid": best_bid,
        "bid_size": bb["quantity"] if bb else None,
        "best_ask": best_ask,
        "ask_size": ba["quantity"] if ba else None,
        "mid": round((best_bid + best_ask) / 2, 4) if two_sided else None,
        "spread": round(best_ask - best_bid, 4) if two_sided else None,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "bid_depth": sum(l["quantity"] for l in bids),
        "ask_depth": sum(l["quantity"] for l in asks),
    }
