"""Election-night race state machine and automatic call detector (no human input needed).

States per race: PRE (polls open) -> CLOSED (all polls in the state closed, POLL_CLOSE_H) -> CALLED(winner).
A call comes from the outside venues, which react to AP / Decision Desk calls and to counts within seconds:
  * both Kalshi and Polymarket price one party >= hi (0.97) for >= hold_s (60 s), or
  * one venue >= hard (0.99) while the other is >= hard_other (0.95), also for hold_s;
  * only one venue quoting (the ~20 House races Kalshi doesn't list, or the other venue stale): >= hard for
    single_hold_s (300 s), confidence capped at single_conf (0.985) so the engine sizes it as an uncertain call;
  * a contested race (leader < 0.80 at the close) needs 300 s in its first 30 minutes (see CallDetector);
  * optional (election.py --desk-calls, sigbot/desk.py): a news desk (NBC Decision Desk, AP via the NYT) has projected
    the race and one venue prices that party >= desk_venue (0.95; desk_venue_two 0.90 when two desks agree): called
    at once, no hold, from the state's FIRST poll close (desks only project once a state's polls are closed). Desks
    disagreeing = no desk call. Confidence desk_conf_hi (0.99) when two desks agree or a venue is >= hi, else
    desk_conf (0.985). A desk that projects another winner retracts a call and blocks the one-venue / hard venue
    calls for other parties, unless both venues hold >= hi (then the venues win: more likely a feed or mapping fault
    than a desk retraction); one that withdraws its projection retracts a desk call the venues don't hold.
Confidence = the lower venue's price for that party (capped at 0.995; one venue alone never lifts it). A call is retracted when the winner's best
outside price drops under `floor` (a mis-call or a venue glitch): the engine then stops buying that race.
Races only become CALLED after their polls close (safe seats sit at 0.98 all day; the call happens at the close).
Chamber control ("U.S. Senate"/"U.S. House") has no poll close and is called from the venues alone.
"Slow" races (ranked-choice ME/AK, runoff rules GA/LA) are flagged: the venues decide when they are settled.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sigbot.swing import POLL_CLOSE_H, POLL_FIRST_H

NIGHT0 = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)   # 19:00 EST, first full poll closes
SLOW_STATES = {"ME", "AK", "GA", "LA"}


def poll_close(state: str | None) -> datetime:
    """All polls closed in `state` (unknown state: 02:00Z, the median close)."""
    return NIGHT0 + timedelta(hours=POLL_CLOSE_H.get(state or "", 2.0))


def first_close(state: str | None) -> datetime:
    """First polls closed in `state`: results start flowing (earlier than poll_close in split-time-zone states)."""
    return NIGHT0 + timedelta(hours=POLL_FIRST_H.get(state or "", POLL_CLOSE_H.get(state or "", 2.0)))


@dataclass
class RaceState:
    race: str
    state: str | None
    exchanges: dict                    # party -> SIG exchange id
    chamber: bool = False
    status: str = "PRE"                # PRE / CLOSED / CALLED
    winner: str | None = None          # party
    conf: float = 0.0
    called_at: str | None = None
    source: str | None = None
    since: dict = field(default_factory=dict)   # party -> first time the both-venues condition held (iso)
    hard_since: dict = field(default_factory=dict)   # party -> first time one venue >= hard, other >= hard_other
    single_since: dict = field(default_factory=dict)  # party -> first time the only quoting venue was >= hard
    close_lead: float | None = None     # leading party's best outside price when the polls closed
    retracted: int = 0

    @property
    def slow(self) -> bool:
        return self.state in SLOW_STATES


class CallDetector:
    def __init__(self, races: dict[str, RaceState], hi: float = 0.97, hard: float = 0.99, floor: float = 0.90,
                 hold_s: float = 60.0, hard_other: float = 0.95, contested: float = 0.80,
                 early_min: float = 30.0, early_hold_s: float = 300.0, single_hold_s: float = 300.0,
                 single_conf: float = 0.985, desk_venue: float = 0.95, desk_conf: float = 0.985,
                 desk_conf_hi: float = 0.99, desk_venue_two: float = 0.90):
        """Calibrated on the 2024 night (research/calibrate_calls.py, Kalshi 1-minute candles, 52 races): a price that held
        >= 0.95-0.99 for one minute picked the loser once (NY-04 House: 0.99 for a minute, 19 min after its polls
        closed, from 0.385 at the close); a 2-minute hold was never wrong (0 of 25-46 at each threshold). Contested
        races were called hours after the close (median ~5 h Senate, ~17 h House), so a contested race (leader
        < `contested` at the close) needs `early_hold_s` in its first `early_min` minutes.
        One venue alone (a race only Polymarket lists, or Kalshi stale under election-night load) is the NY-04 case
        with nothing to cross-check it: it needs `single_hold_s` at >= hard, and its confidence is capped at
        `single_conf` (< 0.99), so the engine buys it with the wider margin under the smaller per-race cap."""
        self.races = races
        self.hi, self.hard, self.floor, self.hold_s = hi, hard, floor, hold_s
        self.hard_other, self.contested = hard_other, contested
        self.early_min, self.early_hold_s = early_min, early_hold_s
        self.single_hold_s, self.single_conf = single_hold_s, single_conf
        self.desk_venue, self.desk_conf, self.desk_conf_hi = desk_venue, desk_conf, desk_conf_hi
        self.desk_venue_two = desk_venue_two

    def closes_at(self, r: RaceState) -> datetime:
        return NIGHT0 if r.chamber else poll_close(r.state)

    def results_at(self, r: RaceState) -> datetime:
        return NIGHT0 if r.chamber else first_close(r.state)

    @staticmethod
    def desk_view(by_source: dict | None) -> tuple[str | None, int, bool]:
        """{source: party | None} -> (projected party, desks projecting it, any desk projecting another party).
        Two desks naming different winners: (None, 0, True)."""
        won = [w for w in (by_source or {}).values() if w is not None]
        if len(set(won)) != 1:
            return None, 0, len(set(won)) > 1
        return won[0], len(won), False

    def update(self, venue: dict[str, dict], now: datetime, desk: dict | None = None) -> list[dict]:
        """venue: exchange id -> {"kalshi": p or None, "polymarket": p or None} (YES mids). desk (optional):
        race -> {source: party | None} from sigbot/desk.py. Returns call events
        ({"kind": "call"|"retract", "race", "winner", "conf"}), and advances PRE -> CLOSED on the clock."""
        events = []
        desk = desk or {}
        for r in self.races.values():
            quotes = {party: venue.get(ex) or {} for party, ex in r.exchanges.items()}
            dwin, dn, dconflict = self.desk_view(desk.get(r.race))
            if r.status == "PRE" and now >= self.closes_at(r):
                r.status = "CLOSED"
                lead = [max([v for v in (q.get("kalshi"), q.get("polymarket")) if v is not None], default=0.0)
                        for q in quotes.values()]
                r.close_lead = max(lead, default=0.0)
            if r.status == "CALLED":
                q = quotes.get(r.winner) or {}
                vals = [v for v in (q.get("kalshi"), q.get("polymarket")) if v is not None]
                best = max(vals, default=None)
                # A desk naming another winner retracts any call; a desk call whose desks all withdrew it (race
                # listed, no winner) is retracted unless both venues hold the winner >= hi on their own.
                venues_hold = len(vals) == 2 and min(vals) >= self.hi
                withdrawn = (r.source == "desk" and bool(desk.get(r.race)) and dwin is None and not dconflict
                             and not venues_hold)
                other = dwin is not None and dwin != r.winner and not venues_hold
                if (best is not None and best < self.floor) or other or withdrawn:
                    events.append({"kind": "retract", "race": r.race, "winner": r.winner, "best": best,
                                   **({"desk": desk.get(r.race)} if other or withdrawn else {})})
                    # a desk call can land between a state's first and last poll close: back to PRE there
                    r.status = "CLOSED" if now >= self.closes_at(r) else "PRE"
                    r.winner, r.conf, r.called_at, r.source = None, 0.0, None, None
                    r.since, r.retracted = {}, r.retracted + 1
                elif best is not None:
                    vals = [v for v in (q.get("kalshi"), q.get("polymarket")) if v is not None]
                    if len(vals) == 2:     # one venue alone never raises the confidence
                        r.conf = min(0.995, max(r.conf, min(vals)))
                continue
            if dwin is not None and dwin in quotes and not r.chamber and now >= self.results_at(r):
                q = quotes[dwin]
                vals = [v for v in (q.get("kalshi"), q.get("polymarket")) if v is not None]
                if vals and max(vals) >= (self.desk_venue_two if dn >= 2 else self.desk_venue):
                    r.status, r.winner = "CALLED", dwin
                    r.conf = self.desk_conf_hi if dn >= 2 or max(vals) >= self.hi else self.desk_conf
                    r.called_at, r.source = now.isoformat(), "desk"
                    r.since, r.hard_since, r.single_since = {}, {}, {}
                    events.append({"kind": "call", "race": r.race, "winner": dwin, "conf": r.conf, "source": "desk",
                                   "desks": desk.get(r.race), "kalshi": q.get("kalshi"),
                                   "polymarket": q.get("polymarket")})
                    continue
            # The hold is tracked before the close too: a race already at >= hi on both venues when its polls close
            # (a safe seat) is called at the close itself, not hold_s later.
            for party, q in quotes.items():
                k, p = q.get("kalshi"), q.get("polymarket")
                vals = [v for v in (k, p) if v is not None]
                if not vals:
                    r.since.pop(party, None)
                    r.hard_since.pop(party, None)
                    r.single_since.pop(party, None)
                    continue
                if k is not None and p is not None and min(k, p) >= self.hi:
                    r.since.setdefault(party, now.isoformat())
                else:
                    r.since.pop(party, None)
                if r.status != "CLOSED":
                    continue
                if len(vals) == 2 and max(vals) >= self.hard and min(vals) >= self.hard_other:
                    r.hard_since.setdefault(party, now.isoformat())
                else:
                    r.hard_since.pop(party, None)
                if len(vals) == 1 and vals[0] >= self.hard:
                    r.single_since.setdefault(party, now.isoformat())
                else:
                    r.single_since.pop(party, None)
                hold = self.hold_s
                if (r.close_lead or 1.0) < self.contested and \
                        (now - self.closes_at(r)).total_seconds() < 60 * self.early_min:
                    hold = self.early_hold_s       # a contested race "called" minutes after the close: wait

                def lasted(since: dict, need: float) -> bool:
                    return party in since and (now - datetime.fromisoformat(since[party])).total_seconds() >= need

                held, hard = lasted(r.since, hold), lasted(r.hard_since, hold)
                single = not (held or hard) and lasted(r.single_since, max(hold, self.single_hold_s))
                if held or ((hard or single) and dwin in (None, party)):   # only both venues >= hi beat a desk
                    r.status, r.winner = "CALLED", party
                    r.conf = min(self.single_conf, vals[0]) if single else min(0.995, min(vals))
                    r.called_at, r.source = now.isoformat(), "both" if held else "hard" if hard else "single"
                    events.append({"kind": "call", "race": r.race, "winner": party, "conf": r.conf,
                                   "source": r.source, "kalshi": k, "polymarket": p})
                    break
        return events

    def manual(self, race: str, winner: str, now: datetime) -> dict | None:
        """Optional override (ops file); never required."""
        r = self.races.get(race)
        if r is None or winner not in r.exchanges:
            return None
        r.status, r.winner, r.conf, r.called_at, r.source = "CALLED", winner, 0.995, now.isoformat(), "manual"
        return {"kind": "call", "race": race, "winner": winner, "conf": r.conf, "source": "manual"}

    def called(self) -> dict[str, RaceState]:
        return {n: r for n, r in self.races.items() if r.status == "CALLED"}

    def dump(self) -> str:
        return json.dumps({n: asdict(r) for n, r in self.races.items()}, indent=1)

    def load(self, text: str):
        """Restore a previous run's calls (a restarted run would re-derive them within hold_s anyway)."""
        for n, d in json.loads(text).items():
            if n in self.races and d.get("status") == "CALLED":
                r = self.races[n]
                r.status, r.winner, r.conf = "CALLED", d["winner"], d["conf"]
                r.called_at, r.source = d.get("called_at"), d.get("source")
