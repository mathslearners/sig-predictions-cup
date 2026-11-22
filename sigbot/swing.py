"""Election-night swing model: a national swing, state effects and race noise, updated from race calls, outside-market
moves and partial vote counts. Pure maths: no API calls.

Latent anti-Republican margin of race i, in points:  m_i = mu_i + N + S_state(i) + R_i
  N ~ N(0, sn^2)   national environment (shared by every race)
  S ~ N(0, ss^2)   per state (shared by its Senate, Governor and House races)
  R_i ~ N(0, sr^2) race-specific (sr by race type)
mu_i is set from the pre-results price: P(anti-R wins) = Phi(mu_i / sd_i), sd_i^2 = sn^2 + ss^2 + sr_i^2, so the prior
reproduces every market exactly and the only modelling choice is how the variance splits (sn calibrates against the
Senate-control market, see calibrate_sn).

Given (N, S) races are independent and given N states are independent, so the posterior is exact on an N x S grid
per state: no Monte Carlo weight collapse however many races are called.

Observations
  call    race decided: likelihood Phi(x / sr) (x = mu + N + S) for an anti-R win, softened by eps
  market  an outside price moved to p: virtual evidence lambda per race, solved jointly so every constrained race's
          posterior equals its price (I-projection). Ten markets moving on the same news are one piece of evidence,
          not ten.
  count   partial count: anti-R margin x (points) with fraction f of the expected vote in, read as m_i + noise with
          sd tau0 * sqrt((1 - f) / f). Early counts lean one way by method of voting (the "red/blue mirage"), so
          counts are only as good as tau0; calls and markets are the robust inputs.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy.special import ndtr, ndtri

SR = {"Senate": 3.5, "Governor": 4.0, "House": 5.0}

# All polls closed in the state, hours after 00:00 UTC on 4 Nov 2026 (19:00 EST). A state whose polls close at
# different times counts as closed at its LAST close (Green Papers G24/G26 closing tables, 270towin, checked 18 Oct):
# MI 2.0 (Central-time UP counties 8 pm CT), IA 2.0 (8 pm CT since 2021), NE 2.0 (closes at once: 8 pm CT = 7 pm MT),
# ND 2.0 (270towin: Mountain part 9 pm ET; by statute towns may stay open to 9 pm local, but no SIG market is in ND),
# TX/KS/SD 2.0 (Mountain-time counties 7 pm MT), ID/OR 4.0 (Pacific parts), AK 6.0 (Aleutians).
POLL_CLOSE_H = {
    **dict.fromkeys(["GA", "IN", "KY", "SC", "VT", "VA"], 0.0),
    **dict.fromkeys(["NC", "OH", "WV"], 0.5),
    **dict.fromkeys(["AL", "CT", "DE", "DC", "FL", "IL", "ME", "MD", "MA", "MS", "MO", "NH", "NJ", "OK", "PA",
                     "RI", "TN"], 1.0),
    "AR": 1.5,
    **dict.fromkeys(["AZ", "CO", "IA", "KS", "LA", "MI", "MN", "NE", "NM", "NY", "ND", "SD", "TX", "WI", "WY"], 2.0),
    **dict.fromkeys(["MT", "NV", "UT"], 3.0),
    **dict.fromkeys(["CA", "ID", "OR", "WA"], 4.0),
    "HI": 5.0, "AK": 6.0,
}
# First polls closed, where earlier than the last close: results start flowing then (Eastern IN/KY at 6 pm EST,
# peninsular FL, Eastern MI, the Central parts of TX/KS/SD/ND, southern ID, Mountain-time OR, AK outside the
# Aleutians), so resting orders are cancelled before this (night.py risk_off), while calls wait for POLL_CLOSE_H.
POLL_FIRST_H = {"IN": -1.0, "KY": -1.0, "FL": 0.0, "MI": 1.0, "TX": 1.0, "KS": 1.0, "SD": 1.0, "ND": 1.0,
                "ID": 3.0, "OR": 3.0, "AK": 5.0}


@dataclass
class Race:
    name: str                 # "Maine Senate", "VA-01 House"
    state: str                # "ME"
    rtype: str                # Senate / Governor / House
    p: float                  # prior P(anti-Republican candidate wins)
    share_d: float = 1.0      # P(the anti-R winner is the Democrat): < 1 where an Independent is the challenger
    exchanges: dict = field(default_factory=dict)   # party -> SIG exchange id


class SwingModel:
    def __init__(self, races: list[Race], sn: float = 3.0, ss: float = 3.0, sr: dict | None = None,
                 n_grid: int = 161, s_grid: int = 61, eps: float = 1e-3, tau0: float = 8.0):
        self.races = {r.name: r for r in races}
        self.sn, self.ss, self.sr = sn, ss, {**SR, **(sr or {})}
        self.eps, self.tau0 = eps, tau0
        z = np.linspace(-5, 5, n_grid)
        self.N, self.wN = z * sn, np.exp(-z * z / 2) / np.exp(-z * z / 2).sum()
        z = np.linspace(-5, 5, s_grid)
        self.S, self.wS = z * ss, np.exp(-z * z / 2) / np.exp(-z * z / 2).sum()
        self.mu, self.sri = {}, {}
        self.by_state = defaultdict(list)
        for r in races:
            sri = self.sr[r.rtype]
            sd = math.sqrt(sn * sn + ss * ss + sri * sri)
            self.mu[r.name] = sd * float(ndtri(min(max(r.p, 1e-3), 1 - 1e-3)))
            self.sri[r.name] = sri
            self.by_state[r.state].append(r.name)
        self.obs: dict[str, tuple] = {}
        self.lam: dict[str, float] = {}

    # ------------------------------------------------------------------ observations
    def call(self, name: str, anti_r_won: bool):
        self.obs[name] = ("call", bool(anti_r_won))

    def market(self, name: str, p: float):
        self.obs[name] = ("market", min(max(p, 1e-3), 1 - 1e-3))
        self.lam.setdefault(name, 1.0)

    def count(self, name: str, margin: float, frac: float):
        self.obs[name] = ("count", float(margin), min(max(frac, 0.01), 1.0))

    def clear(self):
        self.obs, self.lam = {}, {}

    # ------------------------------------------------------------------ grid pieces
    def _x(self, name: str) -> np.ndarray:
        return self.mu[name] + self.N[:, None] + self.S[None, :]

    def _phi(self, name: str) -> np.ndarray:
        c = self.__dict__.setdefault("_phic", {})   # mu, N and S never change after __init__
        if name not in c:
            c[name] = ndtr(self._x(name) / self.sri[name])
        return c[name]

    def _tau(self, frac: float) -> float:
        return self.tau0 * math.sqrt((1 - frac) / frac) if frac < 1 else 1e-3

    def _lik(self, name: str) -> np.ndarray:
        o = self.obs[name]
        if o[0] == "call":
            phi = self._phi(name)
            won = phi if o[1] else 1 - phi
            return (1 - self.eps) * won + self.eps * (1 - won)
        if o[0] == "market":
            phi = self._phi(name)
            return self.lam[name] * phi + (1 - phi)
        s = math.sqrt(self.sri[name] ** 2 + self._tau(o[2]) ** 2)
        return np.exp(-0.5 * ((o[1] - self._x(name)) / s) ** 2)

    def _own(self, name: str) -> np.ndarray:
        """P(anti-R wins | N, S, this race's own observation)."""
        o = self.obs.get(name)
        if o is None:
            return self._phi(name)
        if o[0] == "call":
            return np.full((len(self.N), len(self.S)), 1 - self.eps if o[1] else self.eps)
        if o[0] == "market":
            phi = self._phi(name)
            return self.lam[name] * phi / (self.lam[name] * phi + 1 - phi)
        sr2, t2 = self.sri[name] ** 2, self._tau(o[2]) ** 2
        a = self._x(name)
        k = sr2 / (sr2 + t2)
        return ndtr((a + k * (o[1] - a)) / math.sqrt(max((1 - k) * sr2, 1e-9)))

    def _tables(self):
        """(per-state S weights given N (nN x nS, rows sum to 1), posterior over N). Also sets self.log_ev, the log
        evidence of the calls and counts (markets excluded: virtual evidence is not a likelihood), which weighs the
        national-swing sizes against each other in SwingMixture."""
        logz = np.log(self.wN)
        loge = np.log(self.wN)
        W = {}
        for st, names in self.by_state.items():
            L = np.ones((len(self.N), len(self.S)))
            Le = None
            for n in names:
                if n in self.obs:
                    lk = self._lik(n)
                    L = L * lk
                    if self.obs[n][0] != "market":
                        Le = lk if Le is None else Le * lk
            w = L * self.wS[None, :]
            z = w.sum(1)
            W[st] = w / np.maximum(z[:, None], 1e-300)
            logz = logz + np.log(np.maximum(z, 1e-300))
            if Le is not None:
                loge = loge + np.log(np.maximum((Le * self.wS[None, :]).sum(1), 1e-300))
        self.log_ev = float(loge.max() + np.log(np.exp(loge - loge.max()).sum()))
        post = np.exp(logz - logz.max())
        return W, post / post.sum()

    def _probs(self) -> tuple[dict, np.ndarray]:
        W, postN = self._tables()
        out = {}
        for st, names in self.by_state.items():
            for n in names:
                out[n] = float(postN @ (W[st] * self._own(n)).sum(1))
        return out, postN

    # ------------------------------------------------------------------ results
    def posterior(self, iters: int = 60, tol: float = 5e-4) -> dict:
        """{"p": {race: P(anti-R wins)}, "N": (mean, sd) of the national swing in points, "fit": max market error}."""
        # Damped, backtracking fit of the market lambdas. The plain update (every lambda times its full odds ratio at
        # once) diverged on the night of 4 Nov: ~50 markets all above the prior move N together, so the joint step
        # overshot and N flipped between +10 and -11 points for good (sn 2-6 fit errors 0.79-0.97, an R wave read
        # out of a D wave). Now each step is the odds ratio ** a; a step that raises the summed log-odds error is
        # undone and a halved, a good one lets a grow back to 1 (sn 1.5-6 all fit to ~0.002 on the 4 Nov 02Z data).
        mk = [n for n, o in self.obs.items() if o[0] == "market"]
        p, postN = self._probs()

        def lg(x):
            x = min(max(x, 1e-6), 1 - 1e-6)
            return math.log(x / (1 - x))

        def merit(pp):
            return sum((lg(self.obs[n][1]) - lg(pp[n])) ** 2 for n in mk)

        def max_err(pp):
            return max((abs(self.obs[n][1] - pp[n]) for n in mk), default=0.0)

        err, mer, a = max_err(p), merit(p), 1.0
        for _ in range(iters if mk else 0):
            if err < tol:
                break
            old = dict(self.lam)
            for n in mk:
                t, q = self.obs[n][1], min(max(p[n], 1e-6), 1 - 1e-6)
                self.lam[n] *= ((t / (1 - t)) / (q / (1 - q))) ** a
            p2, post2 = self._probs()
            m2 = merit(p2)
            if m2 > mer:
                self.lam = old
                a *= 0.5
                continue
            p, postN, mer, err = p2, post2, m2, max_err(p2)
            a = min(1.0, a * 1.5)
        m = float(postN @ self.N)
        return {"p": p, "N": (m, float(math.sqrt(max(postN @ (self.N - m) ** 2, 0.0)))), "fit": err}

    def sample(self, n: int, rng: np.random.Generator | None = None) -> tuple[list[str], np.ndarray, np.ndarray]:
        """Joint outcome draws from the posterior (call posterior() first when market observations are set, so their
        lambdas are fitted): (race names, anti-R wins as an n x races bool matrix, national swing per draw in points).
        Draws N from its posterior, then each state's S given N, then each race given (N, S) and its own observation,
        so called races keep their result and correlated open races move together."""
        rng = rng or np.random.default_rng()
        W, postN = self._tables()
        names = list(self.races)
        col = {nm: i for i, nm in enumerate(names)}
        iN = rng.choice(len(self.N), size=n, p=postN)
        out = np.zeros((n, len(names)), dtype=bool)
        for st, ns in self.by_state.items():
            cw = np.cumsum(W[st], axis=1)
            iS = np.minimum((cw[iN] < rng.random(n)[:, None]).sum(1), len(self.S) - 1)
            for nm in ns:
                out[:, col[nm]] = rng.random(n) < self._own(nm)[iN, iS]
        return names, out, self.N[iN]

    def seats(self, names: list[str], need: int) -> float:
        """P(the Democrat wins at least `need` of `names`), with the races' joint dependence through N and S."""
        W, postN = self._tables()
        total = np.ones((len(self.N), 1))
        by = defaultdict(list)
        for n in names:
            by[self.races[n].state].append(n)
        for st, ns in by.items():
            dist = np.ones((len(self.N), len(self.S), 1))       # P(k D wins in this state | N, S)
            for n in ns:
                q = self._own(n) * self.races[n].share_d
                nxt = np.zeros(dist.shape[:2] + (dist.shape[2] + 1,))
                nxt[..., :-1] += dist * (1 - q)[..., None]
                nxt[..., 1:] += dist * q[..., None]
                dist = nxt
            ds = (W[st][..., None] * dist).sum(1)                  # given N only
            nt = np.zeros((len(self.N), total.shape[1] + ds.shape[1] - 1))
            for j in range(ds.shape[1]):
                nt[:, j:j + total.shape[1]] += total * ds[:, j:j + 1]
            total = nt
        return float(postN @ total[:, need:].sum(1)) if need < total.shape[1] else 0.0


class SwingMixture:
    """SwingModel averaged over national-swing sizes, weighted by how well each explains the calls and counts so far.
    The market prices identify the swing size only weakly (4 Oct: Senate control 0.60-0.69 for sd 0.5-8 pts) and a
    model that assumes too much correlation overreacts late in the night (rehearsal: -1.3% vs frozen prices with sd 4
    against a true 2), so the night's own results decide."""

    def __init__(self, races: list[Race], sns=(1.5, 2, 3, 4, 6), prior=None, **kw):
        self.models = [SwingModel(races, sn=s, **kw) for s in sns]
        self.sns = list(sns)
        self.prior = np.asarray(prior if prior is not None else [1.0] * len(sns), dtype=float)
        self.prior = self.prior / self.prior.sum()
        self.races = self.models[0].races
        self.w = self.prior.copy()

    @property
    def obs(self):
        return self.models[0].obs

    def call(self, name, anti_r_won):
        for m in self.models:
            m.call(name, anti_r_won)

    def market(self, name, p):
        for m in self.models:
            m.market(name, p)

    def count(self, name, margin, frac):
        for m in self.models:
            m.count(name, margin, frac)

    def clear(self):
        for m in self.models:
            m.clear()

    def posterior(self, **kw) -> dict:
        posts = [m.posterior(**kw) for m in self.models]
        le = np.array([m.log_ev for m in self.models])
        w = self.prior * np.exp(le - le.max())
        self.w = w / w.sum()
        p = {n: float(sum(wi * po["p"][n] for wi, po in zip(self.w, posts))) for n in posts[0]["p"]}
        mean = float(sum(wi * po["N"][0] for wi, po in zip(self.w, posts)))
        var = float(sum(wi * (po["N"][1] ** 2 + (po["N"][0] - mean) ** 2) for wi, po in zip(self.w, posts)))
        return {"p": p, "N": (mean, math.sqrt(var)), "fit": max(po["fit"] for po in posts),
                "w": {s: round(float(x), 3) for s, x in zip(self.sns, self.w)}}

    def sample(self, n: int, rng: np.random.Generator | None = None):
        """Joint draws from the mixture (weights of the last posterior() call): each draw picks a swing size first."""
        rng = rng or np.random.default_rng()
        k = rng.multinomial(n, self.w)
        parts = [m.sample(int(c), rng) for m, c in zip(self.models, k) if c]
        names = parts[0][0]
        out = np.concatenate([p[1] for p in parts])
        nat = np.concatenate([p[2] for p in parts])
        perm = rng.permutation(len(out))
        return names, out[perm], nat[perm]

    def seats(self, names, need) -> float:
        """Uses the weights of the last posterior() call."""
        return float(sum(wi * m.seats(names, need) for wi, m in zip(self.w, self.models)))


def calibrate_sn(races: list[Race], names: list[str], need: int, target: float, ss: float = 3.0,
                 grid=(0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 5, 6, 7, 8)) -> tuple[float, list]:
    """National-swing sd whose prior P(Senate control) best matches the market's `target`; (sn, [(sn, P)...])."""
    rows = [(sn, SwingModel(races, sn=sn, ss=ss).seats(names, need)) for sn in grid]
    return min(rows, key=lambda r: abs(r[1] - target))[0], rows


def rehearse(races: list[Race], runs: int, model, true_sn: float, ss: float = 3.0,
             senate: tuple[list, int] | None = None, seed: int = 0) -> list[dict]:
    """Simulated election nights: truth drawn from the prior with national sd `true_sn` (each race's price calibrated
    under it), races called as polls close, sooner the wider the margin (>8 pts at close, 4-8 +1 h, 2-4 +3 h, closer
    +6 h). At each checkpoint, Brier score on the races not yet called: the frozen pre-results prices vs `model`
    (SwingModel or SwingMixture) updated on the calls so far."""
    rng = np.random.default_rng(seed)
    truth = SwingModel(races, sn=true_sn, ss=ss)
    names = list(truth.races)
    states = sorted({r.state for r in races})
    checkpoints = (0.25, 0.75, 1.25, 2.25, 3.25, 4.25, 6.5)
    acc = defaultdict(lambda: np.zeros(6))      # open races, Brier prior, Brier model, runs, senate err prior, model
    model.clear()
    prior_sen = None
    if senate:
        model.posterior()
        prior_sen = model.seats(*senate)
    for _ in range(runs):
        N = rng.normal(0, true_sn)
        S = dict(zip(states, rng.normal(0, ss, len(states))))
        m = {n: truth.mu[n] + N + S[truth.races[n].state] + rng.normal(0, truth.sri[n]) for n in names}
        call_at = {}
        for n in names:
            a = abs(m[n])
            call_at[n] = POLL_CLOSE_H.get(truth.races[n].state, 2.0) + (0 if a > 8 else 1 if a > 4 else 3 if a > 2 else 6)
        if senate:
            sen_names, need = senate
            won_sen = sum(m[n] > 0 and rng.random() < truth.races[n].share_d for n in sen_names) >= need
        for cp in checkpoints:
            model.clear()
            for n in names:
                if call_at[n] <= cp:
                    model.call(n, m[n] > 0)
            open_ = [n for n in names if call_at[n] > cp]
            if not open_:
                continue
            post = model.posterior()["p"]
            a = acc[cp]
            a[0] += len(open_)
            a[1] += sum((truth.races[n].p - (m[n] > 0)) ** 2 for n in open_)
            a[2] += sum((post[n] - (m[n] > 0)) ** 2 for n in open_)
            a[3] += 1
            if senate:
                a[4] += (prior_sen - won_sen) ** 2
                a[5] += (model.seats(*senate) - won_sen) ** 2
    out = []
    for cp in checkpoints:
        a = acc[cp]
        if a[3]:
            out.append({"hours": cp, "open": a[0] / a[3], "brier_prior": a[1] / a[0], "brier_model": a[2] / a[0],
                        "senate_prior": a[4] / a[3] if senate else None, "senate_model": a[5] / a[3] if senate else None})
    return out
