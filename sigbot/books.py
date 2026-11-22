"""Full order books for every exchange, kept current from SIG's realtime feed (no REST reads).

Each `market_batch` on tournament:{t}:market:{m} carries the complete, versioned book of every exchange it names
(`books[]`: exchangeId, bids/asks [{price, quantity}], asOf {sequence, at}). Confirmed live 4 Oct: all 237 channels
deliver, books arrive a median 0.28 s after they change. Books are YES-denominated: a NO bid at q is a YES ask at
1 - q. Delivery is best-effort, so a book is only trusted while the feed as a whole is live (`healthy`), and a
book older than `max_age` with no push is still fine (quiet market) but callers that need certainty use REST.
"""
from __future__ import annotations

import threading
import time


class BookCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.books: dict[str, dict] = {}   # exchange -> {"bids": [(p, q)], "asks": [(p, q)], "seq", "at"}
        self.last_push = 0.0
        self.pushes = 0

    def apply(self, payload: dict) -> list[str]:
        """Apply one market_batch payload; returns the exchanges whose book changed."""
        changed = []
        now = time.time()
        with self.lock:
            for b in payload.get("books") or []:
                ex = str(b.get("exchangeId") or b.get("exchange_id") or "")
                if not ex:
                    continue
                seq = (b.get("asOf") or {}).get("sequence")
                old = self.books.get(ex)
                if old is not None and seq is not None and old["seq"] is not None and seq <= old["seq"]:
                    continue   # duplicate or out-of-order delivery (batches can arrive up to 3 times)
                bids = sorted(((float(l["price"]), float(l["quantity"])) for l in b.get("bids") or []
                               if float(l.get("quantity") or 0) > 0), reverse=True)
                asks = sorted((float(l["price"]), float(l["quantity"])) for l in b.get("asks") or []
                              if float(l.get("quantity") or 0) > 0)
                self.books[ex] = {"bids": bids, "asks": asks, "seq": seq, "at": now}
                changed.append(ex)
            if payload.get("books"):
                self.last_push, self.pushes = now, self.pushes + 1
        return changed

    def healthy(self, max_quiet: float = 15.0) -> bool:
        """The feed is delivering (237 markets push several batches a second when it works)."""
        return time.time() - self.last_push < max_quiet

    def top(self, ex: str) -> tuple | None:
        with self.lock:
            b = self.books.get(ex)
            if b is None:
                return None
            return (b["bids"][0][0] if b["bids"] else None, b["asks"][0][0] if b["asks"] else None)

    def tops(self) -> dict[str, tuple]:
        with self.lock:
            return {ex: (b["bids"][0][0] if b["bids"] else None, b["asks"][0][0] if b["asks"] else None)
                    for ex, b in self.books.items()}

    def side_bids(self, ex: str, side: str) -> list[tuple] | None:
        """Bids for buying `side` (best first) as (price of that side, qty): YES bids as they are, NO bids from the
        YES asks (a NO bid at q is a YES ask at 1 - q). None if no book is cached."""
        with self.lock:
            b = self.books.get(ex)
            if b is None:
                return None
            if side == "yes":
                return list(b["bids"])
            return [(round(1 - p, 3), q) for p, q in b["asks"]]
