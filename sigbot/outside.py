"""Fair value from the outside venues: Kalshi and Polymarket quotes blended into one {"p", "buffer"}."""
from __future__ import annotations

MAX_K_SPREAD = 0.05     # ignore Kalshi quotes wider than this
MAX_P_SPREAD = 0.05     # ... and Polymarket quotes wider than this
MAX_VENUE_GAP = 0.06    # Kalshi and Polymarket further apart than this: trust Kalshi, widen the buffer


def outside_fair(k: dict | None, p: dict | None) -> dict | None:
    """{"p", "buffer", "sources"} from a Kalshi quote and/or a Polymarket quote (either may be None).

    Both tight: the mean (on 4 Oct the two sat a median 0.0095 apart over 189 exchanges, at most 0.05), buffer from
    their spreads plus half of any gap beyond 0.02, so the ladder and the sniper stand further back where the venues
    disagree. Polymarket alone (36 House/KY/NH exchanges Kalshi doesn't list): its mid, buffer >= 0.005."""
    kv = k if (k and k.get("mid") is not None and k.get("status") == "active"
               and k["ask"] - k["bid"] <= MAX_K_SPREAD) else None
    pv = p if (p and p.get("mid") is not None and p.get("active")
               and p["ask"] - p["bid"] <= MAX_P_SPREAD) else None
    if kv and pv:
        gap = abs(kv["mid"] - pv["mid"])
        ks, ps = kv["ask"] - kv["bid"], pv["ask"] - pv["bid"]
        if gap > MAX_VENUE_GAP:   # one venue stale or a news move half-priced: Kalshi, standing well back
            return {"p": kv["mid"], "buffer": max(0.0, ks - 0.01) / 2 + gap / 2,
                    "sources": {"kalshi": kv["mid"], "polymarket": pv["mid"]}}
        return {"p": (kv["mid"] + pv["mid"]) / 2,
                "buffer": max(0.0, (ks + ps) / 2 - 0.01) / 2 + max(0.0, gap - 0.02) / 2,
                "sources": {"kalshi": kv["mid"], "polymarket": pv["mid"]}}
    if kv:
        return {"p": kv["mid"], "buffer": max(0.0, (kv["ask"] - kv["bid"]) - 0.01) / 2, "sources": {"kalshi": kv["mid"]}}
    if pv:
        return {"p": pv["mid"], "buffer": max(0.005, (pv["ask"] - pv["bid"]) - 0.01) / 2,
                "sources": {"polymarket": pv["mid"]}}
    return None

