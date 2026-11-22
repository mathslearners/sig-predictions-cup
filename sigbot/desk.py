"""News-desk race projections: the optional third call signal for election.py (--desk-calls). Read-only, public.

Sources (all public, no key, JSON, checked 27 Oct 2026):
  nbc   NBC News Decision Desk (its own projections, not AP's), the JSON behind nbcnews.com's results pages:
          https://www.nbcnews.com/firecracker/api/v2/national-results/{year}-elections/{senate|governor|house}-results
          (one file per office, 80-480 KB, the pages re-poll it every 15 s; `mapData` keyed by state postal code,
          "XX~P" for a partial-term special, and 4-digit state FIPS + district for the House, e.g. "4207" = PA-07).
          The 2026 national files 404 until NBC publishes them; until then the state files
          .../state-results/2026-elections/{state-slug}-results already exist (0.15-0.8 MB brotli each; races in
          stateRaceResults.discreteOffices [officeCode S / S2 (special) / G] and .house [district]) and are read
          instead, a few per cycle, only for states whose first polls have closed.
          A projection = candidate callStatus "P" with isWinner (race callStatusCode "P").
  nyt   New York Times results files (AP's calls): https://static01.nyt.com/elections-assets/pages/data/{date}/
          results-{senate|governor|house}.json (16-110 KB brotli, max-age 5 s). races[].outcome.won lists the winner
          (called_by ["ap"], winner_accepted_at = when the call landed). The 2026 files (date 2026-11-03) 404 until
          the NYT publishes them; 2024's (2024-11-05) are the format reference.
A race in a file with no winner is an explicit "not called" (used to retract a desk call that a desk withdrew); a race
missing from the file (or a failed fetch) says nothing.

Output of DeskFeed.calls(): {SIG race name: {source: party or None}} for every source read within `stale_s`.
Party: "D" / "R" / "I" (any independent / unaffiliated candidate) or None (no call, or a minor party).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Callable

import requests

from sigbot.races import STATES

NAMES = {v: k for k, v in STATES.items()}
FIPS = {"AL": 1, "AK": 2, "AZ": 4, "AR": 5, "CA": 6, "CO": 8, "CT": 9, "DE": 10, "FL": 12, "GA": 13, "HI": 15,
        "ID": 16, "IL": 17, "IN": 18, "IA": 19, "KS": 20, "KY": 21, "LA": 22, "ME": 23, "MD": 24, "MA": 25, "MI": 26,
        "MN": 27, "MS": 28, "MO": 29, "MT": 30, "NE": 31, "NV": 32, "NH": 33, "NJ": 34, "NM": 35, "NY": 36, "NC": 37,
        "ND": 38, "OH": 39, "OK": 40, "OR": 41, "PA": 42, "RI": 44, "SC": 45, "SD": 46, "TN": 47, "TX": 48, "UT": 49,
        "VT": 50, "VA": 51, "WA": 53, "WV": 54, "WI": 55, "WY": 56}
POSTAL = {f"{n:02d}": st for st, n in FIPS.items()}

NBC_NATIONAL = "https://www.nbcnews.com/firecracker/api/v2/national-results/{year}-elections/{office}-results"
NBC_STATE = "https://www.nbcnews.com/firecracker/api/v2/state-results/{year}-elections/{slug}-results"
NYT = "https://static01.nyt.com/elections-assets/pages/data/{date}/results-{office}.json"
OFFICES = ("senate", "governor", "house")
try:      # requests decodes brotli only with the brotli package; without it ask for gzip
    import brotli  # noqa: F401
    _ENC = "gzip, deflate, br"
except ImportError:
    _ENC = "gzip, deflate"
UA = {"User-Agent": "Mozilla/5.0 (sigbot election-night results reader)", "Accept-Encoding": _ENC}


def party(code: str | None) -> str | None:
    c = (code or "").strip().lower()
    if c in ("dem", "d", "democrat", "democratic"):
        return "D"
    if c in ("gop", "rep", "r", "republican"):
        return "R"
    if c in ("ind", "i", "una", "npa", "npp", "independent", "unaffiliated", "nonpartisan"):
        return "I"
    return None


def race_name(st: str, office: str, district: int | None = None) -> str | None:
    """SIG race names (sigbot.markets.parse_title): 'Maine Senate', 'Ohio Governor', 'PA-07 House'."""
    if office == "S":
        return f"{NAMES[st]} Senate" if st in NAMES else None
    if office == "G":
        return f"{NAMES[st]} Governor" if st in NAMES else None
    if office == "H" and district:
        return f"{st}-{int(district):02d} House"
    return None


class _Acc:
    """Per-source accumulator: a race listed twice (NBC lists ME-02's RCV rounds twice; a state with a regular and a
    special Senate race) is called if any entry is, None if no entry is, dropped if entries disagree. A special only
    stands in for the state's race when the state has no regular one (2026: FL, OH)."""

    def __init__(self):
        self.reg: dict[str, list] = defaultdict(list)
        self.spec: dict[str, list] = defaultdict(list)

    def add(self, name: str | None, winner: str | None, special: bool = False):
        if name:
            (self.spec if special else self.reg)[name].append(winner)

    def result(self) -> dict[str, str | None]:
        out = {}
        for name in set(self.reg) | set(self.spec):
            ws = self.reg.get(name) or self.spec.get(name)
            won = {w for w in ws if w is not None}
            if len(won) == 1:
                out[name] = won.pop()
            elif not won:
                out[name] = None
        return out


def _nbc_winner(cands: list[dict]) -> str | None:
    won = [c for c in cands or [] if c.get("isWinner") and (c.get("callStatus") or "").upper() == "P"]
    return party(won[0].get("party")) if len(won) == 1 else None


def parse_nbc_national(d: dict, office: str) -> dict[str, str | None]:
    """national-results/{year}-elections/{office}-results -> {race: party | None}."""
    acc = _Acc()
    for key, v in (d.get("mapData") or {}).items():
        t = (v or {}).get("tooltip") or {}
        w = _nbc_winner(t.get("candidates"))
        if office == "house":
            if len(key) == 4 and key.isdigit() and key[:2] in POSTAL:
                acc.add(race_name(POSTAL[key[:2]], "H", int(key[2:])), w)
            continue
        st, _, suffix = key.partition("~")
        if st in NAMES:
            acc.add(race_name(st, "S" if office == "senate" else "G"), w, special=bool(suffix))
    return acc.result()


def parse_nbc_state(d: dict) -> dict[str, str | None]:
    """state-results/{year}-elections/{slug}-results -> {race: party | None}."""
    st = d.get("stateAbbr")
    acc = _Acc()
    if st not in NAMES:
        return {}
    srr = d.get("stateRaceResults") or {}
    for r in srr.get("discreteOffices") or []:
        code = (r.get("officeCode") or "").upper()
        if code in ("S", "S2", "G"):
            acc.add(race_name(st, code[0]), _nbc_winner(r.get("candidates")), special=code == "S2")
    for r in srr.get("house") or []:
        if r.get("district"):
            acc.add(race_name(st, "H", r["district"]), _nbc_winner(r.get("candidates")))
    return acc.result()


def parse_nyt(d: dict) -> dict[str, str | None]:
    """results-{office}.json -> {race: party | None}."""
    acc = _Acc()
    for r in d.get("races") or []:
        ru = (r.get("reporting_units") or [{}])[0]
        st, office = ru.get("state_postal"), (r.get("office_id") or "").upper()
        kind = (r.get("type") or "").lower()
        if office not in ("S", "G", "H") or "primary" in kind or "runoff" in kind:
            continue        # 2024 Louisiana's November races are typed "Non-Partisan Election"
        out = r.get("outcome") or {}
        won = out.get("won") or []
        w = None
        if len(won) == 1:
            meta = (r.get("candidate_metadata") or {}).get(won[0]) or {}
            w = party((meta.get("party") or {}).get("nyt_id"))
        name = race_name(st, office, r.get("seat_number") if office == "H" else None)
        acc.add(name, w, special="special" in kind)
    return acc.result()


class DeskFeed:
    """Background poller. Every failure is counted and skipped; calls() only returns views read within stale_s."""

    def __init__(self, races: set[str], sources: tuple = ("nbc", "nyt"), year: int = 2026, date: str = "2026-11-03",
                 every: float = 10.0, timeout: float = 8.0, stale_s: float = 120.0, state_reads: int = 4,
                 due_states: Callable[[], list[str]] | None = None, log=print, session=None):
        self.races, self.sources = set(races), tuple(sources)
        self.year, self.date = year, date
        self.every, self.timeout, self.stale_s, self.state_reads = every, timeout, stale_s, state_reads
        self.due_states = due_states or (lambda: [])
        self.log = log
        self.s = session or requests.Session()
        self.views: dict[str, tuple[dict, float]] = {}   # "nbc:senate" / "nbc:PA" / "nyt:house" -> (calls, read at)
        self.national_missing: set[str] = set()          # NBC national offices that 404'd (state files used instead)
        self._rr = 0
        self.stats = defaultdict(int)
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.seen: dict[tuple, str | None] = {}          # (source, race) -> last party logged
        self.thread = None

    # ------------------------------------------------------------------ reads
    def _get(self, url: str) -> dict | None:
        """JSON, or None on a 404 (not published yet). Other failures raise (counted by the caller)."""
        r = self.s.get(url, headers=UA, timeout=self.timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def _put(self, key: str, calls: dict, now: float):
        calls = {k: v for k, v in calls.items() if k in self.races}
        with self.lock:
            self.views[key] = (calls, now)

    def poll_once(self):
        now = time.time
        if "nbc" in self.sources:
            for office in OFFICES:
                if office in self.national_missing and self._rr % 30:
                    continue        # re-check a missing national file every 30 cycles
                try:
                    d = self._get(NBC_NATIONAL.format(year=self.year, office=office))
                except Exception as e:  # noqa: BLE001
                    self._err("nbc_" + office, e)
                    continue
                if d is None:
                    self.national_missing.add(office)
                    self.stats["nbc_404"] += 1
                    continue
                self.national_missing.discard(office)
                self._put(f"nbc:{office}", parse_nbc_national(d, office), now())
            if self.national_missing:
                self._poll_nbc_states(now)
        if "nyt" in self.sources:
            for office in OFFICES:
                try:
                    d = self._get(NYT.format(date=self.date, office=office))
                except Exception as e:  # noqa: BLE001
                    self._err("nyt_" + office, e)
                    continue
                if d is None:
                    self.stats["nyt_404"] += 1
                    continue
                self._put(f"nyt:{office}", parse_nyt(d), now())
        self._rr += 1
        self._log_changes()

    def _poll_nbc_states(self, now):
        """National file missing: the state files of states whose first polls have closed, oldest read first."""
        due = [st for st in self.due_states() if st in NAMES]
        due.sort(key=lambda st: (self.views.get(f"nbc:{st}") or ({}, 0.0))[1])
        for st in due[:self.state_reads]:
            slug = NAMES[st].lower().replace(" ", "-")
            try:
                d = self._get(NBC_STATE.format(year=self.year, slug=slug))
            except Exception as e:  # noqa: BLE001
                self._err("nbc_state", e)
                continue
            if d is not None:
                self._put(f"nbc:{st}", parse_nbc_state(d), now())

    def _err(self, what: str, e: Exception):
        self.stats[what + "_errors"] += 1
        if self.stats[what + "_errors"] <= 3 or self.stats[what + "_errors"] % 100 == 0:
            self.log({"kind": "desk_error", "what": what, "n": self.stats[what + "_errors"],
                      "error": f"{type(e).__name__}: {str(e)[:160]}"})

    def _log_changes(self):
        for race, by in self.calls().items():
            for src, w in by.items():
                if self.seen.get((src, race), None) != w and (w is not None or (src, race) in self.seen):
                    self.log({"kind": "desk", "race": race, "source": src, "winner": w,
                              "was": self.seen.get((src, race))})
                self.seen[(src, race)] = w

    # ------------------------------------------------------------------ output
    def calls(self) -> dict[str, dict[str, str | None]]:
        """{race: {source: party | None}} from views younger than stale_s. Within one source a fresh call beats a
        fresh 'not called' from another of its files (NBC's national vs state file for the same race)."""
        now = time.time()
        out: dict[str, dict] = defaultdict(dict)
        with self.lock:
            views = list(self.views.items())
        for key, (calls, at) in views:
            if now - at > self.stale_s:
                continue
            src = key.split(":")[0]
            for race, w in calls.items():
                if w is not None or out[race].get(src) is None:
                    out[race][src] = w
        return dict(out)

    # ------------------------------------------------------------------ thread
    def _run(self):
        while not self.halt.is_set():
            t0 = time.time()
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 - never fatal
                self._err("poll", e)
            self.halt.wait(max(1.0, self.every - (time.time() - t0)))

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="desk")
        self.thread.start()

    def stop(self):
        self.halt.set()


def main():
    """python -m sigbot.desk [--year 2024 --date 2024-11-05]: one read-only poll of every source, per-source counts."""
    import argparse
    import json

    from sigbot.client import ROOT
    from sigbot.markets import parse_title
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--date", default="2026-11-03")
    ap.add_argument("--states", default="", help="comma-separated states whose NBC state files to read if the "
                                                 "national files 404 (default: every state with a SIG race)")
    a = ap.parse_args()
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    races = {parse_title(m["title"])[0] for m in u["markets"]}
    sts = [s for s in a.states.split(",") if s] or sorted(
        {r.split("-")[0] if r.endswith(" House") else STATES.get(r.rsplit(" ", 1)[0], "") for r in races} & set(NAMES))
    f = DeskFeed(races, year=a.year, date=a.date, state_reads=len(sts), due_states=lambda: sts, log=lambda e: None)
    t0 = time.time()
    f.poll_once()
    c = f.calls()
    for src in f.sources:
        seen = {r: v[src] for r, v in c.items() if src in v}
        print(f"{src}: {len(seen)} of {len(races)} SIG races listed, {sum(w is not None for w in seen.values())} "
              f"projected")
    dis = {r: v for r, v in c.items() if len({w for w in v.values() if w}) > 1}
    print(f"disagreements: {dis or 'none'}; NBC national files missing: {sorted(f.national_missing) or 'none'}; "
          f"errors/404s: {dict(f.stats) or 'none'}; {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
