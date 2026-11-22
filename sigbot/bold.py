"""Bold overlay: choose the bets that maximise P(final balance >= the 3rd-place bar), not expected SUSQies.

The prize pays the top 3 only, and on 12 Oct the 3rd-place balance (~850k) was 4-5x ours. With a fair-ish casino the
best way to reach a far target is bold play (Dubins-Savage): few, large, correlated bets whose win lands near the
target, instead of many small positive-edge ones. This module does it numerically:

  * outcomes: joint draws from the swing model (SwingMixture.sample), so a party wave moves every race together;
    chamber control from the Senate seat count (D needs `need` of the Senate races) and, for the House (only ~40
    seats are on SIG), a probit on the national swing calibrated to the House-control price;
  * candidate lots: every price level of every SIG offer (both sides of every exchange; buying the side opposite a
    holding is a sale, so de-risking is in the same search);
  * wealth per draw = cash kept x (1 + night gain G) + payoff of holdings and new lots, where G is a random return on
    the cash left for election-night speed trading (election.py's other roles);
  * objective: P(W >= T) over draws, with T drawn around the 3rd-place balance plus the leaders' own expected night
    gain; while P is ~0 the tie-break is the upper tail of W - T (see optimise). Greedy: add the 4%-of-bankroll
    chunk of the lot that raises it most; stop when nothing does. Ahead of the bar this buys nothing (risk only
    lowers P); far behind it concentrates, even on long shots SIG prices above fair (any chance beats none).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from scipy.special import ndtr

SENATE_NEED = 17          # D needs 17 of the 35 Senate races up in 2026 (34 holdovers + VP R), see swing.py


@dataclass
class Lot:
    ex: str
    side: str        # "yes" / "no"
    price: float     # price of that side
    qty: float       # shares available at that price


def outcome_table(races, names, anti, nat, house_p: float | None, senate_ex: tuple | None,
                  house_ex: tuple | None, rng: np.random.Generator, senate_need: int = SENATE_NEED,
                  house_sd: float = 2.0) -> dict[str, np.ndarray]:
    """exchange id -> YES payoff per draw (bool). races: {name: Race} with .exchanges {party: ex} and .share_d."""
    n = anti.shape[0]
    col = {nm: i for i, nm in enumerate(names)}
    pay = {}
    d_seats = np.zeros(n, dtype=int)
    for nm, r in races.items():
        if nm not in col:
            continue
        a = anti[:, col[nm]]
        is_d = a & (rng.random(n) < r.share_d) if "I" in r.exchanges or r.share_d < 1 else a
        if r.rtype == "Senate":
            d_seats += is_d
        for party, ex in r.exchanges.items():
            pay[str(ex)] = {"R": ~a, "D": is_d, "I": a & ~is_d}.get(party, np.zeros(n, dtype=bool))
    if senate_ex:
        d = d_seats >= senate_need
        pay[str(senate_ex[0])], pay[str(senate_ex[1])] = d, ~d
    if house_ex and house_p is not None:
        # a such that mean(Phi((a + N) / sd)) over the draws equals the House-control price
        lo, hi = -40.0, 40.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if ndtr((mid + nat) / house_sd).mean() < house_p:
                lo = mid
            else:
                hi = mid
        d = rng.random(n) < ndtr((lo + nat) / house_sd)
        pay[str(house_ex[0])], pay[str(house_ex[1])] = d, ~d
    return pay


def target_draws(third_balance: float, n: int, rng: np.random.Generator, gain_lo: float = 0.0,
                 gain_hi: float = 0.3) -> np.ndarray:
    """The 3rd-place final balance: today's 3rd balance x (1 + the leaders' night gain ~ U(lo, hi))."""
    return third_balance * (1 + rng.uniform(gain_lo, gain_hi, n))


def leader_targets(leaders: dict, pay: dict, mids: dict, n: int, rng: np.random.Generator, rank: int = 3,
                   gain_lo: float = 0.0, gain_hi: float = 0.3) -> np.ndarray | None:
    """Per-draw target: the `rank`-th highest leader balance in each simulated outcome, from research/leader_exposure.py's
    estimates (balance + net YES shares x (payoff - SIG mid now), x the leaders' own night gain). None when fewer
    than `rank` leaders are modelled."""
    if len(leaders) < rank:
        return None
    rows = []
    for d in leaders.values():
        w = np.full(n, float(d["balance"]))
        for ex, q in (d.get("exposures") or {}).items():
            if str(ex) in pay and str(ex) in mids:
                w = w + float(q) * (pay[str(ex)].astype(float) - mids[str(ex)])
        rows.append(w)
    W = np.sort(np.stack(rows), axis=0)[::-1]
    return W[rank - 1] * (1 + rng.uniform(gain_lo, gain_hi, n))


def night_gain_draws(n: int, rng: np.random.Generator, mean: float = 0.15, sd: float = 0.25) -> np.ndarray:
    """Return on cash kept for the night's speed roles (call sweeps, lag snipes): positive edge, uncertain size."""
    return np.clip(rng.normal(mean, sd, n), -0.5, None)


def evaluate(cash: float, holdings: dict, pay: dict, T: np.ndarray, G: np.ndarray) -> np.ndarray:
    """Wealth per draw. holdings: ex -> {"side", "qty"}."""
    W = np.full(len(T), cash) * (1 + G)
    for ex, h in holdings.items():
        if str(ex) in pay:
            y = pay[str(ex)]
            W = W + h["qty"] * (y if h["side"] == "yes" else ~y)
    return W


def optimise(cash: float, holdings: dict, lots: list[Lot], pay: dict, T: np.ndarray, G: np.ndarray,
             step_frac: float = 0.04, max_frac: float = 1.0, min_gain: float = 0.0,
             max_steps: int = 200) -> dict:
    """Greedy chunks of step_frac x bankroll. Returns {"orders": [{ex, side, price (limit), qty, cost}], "p_before",
    "p_after", "ev_before", "ev_after", "spent"}. max_frac caps the share of the cash it may spend."""
    lots = [l for l in lots if str(l.ex) in pay and 0 < l.price < 1 and l.qty > 0]
    W = evaluate(cash, holdings, pay, T, G)
    # Objective: P(W >= T) first. While that is ~0 (far behind the bar) it cannot rank bets, so the tie-break is the
    # upper tail: the q-quantile of W - T with q = 1 - 0.35 W/T (a fair bet could make P = W/T, SIG's premium less), scaled
    # so one draw crossing the bar always outweighs any tail move. A smooth Phi((W - T) / s) surrogate was tried
    # first: it rejected every long shot at SIG's premium and spent nothing in 8 simulated nights at P = 0.
    q = float(np.clip(1 - 0.35 * np.median(W) / np.median(T), 0.5, 0.98))
    tie = 0.25 / len(T)

    def score(w):
        return (w >= T).mean() + tie * np.tanh(np.quantile(w - T, q) / np.median(T))

    p0, ev0 = float((W >= T).mean()), float(W.mean())
    if not lots:
        return {"orders": [], "p_before": p0, "p_after": p0, "ev_before": ev0, "ev_after": ev0, "spent": 0.0}
    bank = cash + sum(h["qty"] * 0.5 for h in holdings.values())
    chunk = max(500.0, step_frac * bank)
    # One active lot per exchange side (its cheapest level with shares left): the greedy always buys the cheaper level
    # first anyway, and the candidate matrix stays ~2 x 237 rows instead of every level.
    keys = sorted({(str(l.ex), l.side) for l in lots})
    queue = {k: sorted([l for l in lots if (str(l.ex), l.side) == k], key=lambda l: l.price) for k in keys}
    pos = {k: 0 for k in keys}
    Y = np.stack([pay[k[0]] if k[1] == "yes" else ~pay[k[0]] for k in keys]).astype(np.float32)
    left = {id(l): float(l.qty) for l in lots}
    budget = budget_total = cash * max_frac
    W0 = W.copy()
    spent = 0.0
    bought = defaultdict(float)          # index into lots -> shares
    lot_ix = {id(l): i for i, l in enumerate(lots)}
    G1 = (1 + G).astype(np.float32)
    T32 = T.astype(np.float32)
    for _ in range(max_steps):
        cur = score(W)
        act = [queue[k][pos[k]] if pos[k] < len(queue[k]) else None for k in keys]
        price = np.array([l.price if l else 1.0 for l in act])
        room = np.array([left[id(l)] * l.price if l else 0.0 for l in act])
        c = np.minimum(np.minimum(chunk, room), budget - spent)
        ok = c > 1
        if not ok.any():
            break
        sh = np.where(ok, c / price, 0.0)
        W32 = W.astype(np.float32)
        sc = np.full(len(keys), -1.0)
        for j in range(0, len(keys), 64):
            sl = slice(j, j + 64)
            cand = W32[None, :] + (sh[sl, None] * Y[sl]).astype(np.float32) - (c[sl, None] * G1[None, :]).astype(np.float32)
            d = cand - T32[None, :]
            sc[sl] = (d >= 0).mean(1) + tie * np.tanh(np.quantile(d, q, axis=1) / float(np.median(T)))
        sc[~ok] = -1
        i = int(np.argmax(sc))
        if sc[i] - cur <= min_gain:
            break
        W = W + sh[i] * Y[i] - c[i] * (1 + G)
        l = act[i]
        bought[lot_ix[id(l)]] += sh[i]
        left[id(l)] -= sh[i]
        if left[id(l)] * l.price <= 1:
            pos[keys[i]] += 1
        spent += c[i]
    # The greedy is myopic (far behind, small favourite chunks lift the tail a little each step while one concentrated
    # long shot would reach the bar). Compare with all-in-on-one-bet plans at 25-100% of the budget; keep the better.
    best_single = None
    for k in keys:
        for f in (0.25, 0.5, 0.75, 1.0):
            amt, sh_, cost_, top = budget_total * f, 0.0, 0.0, 0.0
            for l in queue[k]:
                take = min(l.qty, (amt - cost_) / l.price)
                if take <= 0:
                    break
                sh_, cost_, top = sh_ + take, cost_ + take * l.price, l.price
            if cost_ < 1:
                continue
            y = pay[k[0]] if k[1] == "yes" else ~pay[k[0]]
            w1 = W0 + sh_ * y - cost_ * (1 + G)
            sc1 = score(w1)
            if best_single is None or sc1 > best_single[0]:
                best_single = (sc1, k, sh_, cost_, top, w1)
    if best_single is not None and best_single[0] > score(W) + 1.0 / len(T):
        _, k, sh_, cost_, top, W = best_single
        return {"orders": [{"ex": k[0], "side": k[1], "price": top, "qty": int(sh_), "cost": cost_}],
                "p_before": p0, "p_after": float((W >= T).mean()), "ev_before": ev0, "ev_after": float(W.mean()),
                "spent": cost_, "plan": "single"}
    agg = {}
    for i, q in bought.items():
        l = lots[i]
        k = (str(l.ex), l.side)
        o = agg.setdefault(k, {"ex": str(l.ex), "side": l.side, "price": 0.0, "qty": 0.0, "cost": 0.0})
        o["price"] = max(o["price"], l.price)
        o["qty"] += q
        o["cost"] += q * l.price
    orders = [{**o, "qty": int(o["qty"])} for o in agg.values() if int(o["qty"]) > 0]
    return {"orders": sorted(orders, key=lambda o: -o["cost"]), "p_before": p0, "p_after": float((W >= T).mean()),
            "ev_before": ev0, "ev_after": float(W.mean()), "spent": spent, "plan": "greedy"}


def lots_from_books(books: dict, exchanges, max_levels: int = 8) -> list[Lot]:
    """books: ex -> {"bids": [(p, q)], "asks": [(p, q)]} (YES prices, BookCache shape). YES offers are the asks;
    NO offers are the YES bids (a YES bid at p = a NO offer at 1 - p)."""
    out = []
    for ex in exchanges:
        b = books.get(str(ex))
        if not b:
            continue
        for p, q in (b.get("asks") or [])[:max_levels]:
            out.append(Lot(str(ex), "yes", round(p, 3), q))
        for p, q in (b.get("bids") or [])[:max_levels]:
            out.append(Lot(str(ex), "no", round(1 - p, 3), q))
    return out

