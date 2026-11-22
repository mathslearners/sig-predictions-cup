"""Minimal Supabase Realtime (Phoenix protocol) client for SIG's private broadcast channels.

SIG pushes `market_batch` on `tournament:{tournament_id}:market:{market_id}` once a second, carrying trades and the
full versioned `books` of each exchange it names (API docs, Realtime section). That is ~1 s from a book change to us,
against 5 s+ for polling /exchanges/prices on our slice of the 100 reads/min. One socket carries at most 100
channels, so the 237 markets are spread over several sockets.

Usage:
    feed = RealtimeFeed(sig_client, tournament_id, market_ids, on_batch)   # on_batch(topic, payload)
    feed.start(); ...; feed.stop()

Delivery is best-effort with no replay: callers keep polling REST as the fallback and the authority.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict

import websocket   # websocket-client

MAX_CHANNELS = 100
HEARTBEAT_S = 25
TOKEN_REFRESH_S = 2.5 * 3600   # tokens live 3 h


class _Socket(threading.Thread):
    def __init__(self, feed: "RealtimeFeed", topics: list[str], n: int):
        super().__init__(daemon=True, name=f"rt{n}")
        self.feed, self.topics = feed, topics
        self.ws: websocket.WebSocket | None = None
        self.ref = 0
        self.lock = threading.Lock()

    def send(self, topic: str, event: str, payload: dict, join_ref: str | None = None):
        with self.lock:
            self.ref += 1
            msg = {"topic": topic, "event": event, "payload": payload, "ref": str(self.ref)}
            if join_ref:
                msg["join_ref"] = join_ref
            self.ws.send(json.dumps(msg))

    def join_all(self):
        cfg = {"config": {"broadcast": {"ack": False, "self": False}, "presence": {"key": ""},
                          "postgres_changes": [], "private": True},
               "access_token": self.feed.token}
        for t in self.topics:
            self.send(f"realtime:{t}", "phx_join", cfg, join_ref=str(self.ref + 1))

    def run(self):
        f = self.feed
        while not f.halt.is_set():
            try:
                self.ws = websocket.create_connection(
                    f"{f.url.replace('https://', 'wss://')}/realtime/v1/websocket?apikey={f.anon}&vsn=1.0.0",
                    timeout=HEARTBEAT_S)
                self.join_all()
                f.stats["connects"] += 1
                last_hb = time.time()
                token_seen = f.token
                while not f.halt.is_set():
                    if time.time() - last_hb > HEARTBEAT_S:
                        self.send("phoenix", "heartbeat", {})
                        last_hb = time.time()
                    if f.token != token_seen:   # refreshed token: push it to every joined channel
                        for t in self.topics:
                            self.send(f"realtime:{t}", "access_token", {"access_token": f.token})
                        token_seen = f.token
                    try:
                        raw = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    self.handle(json.loads(raw))
            except Exception as e:  # noqa: BLE001 - reconnect; callers fall back to REST meanwhile
                f.stats["errors"] += 1
                f.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            finally:
                try:
                    self.ws and self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
            f.stats["reconnects"] += 1
            # Back off while connects keep failing (4 Oct cloud runs: ~1 failed connect/s per socket for 2 h when
            # the environment's network allowlist lacked the Supabase host).
            fails = 0 if f.stats.get("batches") else f.stats["errors"]
            f.halt.wait(min(60, 3 * 2 ** min(fails // len(f.sockets), 5)) if fails else 3)

    def handle(self, m: dict):
        f = self.feed
        topic = m.get("topic", "").removeprefix("realtime:")
        ev = m.get("event")
        if ev == "phx_reply" and topic != "phoenix":
            st = (m.get("payload") or {}).get("status")
            f.join_status[topic] = st if st != "ok" else "ok"
            if st != "ok":
                f.join_error[topic] = str((m.get("payload") or {}).get("response"))[:200]
        elif ev == "system":
            p = m.get("payload") or {}
            if p.get("status") == "error":
                f.join_status[topic] = "error"
                f.join_error[topic] = str(p.get("message"))[:200]
        elif ev == "phx_error" or ev == "phx_close":
            f.join_status[topic] = ev
        elif ev == "broadcast":
            p = m.get("payload") or {}
            f.stats["batches"] += 1
            f.per_topic[topic] += 1
            f.last_batch_at = time.time()
            try:
                f.on_batch(topic, p.get("event"), p.get("payload") or {})
            except Exception as e:  # noqa: BLE001
                f.stats["callback_errors"] += 1
                f.last_error = f"callback {type(e).__name__}: {str(e)[:200]}"


class RealtimeFeed:
    def __init__(self, client, tournament_id: str, market_ids: list[str], on_batch, extra_topics: list[str] = ()):
        self.c, self.tid, self.on_batch = client, tournament_id, on_batch
        self.halt = threading.Event()
        self.stats = defaultdict(int)
        self.per_topic = defaultdict(int)
        self.join_status: dict[str, str] = {}
        self.join_error: dict[str, str] = {}
        self.last_error: str | None = None
        self.last_batch_at: float | None = None
        self._mint()
        topics = [f"tournament:{tournament_id}:market:{m}" for m in market_ids] + list(extra_topics)
        self.sockets = [_Socket(self, topics[i:i + MAX_CHANNELS - 5], n)
                        for n, i in enumerate(range(0, len(topics), MAX_CHANNELS - 5))]
        self._refresher = threading.Thread(target=self._refresh_loop, daemon=True, name="rt-token")

    def _mint(self):
        r = self.c.post("/realtime/token", {})
        if not r.get("token"):
            raise RuntimeError("realtime token response carried no token")
        self.token, self.url, self.anon = r["token"], r["supabaseUrl"], r["anonKey"]
        self.channels = r.get("channels") or {}   # e.g. {"user": "user:<profile>"}

    def _refresh_loop(self):
        while not self.halt.wait(TOKEN_REFRESH_S):
            try:
                self._mint()
            except Exception as e:  # noqa: BLE001
                self.last_error = f"token {type(e).__name__}: {str(e)[:200]}"

    def start(self):
        for s in self.sockets:
            s.start()
        self._refresher.start()

    def stop(self):
        self.halt.set()

    def live(self, max_age_s: float = 10) -> bool:
        return self.last_batch_at is not None and time.time() - self.last_batch_at < max_age_s

    def summary(self) -> dict:
        st = defaultdict(int)
        for v in self.join_status.values():
            st[v] += 1
        return {**self.stats, "joins": dict(st), "topics_with_data": len(self.per_topic),
                "last_error": self.last_error, "sample_join_error": next(iter(self.join_error.values()), None)}
