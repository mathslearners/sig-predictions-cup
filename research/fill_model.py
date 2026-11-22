"""Fill-chance model for resting buys. READ-ONLY: places and cancels nothing.

  python research/fill_model.py --pull 24       # pull the last 24 h of tapes (Kalshi-priced exchanges), own fills and closed
                                       # orders into data/tapes/; resumable: re-run the same command until it says done
  python research/fill_model.py                 # newest tape cache -> labels, model, reports/fill_model_<UTC>.md
  python research/fill_model.py --no-orders     # skip own closed orders (the validation section)

The pull paces itself at --reads reads/min (default 8: the live maker + sniper use ~90 of the key's 100) and saves each
exchange's tape as it lands, so a run cut short (e.g. a 2 h background limit) loses nothing. 24 h of tapes took
~2 h of reads on 4 Oct.

Label without book history. Under price-time priority a sweep exhausts every bid at price >= q before it prints below q,
so a resting buy of side s at q would have been filled by every share that printed on s's bid side strictly under q
("through", a lower bound that holds whatever our queue place) and at best by every share printed at or under q
("touch", the upper bound for an order at the front of its level, e.g. a catcher jumped one tick above a wall).
Taker side on the tape: "YES" = a taker bought Yes, i.e. hit a resting No bid at 1 - price (checked against our own
fills in the validation section).

Samples: every --step minutes, every exchange with a fresh Kalshi mid (minute candles), both sides, a grid of distances d
under fair -> did the level get traded through within H minutes, and how many shares. Features are what the maker knows
when it places: distance, fair, SIG's own level vs fair, the side's recent activity and sweeps, hot flag, race type,
hour. Gradient boosting, trained on the first --train-frac of the window and scored on the rest (time split).
Markout: each through is valued at Kalshi fair 30 min after the first through print (adverse selection check).
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import bisect
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from daily_report import kalshi_history
from sigbot.client import ROOT, RateLimiter, SigClient
from sigbot.markets import TOURNAMENT_SLUG, parse_title

CACHE = ROOT / "data" / "tapes"
OUT = ROOT / "reports"
TICK = 0.005
DISTANCES = (0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.125, 0.15, 0.18, 0.22, 0.26, 0.30, 0.35, 0.40)
HORIZONS = (15, 60, 360)          # minutes
MAIN_H = 60
S_REF = 10_000                    # shares: E[min(V, S_REF)] is the expected fill of a catcher-sized order
LOOKBACK = 3 * 3600
MARKOUT = 30 * 60
FEATURES = ["d", "q", "fair_s", "roc", "gap_s", "dist_sig", "past_thru", "past_deep", "act_n", "act_sh",
            "hot", "rtype", "hour"]
RTYPES = {"House": 0, "Senate": 1, "Governor": 2, "Chamber": 3}


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def tick_floor(p: float) -> float:
    return round(math.floor(round(p / TICK, 6)) * TICK, 3)


class Fair:
    """Kalshi minute mids for one exchange; None when the last tight minute is older than max_age."""

    def __init__(self, series, max_age=600):
        self.t, self.v, self.max_age = series[0], series[1], max_age

    def __call__(self, t: float) -> float | None:
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 and t - self.t[i] <= self.max_age else None


def pull(a) -> bool:
    """Resumable pull into data/tapes/pull/: window fixed in manifest.json at the first run. True once complete (then
    data/tapes/tapes_<tag>.json, the same format research/tape_study.py writes, holds tapes + own fills)."""
    d = CACHE / "pull"
    (d / "ex").mkdir(parents=True, exist_ok=True)
    mf = d / "manifest.json"
    if mf.exists():
        man = json.loads(mf.read_text(encoding="utf-8"))
    else:
        end = datetime.now(timezone.utc).replace(microsecond=0)
        man = {"start": (end - timedelta(hours=a.pull)).isoformat(), "end": end.isoformat()}
        mf.write_text(json.dumps(man), encoding="utf-8")
    start, end = man["start"][:19] + "Z", man["end"][:19] + "Z"
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    exs = [str(e["id"]) for m in u["markets"] for e in m["exchanges"] if str(e["id"]) in u["kalshi"]]
    c = SigClient()
    c.read_limiter = RateLimiter(a.reads)
    c.write_limiter = RateLimiter(1)
    stop = datetime.now(timezone.utc) + timedelta(minutes=a.max_minutes)
    todo = [ex for ex in exs if not (d / "ex" / f"{ex}.json").exists()]
    print(f"window {start} -> {end}: {len(exs) - len(todo)}/{len(exs)} tapes cached", flush=True)
    for i, ex in enumerate(todo):
        if datetime.now(timezone.utc) > stop:
            print(f"stopping after {a.max_minutes:g} min: {len(todo) - i} tapes left, re-run to resume", flush=True)
            return False
        rows = list(c.paginate(f"/exchanges/{ex}/trades", limit=200, tournamentId=u["tournamentId"],
                               **{"from": start, "to": end}))
        (d / "ex" / f"{ex}.json").write_text(json.dumps(rows), encoding="utf-8")
        if i % 10 == 0:
            print(f"  {len(exs) - len(todo) + i + 1}/{len(exs)} tapes", flush=True)
    for name, path, kw, key in (("fills", f"/tournaments/{TOURNAMENT_SLUG}/portfolio/fills", {}, "filledAt"),
                                ("orders", "/orders", {"status": "closed", "tournamentId": u["tournamentId"]},
                                 "createdAt")):
        fp = d / f"{name}.json"
        if fp.exists():
            continue
        rows = []
        for r in c.paginate(path, limit=200, **kw):
            if r[key] < start[:19]:
                break
            if r[key] < end[:19]:
                rows.append(r)
        fp.write_text(json.dumps(rows), encoding="utf-8")
        print(f"  {len(rows)} own {name}", flush=True)
    tag = f"{start[:16].replace('-', '').replace(':', '')}Z_{end[:16].replace('-', '').replace(':', '')}Z"
    tapes = {ex: json.loads((d / "ex" / f"{ex}.json").read_text(encoding="utf-8")) for ex in exs}
    (CACHE / f"tapes_{tag}.json").write_text(json.dumps(
        {"start": man["start"], "end": man["end"], "tapes": tapes,
         "fills": json.loads((d / "fills.json").read_text(encoding="utf-8"))}), encoding="utf-8")
    (CACHE / f"orders_{tag}.json").write_text((d / "orders.json").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"done: data/tapes/tapes_{tag}.json ({sum(map(len, tapes.values())):,} trades)", flush=True)
    return True


def load(a):
    files = sorted(CACHE.glob("tapes_*.json"))
    if not files:
        raise SystemExit("no data/tapes/tapes_*.json: run python research/fill_model.py --pull 24 first")
    path = files[-1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    start, end = ts(raw["start"]), ts(raw["end"])
    tag = path.stem.removeprefix("tapes_")
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    kmap = u["kalshi"]
    kpath = CACHE / f"kalshi_{tag}.json"
    if kpath.exists():
        hist = json.loads(kpath.read_text(encoding="utf-8"))
    else:
        hist = kalshi_history({kmap[e] for e in raw["tapes"] if e in kmap}, int(start) - 3600, int(end) + 60)
        kpath.write_text(json.dumps(hist), encoding="utf-8")
    fair = {ex: Fair(hist[kmap[ex]]) for ex in raw["tapes"] if ex in kmap and kmap[ex] in hist}
    orders = None
    if not a.no_orders:
        opath = CACHE / f"orders_{tag}.json"
        if opath.exists():
            orders = json.loads(opath.read_text(encoding="utf-8"))
        else:
            c = SigClient()
            c.read_limiter = RateLimiter(a.reads)
            c.write_limiter = RateLimiter(1)
            orders = []
            for o in c.paginate("/orders", limit=200, status="closed", tournamentId=u["tournamentId"]):
                if ts(o["createdAt"]) < start:
                    break
                orders.append(o)
            opath.write_text(json.dumps(orders), encoding="utf-8")
    meta = {}
    hot = set(json.loads((ROOT / "config" / "hot_markets.json").read_text(encoding="utf-8"))["exchanges"])
    for m in u["markets"]:
        _, rt, party = parse_title(m["title"])
        for e in m["exchanges"]:
            meta[str(e["id"])] = {"title": m["title"], "rtype": RTYPES.get(rt, -1), "hot": int(str(e["id"]) in hot)}
    return raw, start, end, tag, fair, orders, meta


def prints_of(rows: list[dict]) -> dict[str, np.ndarray]:
    """Tape -> arrays sorted by time: t, resting side (0 = yes bid, 1 = no bid), resting price x, size, yes price."""
    t, rs, x, sz, py = [], [], [], [], []
    for r in rows:
        if r.get("price") is None or not r.get("size"):
            continue
        p = round(float(r["price"]), 4)
        taker_yes = str(r.get("side", "")).upper() == "YES"
        t.append(ts(r["createdAt"]))
        rs.append(1 if taker_yes else 0)          # a Yes taker hits a resting No bid (at 1 - p) and vice versa
        x.append(round(1 - p, 4) if taker_yes else p)
        sz.append(float(r["size"]))
        py.append(p)
    o = np.argsort(t, kind="stable")
    return {k: np.asarray(v, dtype=float)[o] for k, v in
            (("t", t), ("rs", rs), ("x", x), ("sz", sz), ("py", py))}


def build(raw, start, end, fair, meta, step_min) -> pd.DataFrame:
    rows = []
    t0s = np.arange(start + LOOKBACK, end - min(HORIZONS) * 60, step_min * 60)
    for ex, tape in raw["tapes"].items():
        if ex not in fair:
            continue
        P = prints_of(tape)
        if not len(P["t"]):
            P = {k: np.zeros(0) for k in ("t", "rs", "x", "sz", "py")}
        fx, m = fair[ex], meta.get(ex, {"rtype": -1, "hot": 0})
        for t0 in t0s:
            fy = fx(t0)
            if fy is None or not 0.01 <= fy <= 0.99:
                continue
            lo, hi = np.searchsorted(P["t"], [t0 - LOOKBACK, t0], side="right")
            hist = slice(lo, hi)
            act_n, act_sh = hi - lo, float(P["sz"][hist].sum())
            l60 = np.searchsorted(P["t"], t0 - 3600, side="right")
            w60 = slice(l60, hi)
            vw_yes = float((P["py"][w60] * P["sz"][w60]).sum() / P["sz"][w60].sum()) if hi > l60 else np.nan
            ends = {H: np.searchsorted(P["t"], t0 + H * 60, side="right") for H in HORIZONS}
            for si, s in enumerate(("yes", "no")):
                fs = fy if s == "yes" else 1 - fy
                sig_s = vw_yes if s == "yes" else 1 - vw_yes
                hm = P["rs"][hist] == si
                hx, hsz = P["x"][hist][hm], P["sz"][hist][hm]
                past_deep = float(hsz[hx <= fs - 0.10].sum())
                for d in DISTANCES:
                    q = tick_floor(fs - d)
                    if q < 0.005 or q > 0.995:
                        continue
                    r = {"ex": ex, "t0": t0, "side": s, "dn": d, "d": round(fs - q, 4), "q": q, "fair_s": fs,
                         "roc": (fs - q) / q, "gap_s": sig_s - fs, "dist_sig": sig_s - q,
                         "past_thru": math.log1p(float(hsz[hx < q - 1e-9].sum())),
                         "past_deep": math.log1p(past_deep), "act_n": act_n, "act_sh": math.log1p(act_sh),
                         "hot": m["hot"], "rtype": m["rtype"],
                         "hour": datetime.fromtimestamp(t0, timezone.utc).hour}
                    for H in HORIZONS:
                        e = ends[H]
                        if t0 + H * 60 > end:
                            r[f"thr{H}"] = r[f"vb{H}"] = r[f"vt{H}"] = np.nan
                            continue
                        wm = P["rs"][hi:e] == si
                        wx, wsz, wt = P["x"][hi:e][wm], P["sz"][hi:e][wm], P["t"][hi:e][wm]
                        b = wx < q - 1e-9
                        r[f"vb{H}"] = float(wsz[b].sum())
                        r[f"vt{H}"] = float(wsz[wx <= q + 1e-9].sum())
                        r[f"thr{H}"] = float(b.any())
                        if H == MAIN_H:
                            r["touch60"] = float((wx <= q + 1e-9).any())
                            if b.any():
                                t1 = float(wt[b][0])
                                f1 = fx(min(t1 + MARKOUT, end))
                                r["mark60"] = ((f1 if s == "yes" else 1 - f1) - q) if f1 is not None else np.nan
                            else:
                                r["mark60"] = np.nan
                    rows.append(r)
    return pd.DataFrame(rows)


def calib_table(y, p, n=10) -> list[tuple]:
    o = np.argsort(p)
    out = []
    for chunk in np.array_split(o, n):
        if len(chunk):
            out.append((float(p[chunk].mean()), float(y[chunk].mean()), len(chunk)))
    return out


def fit(df: pd.DataFrame, train_frac: float, md: list):
    """Through-classifier per horizon + Poisson regressor for E[min(V_through, S_REF)] at MAIN_H."""
    cut = df["t0"].min() + train_frac * (df["t0"].max() - df["t0"].min())
    models = {}
    for H in HORIZONS:
        sub = df[df[f"thr{H}"].notna()]
        tr, te = sub[sub.t0 < cut], sub[sub.t0 >= cut]
        if len(te) < 1000 or te[f"thr{H}"].nunique() < 2:
            md.append(f"- H = {H} min: too few test samples ({len(te)}), skipped")
            continue
        clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.06, max_leaf_nodes=31,
                                             min_samples_leaf=200, l2_regularization=1.0,
                                             categorical_features=[FEATURES.index("rtype")], random_state=0)
        clf.fit(tr[FEATURES], tr[f"thr{H}"])
        p = clf.predict_proba(te[FEATURES])[:, 1]
        y = te[f"thr{H}"].to_numpy()
        # Baseline: distance x fair bucket rates from the training half (what a hand-tuned ladder implicitly assumes).
        key = lambda f: f["d"].round(3).astype(str) + "|" + (f["fair_s"] * 10).astype(int).astype(str)  # noqa: E731
        rate = tr.groupby(key(tr))[f"thr{H}"].mean()
        pb = key(te).map(rate).fillna(tr[f"thr{H}"].mean()).clip(1e-4, 1 - 1e-4).to_numpy()
        lr = LogisticRegression(max_iter=500).fit(np.c_[tr["d"], tr["fair_s"]], tr[f"thr{H}"])
        pl = lr.predict_proba(np.c_[te["d"], te["fair_s"]])[:, 1]
        models[H] = {"clf": clf, "test": te.assign(p=p), "cut": cut}
        md.append(f"- **H = {H} min**: {len(tr):,} train / {len(te):,} test rows, base rate {y.mean():.3%} "
                  f"(train {tr[f'thr{H}'].mean():.3%}). AUC model **{roc_auc_score(y, p):.3f}** vs distance x fair "
                  f"buckets {roc_auc_score(y, pb):.3f} vs logistic(d, fair) {roc_auc_score(y, pl):.3f}; "
                  f"Brier {brier_score_loss(y, p):.4f} / {brier_score_loss(y, pb):.4f} / {brier_score_loss(y, pl):.4f}; "
                  f"log loss {log_loss(y, np.clip(p, 1e-6, 1 - 1e-6)):.4f} / {log_loss(y, pb):.4f} / "
                  f"{log_loss(y, np.clip(pl, 1e-6, 1 - 1e-6)):.4f}")
    if MAIN_H in models:
        sub = df[df[f"vb{MAIN_H}"].notna()]
        tr, te = sub[sub.t0 < cut], sub[sub.t0 >= cut]
        reg = HistGradientBoostingRegressor(loss="poisson", max_iter=300, learning_rate=0.06, min_samples_leaf=200,
                                            categorical_features=[FEATURES.index("rtype")], random_state=0)
        reg.fit(tr[FEATURES], np.minimum(tr[f"vb{MAIN_H}"], S_REF))
        ev = reg.predict(te[FEATURES])
        yv = np.minimum(te[f"vb{MAIN_H}"], S_REF).to_numpy()
        base = tr.groupby(tr["d"].round(3))[f"vb{MAIN_H}"].apply(lambda v: np.minimum(v, S_REF).mean())
        eb = te["d"].round(3).map(base).fillna(yv.mean()).to_numpy()
        corr = lambda u, v: float(np.corrcoef(u, v)[0, 1])  # noqa: E731
        md.append(f"- **E[min(V_through, {S_REF:,})] at {MAIN_H} min** (Poisson GBM): test mean actual {yv.mean():,.0f} "
                  f"predicted {ev.mean():,.0f}; correlation with actual {corr(ev, yv):.3f} vs by-distance baseline "
                  f"{corr(eb, yv):.3f}")
        models[MAIN_H]["reg"] = reg
        models[MAIN_H]["test"] = models[MAIN_H]["test"].assign(ev=ev)
    return models


def validate_orders(raw, orders, fair, md: list, start, end):
    """Own closed orders: did they fill within 15 min, vs the tape's through/touch label at their own price?"""
    fills_by_order = defaultdict(list)
    for f in raw["fills"]:
        fills_by_order[f["orderId"]].append(ts(f["filledAt"]))
    P = {ex: prints_of(rows) for ex, rows in raw["tapes"].items()}
    # Taker-side check: our resting fills should sit on the tape as prints whose inferred resting side is ours.
    agree = disagree = 0
    for f in raw["fills"]:
        ex = str(f["exchangeId"])
        if ex not in P or not len(P[ex]["t"]):
            continue
        t = ts(f["filledAt"])
        i0, i1 = np.searchsorted(P[ex]["t"], [t - 0.6, t + 0.6])
        if i1 == i0:
            continue
        sides = set(P[ex]["rs"][i0:i1].astype(int))
        mine = 0 if f["side"] == "yes" else 1
        if sides == {mine}:
            agree += 1
        elif mine not in sides:
            disagree += 1
    md.append(f"- Taker-side convention: of our fills with a print within 0.6 s on the tape, {agree:,} sit only on prints "
              f"whose inferred resting side is ours, {disagree:,} only on the other side (our takes: snipes, crossing "
              f"exits). A majority 'agree' confirms side = taker side.")
    W = 15 * 60
    stat = defaultdict(lambda: np.zeros(5))   # n, filled, through, touch, filled&through
    for o in orders:
        ex = str(o["exchangeId"])
        t0 = ts(o["createdAt"])
        if ex not in fair or o.get("action") != "buy" or t0 + W > end or ex not in P:
            continue
        fy = fair[ex](t0)
        q = float(o.get("priceLimit") or 0)
        if fy is None or q <= 0:
            continue
        si = 0 if o["side"] == "yes" else 1
        fs = fy if si == 0 else 1 - fy
        d = fs - q
        bk = ("<0" if d < 0 else "0-0.03" if d < 0.03 else "0.03-0.06" if d < 0.06 else "0.06-0.10" if d < 0.10
              else "0.10-0.15" if d < 0.15 else "0.15-0.25" if d < 0.25 else ">=0.25")
        pp = P[ex]
        i0, i1 = np.searchsorted(pp["t"], [t0, t0 + W])
        m = pp["rs"][i0:i1] == si
        x = pp["x"][i0:i1][m]
        thr, tch = float((x < q - 1e-9).any()), float((x <= q + 1e-9).any())
        filled = float(any(t0 <= t <= t0 + W for t in fills_by_order.get(o["id"], [])))
        stat[bk] += (1, filled, thr, tch, filled * thr)
    md.append("\n| distance under fair | orders | filled in 15 min | tape through | tape touch | filled when through |")
    md.append("|---|---:|---:|---:|---:|---:|")
    for bk in ("<0", "0-0.03", "0.03-0.06", "0.06-0.10", "0.10-0.15", "0.15-0.25", ">=0.25"):
        if bk in stat:
            n, f_, th, tc, ft = stat[bk]
            md.append(f"| {bk} | {n:,.0f} | {f_ / n:.1%} | {th / n:.1%} | {tc / n:.1%} | "
                      f"{(ft / th if th else float('nan')):.0%} |")
    md.append("\n'Filled when through' under 100% = orders cancelled/replaced before the sweep, or fills cut short by cash "
              "(SIG checks cash at match time).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--step", type=float, default=10, help="minutes between sample start times")
    ap.add_argument("--train-frac", type=float, default=0.65)
    ap.add_argument("--no-orders", action="store_true")
    ap.add_argument("--reads", type=int, default=8)
    ap.add_argument("--pull", type=float, help="pull this many hours of tapes (resumable), then stop")
    ap.add_argument("--max-minutes", type=float, default=110, help="--pull: stop after this long (re-run to resume)")
    a = ap.parse_args()
    if a.pull:
        pull(a)
        return

    raw, start, end, tag, fair, orders, meta = load(a)
    df = build(raw, start, end, fair, meta, a.step)
    df.to_pickle(CACHE / f"fill_samples_{tag}.pkl")
    span = f"{datetime.fromtimestamp(start, timezone.utc):%m-%d %H:%MZ} -> {datetime.fromtimestamp(end, timezone.utc):%m-%d %H:%MZ}"
    md = [f"# Fill-chance model ({span})", "",
          f"{sum(map(len, raw['tapes'].values())):,} prints on {len(raw['tapes'])} exchanges, {len(fair)} with Kalshi "
          f"minute fair; {len(df):,} samples ({df.groupby(['ex', 't0', 'side']).ngroups:,} exchange-side-times x "
          f"{len(DISTANCES)} distances), start every {a.step:g} min.", "", "## Model quality (time split)", ""]
    models = fit(df, a.train_frac, md)
    if MAIN_H in models:
        te = models[MAIN_H]["test"]
        md += ["", f"### Calibration, through within {MAIN_H} min (test)", "", "| predicted | actual | n |", "|---:|---:|---:|"]
        md += [f"| {p:.3%} | {y:.3%} | {n:,} |" for p, y, n in calib_table(te[f"thr{MAIN_H}"].to_numpy(), te["p"].to_numpy())]
        sm = te.sample(min(len(te), 40000), random_state=0)
        pi = permutation_importance(models[MAIN_H]["clf"], sm[FEATURES], sm[f"thr{MAIN_H}"], scoring="roc_auc",
                                    n_repeats=3, random_state=0)
        md += ["", "### What drives fills (permutation importance, AUC drop)", ""]
        md += [f"- `{FEATURES[i]}` {pi.importances_mean[i]:.4f}" for i in np.argsort(-pi.importances_mean)]

        # Fill rate, size and markout by distance, all data (the curves the ladder/catcher levels sit on).
        g = df[df[f"thr{MAIN_H}"].notna()].copy()
        g["fs_b"] = pd.cut(g["fair_s"], [0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0])
        g["fill"] = np.minimum(g[f"vb{MAIN_H}"], S_REF)
        md += ["", f"## Through within {MAIN_H} min by distance (all {len(g):,} samples)", "",
               f"Edge = Kalshi fair 30 min after the first through print minus our price (per filled share); "
               f"edge/order-hour = E[min(V, {S_REF // 1000}k)] x edge.", "",
               "| d | P(through) | P(touch) | E[fill] | median V given through | edge/share at fill+30m | edge/order-hour | hot P(through) |",
               "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for d, x in g.groupby("dn"):
            th = x[x[f"thr{MAIN_H}"] == 1]
            mk = th["mark60"].mean()
            hotp = x[x.hot == 1][f"thr{MAIN_H}"].mean()
            md.append(f"| {d:.3f} | {x[f'thr{MAIN_H}'].mean():.2%} | {x['touch60'].mean():.2%} | {x['fill'].mean():,.0f} | "
                      f"{th[f'vb{MAIN_H}'].median() if len(th) else 0:,.0f} | {mk:+.3f} | "
                      f"{(x['fill'] * x['mark60'].fillna(0)).mean():,.1f} | {hotp:.2%} |")
        md += ["", "### P(through 60 min) by distance x fair of the side", "",
               "| d \\ fair_s | " + " | ".join(str(c) for c in g["fs_b"].cat.categories) + " |",
               "|---|" + "---:|" * len(g["fs_b"].cat.categories)]
        pv = g.pivot_table(index="dn", columns="fs_b", values=f"thr{MAIN_H}", aggfunc="mean", observed=False)
        for d, row in pv.iterrows():
            md.append(f"| {d:.3f} | " + " | ".join("" if pd.isna(v) else f"{v:.1%}" for v in row) + " |")
        md += ["", "### Edge per order-hour by distance x fair of the side (catcher-sized, 10k shares)", "",
               "| d \\ fair_s | " + " | ".join(str(c) for c in g["fs_b"].cat.categories) + " |",
               "|---|" + "---:|" * len(g["fs_b"].cat.categories)]
        g["eh"] = g["fill"] * g["mark60"].fillna(0)
        pv = g.pivot_table(index="dn", columns="fs_b", values="eh", aggfunc="mean", observed=False)
        for d, row in pv.iterrows():
            md.append(f"| {d:.3f} | " + " | ".join("" if pd.isna(v) else f"{v:,.1f}" for v in row) + " |")

        # Exchanges ranked by model-expected edge at catcher distances in the latest hour (candidate hot list).
        last = df[df.t0 >= df.t0.max() - 3600]
        last = last[last["dn"].between(0.10, 0.22)]
        if "reg" in models[MAIN_H] and len(last):
            last = last.assign(ev=models[MAIN_H]["reg"].predict(last[FEATURES]),
                               p=models[MAIN_H]["clf"].predict_proba(last[FEATURES])[:, 1])
            last = last.assign(eh=last["ev"] * last["d"])
            rank = last.groupby("ex")["eh"].mean().sort_values(ascending=False)
            hot = {ex for ex, m in meta.items() if m["hot"]}
            top = list(rank.index[:25])
            md += ["", "## Where catchers should sit (model, latest hour, d 0.10-0.22)", "",
                   f"Top 25 by predicted edge/order-hour (E[fill] x d); {len(set(top) & hot)} of them are in "
                   f"config/hot_markets.json, {len(hot - set(top))} hot exchanges are not.", "",
                   "| exchange | E[edge]/order-hour | P(through 60m) | hot | title |", "|---|---:|---:|---|---|"]
            for ex in top:
                x = last[last.ex == ex]
                md.append(f"| {ex} | {rank[ex]:,.1f} | {x['p'].mean():.1%} | {'yes' if ex in hot else ''} | "
                          f"{meta.get(ex, {}).get('title', '')[:60]} |")
    if orders is not None:
        md += ["", "## Validation against our own closed orders", ""]
        validate_orders(raw, orders, fair, md, start, end)
    OUT.mkdir(exist_ok=True)
    out = OUT / f"fill_model_{datetime.now(timezone.utc):%Y%m%dT%H%MZ}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
