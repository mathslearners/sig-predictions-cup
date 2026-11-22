"""Normalise race descriptions from SIG, Kalshi and Polymarket to one key.

Key = (state_code, office, district, special)
  office   in {"SEN", "GOV", "HOUSE"}
  district "" for statewide races, "1".."53" for House seats, "AL" for at-large
  special  True for special elections (e.g. the Ohio special Senate race)
"""
from __future__ import annotations

import re

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
CODES = set(STATES.values())
# Longest names first so "West Virginia" wins over "Virginia", "North Dakota" over "Dakota", etc.
_STATE_RE = re.compile(r"\b(" + "|".join(sorted(STATES, key=len, reverse=True)) + r")\b")
_CODE_DIST_RE = re.compile(r"\b([A-Z]{2})[- ]?(\d{1,2}|AL)\b")
_DIST_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\b(?:\s*(?:congressional\s*)?district)?", re.I)

Key = tuple  # (state, office, district, special)


def party_of(text: str) -> str | None:
    t = text.lower()
    hits = {p for word, p in (("republican", "R"), ("gop", "R"), ("democrat", "D"),
                              ("independent", "I")) if word in t}
    if len(hits) == 1:
        return hits.pop()
    m = re.search(r"\(([RDI])\)", text)
    return m.group(1) if m else None


def key_from_text(text: str) -> Key | None:
    """Parse a free-text race description, e.g. 'Maine Senate', 'Arizona 6th District House',
    'AZ-06 House', 'Ohio Special Senate', 'Alaska At-Large House'."""
    low = text.lower()
    if "senate" in low:
        office = "SEN"
    elif "governor" in low or "gubernatorial" in low:
        office = "GOV"
    elif "house" in low or "district" in low or "congress" in low:
        office = "HOUSE"
    else:
        return None
    special = "special" in low

    state, district = None, ""
    if (m := _STATE_RE.search(text)):
        state = STATES[m.group(1)]
    if office == "HOUSE":
        cm = _CODE_DIST_RE.search(text)
        if cm and cm.group(1) in CODES:
            state = state or cm.group(1)
            district = cm.group(2)
        elif "at-large" in low or "at large" in low:
            district = "AL"
        elif (dm := _DIST_RE.search(text[m.end():] if m else text)):
            district = dm.group(1)
        if district.isdigit():
            district = str(int(district))
        if not district:
            return None
    if not state:
        return None
    return (state, office, district, special)


_KALSHI_SERIES = re.compile(r"^(?:KX)?(SENATE|GOVPARTY|GOV|HOUSE)([A-Z]{2})(S)?(\d{1,2}|AL)?$")


def key_from_kalshi_series(series: str) -> Key | None:
    m = _KALSHI_SERIES.match(series.upper())
    if not m or m.group(2) not in CODES:
        return None
    kind, state, special, dist = m.groups()
    office = {"SENATE": "SEN", "GOVPARTY": "GOV", "GOV": "GOV", "HOUSE": "HOUSE"}[kind]
    if office == "HOUSE":
        if not dist:
            return None
        return (state, office, dist if dist == "AL" else str(int(dist)), False)
    if dist:
        return None
    return (state, office, "", bool(special))


def fmt(key: Key) -> str:
    s, o, d, sp = key
    return f"{s} {o}{('-' + d) if d else ''}{' (special)' if sp else ''}"
