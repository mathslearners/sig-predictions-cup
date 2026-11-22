"""READ-ONLY probe of SIG's realtime market feed: joins every tournament market channel for --seconds and reports
join replies, batches per channel and how fresh the pushed books are. Places nothing; uses 1 REST call (token).

  python tools/realtime_probe.py --seconds 120
  python tools/realtime_probe.py --seconds 60 --tournament-channel   # also tournament:{tid} (one channel, all markets)
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots

import argparse
import json
import time
from datetime import datetime, timezone

from sigbot.client import ROOT, SigClient
from sigbot.realtime import RealtimeFeed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--tournament-channel", action="store_true",
                    help="also join tournament:{tid}, which grissou/prediction-market-maker uses for bookDirty")
    a = ap.parse_args()
    u = json.loads((ROOT / "config" / "universe.json").read_text(encoding="utf-8"))
    lag = []
    kinds = {}

    def on_batch(topic, event, p):
        kinds[event] = kinds.get(event, 0) + 1
        for b in p.get("books") or []:
            at = (b.get("asOf") or {}).get("at")
            if at:
                lag.append(time.time() - datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp())

    tid = u["tournamentId"]
    feed = RealtimeFeed(SigClient(), tid, [m["id"] for m in u["markets"]], on_batch,
                        extra_topics=[f"tournament:{tid}"] if a.tournament_channel else [])
    print("token channels:", sorted(feed.channels), flush=True)
    feed.start()
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        time.sleep(10)
        print(f"[{time.time() - t0:5.0f}s] {feed.summary()} events {kinds}", flush=True)
    feed.stop()
    if lag:
        lag.sort()
        print(f"book age on arrival: median {lag[len(lag) // 2]:.2f}s p90 {lag[9 * len(lag) // 10]:.2f}s (n={len(lag)})")
    if a.tournament_channel:
        t = f"tournament:{tid}"
        print(f"{t}: join {feed.join_status.get(t)} {feed.join_error.get(t) or ''} batches {feed.per_topic.get(t, 0)}")
    print("VERDICT:", "feed LIVE" if feed.stats["batches"] else "no batches received")


if __name__ == "__main__":
    main()
