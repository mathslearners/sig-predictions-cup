"""Polymarket push prices for election night (public CLOB market websocket, no key).

  wss://ws-subscriptions-clob.polymarket.com/ws/market, subscribe {"assets_ids": [...], "type": "market"}
  -> "book" snapshots (bids/asks [{price, size}]) and "price_change" deltas (new format: price_changes[] with
     asset_id/price/size/side and best_bid/best_ask; old format: asset_id + changes[]). Text "PING" every 10 s.

Subscribes to the YES token of each mapped Polymarket market (clobTokenIds from the Gamma API, one call per 50 ids)
and keeps best bid/ask per market id, in the same {"bid", "ask", "mid", "active"} shape as Polymarket.quotes(), so
outside_fair() takes it unchanged. The cloud environment's network allowlist must include
ws-subscriptions-clob.polymarket.com (blocked by default: checked 12 Oct). While it isn't live, callers keep polling
Gamma (Polymarket.quotes) as they do today.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict

import requests

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com"
PING_S = 10
PER_SOCKET = 250


def yes_tokens(market_ids: list[str], timeout: float = 10.0) -> dict[str, str]:
    """{YES clob token id: Polymarket market id}."""
    out = {}
    ids = sorted(set(market_ids))
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        r = requests.get(f"{GAMMA}/markets", params=[("id", x) for x in chunk] + [("limit", len(chunk))],
                         timeout=timeout)
        r.raise_for_status()
        for m in r.json() or []:
            try:
                toks, outs = json.loads(m.get("clobTokenIds") or "[]"), json.loads(m.get("outcomes") or "[]")
            except (TypeError, ValueError):
                continue
            if toks and outs and str(outs[0]).lower() == "yes":
                out[str(toks[0])] = str(m["id"])
    return out


class PolyBooks:
    """Best bid/ask per Polymarket market id, maintained from websocket messages (pure state; testable offline)."""

    def __init__(self, token_to_market: dict[str, str]):
        self.tok = token_to_market
        self.lock = threading.Lock()
        self.levels = defaultdict(lambda: {"bids": {}, "asks": {}})   # token -> price -> size
        self.best: dict[str, tuple] = {}     # market id -> (bid, ask, time)
        self.last_msg = 0.0
        self.msgs = 0

    def _set_best(self, token: str, bid=None, ask=None):
        mid = self.tok.get(token)
        if mid is None:
            return
        lv = self.levels[token]
        if bid is None:
            bid = max((p for p, s in lv["bids"].items() if s > 0), default=None)
        if ask is None:
            ask = min((p for p, s in lv["asks"].items() if s > 0), default=None)
        self.best[mid] = (bid, ask, time.time())

    def handle(self, msg):
        if isinstance(msg, list):
            for m in msg:
                self.handle(m)
            return
        if not isinstance(msg, dict):
            return
        et = msg.get("event_type")
        with self.lock:
            self.last_msg, self.msgs = time.time(), self.msgs + 1
            if et == "book":
                tok = str(msg.get("asset_id"))
                self.levels[tok] = {
                    "bids": {float(l["price"]): float(l["size"]) for l in msg.get("bids") or msg.get("buys") or []},
                    "asks": {float(l["price"]): float(l["size"]) for l in msg.get("asks") or msg.get("sells") or []}}
                self._set_best(tok)
            elif et == "price_change":
                if msg.get("price_changes"):
                    for c in msg["price_changes"]:
                        tok = str(c.get("asset_id"))
                        side = "bids" if str(c.get("side")).upper() == "BUY" else "asks"
                        self.levels[tok][side][float(c["price"])] = float(c.get("size") or 0)
                        bb, ba = c.get("best_bid"), c.get("best_ask")
                        self._set_best(tok, float(bb) if bb not in (None, "") else None,
                                       float(ba) if ba not in (None, "") else None)
                else:
                    tok = str(msg.get("asset_id"))
                    for c in msg.get("changes") or []:
                        side = "bids" if str(c.get("side")).upper() == "BUY" else "asks"
                        self.levels[tok][side][float(c["price"])] = float(c.get("size") or 0)
                    self._set_best(tok)
            elif et == "best_bid_ask":
                tok = str(msg.get("asset_id"))
                self._set_best(tok, float(msg["best_bid"]), float(msg["best_ask"]))

    def quotes(self, max_age: float = 120.0) -> dict[str, dict]:
        """Polymarket.quotes() shape for markets updated within max_age s (a quiet book keeps its last snapshot,
        so max_age is generous; the feed as a whole must be live, see live())."""
        now = time.time()
        out = {}
        with self.lock:
            for mid, (b, a, t) in self.best.items():
                if now - t > max_age and now - self.last_msg > 15:
                    continue
                ok = b is not None and a is not None and 0 < b <= a < 1
                out[mid] = {"bid": b, "ask": a, "mid": (b + a) / 2 if ok else None, "active": True, "at": t}
        return out

    def live(self, quiet_s: float = 20.0) -> bool:
        return time.time() - self.last_msg < quiet_s


class PolyFeed:
    """Websocket threads (PER_SOCKET tokens each) feeding a PolyBooks. Reconnects with backoff forever."""

    def __init__(self, market_ids: list[str]):
        self.books = PolyBooks(yes_tokens(market_ids))
        self.halt = threading.Event()
        self.stats = defaultdict(int)
        self.last_error = None
        toks = sorted(self.books.tok)
        self.threads = [threading.Thread(target=self._run, args=(toks[i:i + PER_SOCKET],), daemon=True,
                                         name=f"polyws{i // PER_SOCKET}") for i in range(0, len(toks), PER_SOCKET)]

    def start(self):
        for t in self.threads:
            t.start()

    def stop(self):
        self.halt.set()

    def _run(self, toks: list[str]):
        import websocket
        fails = 0
        while not self.halt.is_set():
            ws = None
            try:
                ws = websocket.create_connection(WS_URL, timeout=PING_S)
                ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                self.stats["connects"] += 1
                last_ping = time.time()
                while not self.halt.is_set():
                    if time.time() - last_ping > PING_S:
                        ws.send("PING")
                        last_ping = time.time()
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    if raw in ("PONG", "PING"):
                        continue
                    fails = 0
                    self.books.handle(json.loads(raw))
            except Exception as e:  # noqa: BLE001 - REST polling stays the fallback
                self.stats["errors"] += 1
                fails += 1
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            finally:
                try:
                    ws and ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self.halt.wait(min(60, 2 ** min(fails, 6)))
