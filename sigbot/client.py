"""Minimal SIG Predictions Cup (Super Market) API client.

- Auth: Bearer key from SIG_API_KEY (.env). The key is never printed or logged.
- Retries per the documented error codes (see CLAUDE.md / openapi.json).
- Every request/response is appended to logs/api-YYYYMMDD.jsonl (no headers, key redacted).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

CANDIDATE_BASE_URLS = [
    "https://sig.thesuper.market/api/v1",
    "https://www.thesuper.market/api/v1",
]

# Codes that are safe to retry with exponential backoff + jitter.
BACKOFF_CODES = {"RATE_LIMITED", "TX_CONFLICT", "SERVICE_UNAVAILABLE"}
# Engine may have accepted the order; retry the identical request with the same idempotency key.
UNKNOWN_CODES = {"ORDER_STATUS_UNKNOWN"}
IN_FLIGHT_WAIT_S = 90
# Red line: never touch the user's account registration/profile info.
BLOCKED_ROOTS = {"account"}
# Documented per-key budget is 100 reads + 30 writes per minute, and a 429 costs Retry-After: 60.
# Stay a little under it client-side so we never pay that minute.
READS_PER_MIN, WRITES_PER_MIN = 90, 27
_LOG_LOCK = threading.Lock()


class RateLimiter:
    """Sliding 60 s window; `acquire` sleeps until one more call fits."""

    def __init__(self, per_min: int):
        self.per_min = per_min
        self.calls: list[float] = []
        self.lock = threading.Lock()   # maker.py sends order batches from several threads

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.calls = [t for t in self.calls if now - t < 60]
                if len(self.calls) < self.per_min:
                    self.calls.append(now)
                    return
                wait = 60 - (now - self.calls[0]) + 0.05
            time.sleep(wait)

    def available(self) -> int:
        now = time.monotonic()
        return self.per_min - sum(1 for t in self.calls if now - t < 60)


class SigAPIError(Exception):
    def __init__(self, status: int, code: str | None, message: str, details: Any = None):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(f"{status} {code}: {message}" + (f" {details}" if details else ""))


def load_api_key() -> str:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("SIG_API_KEY", "").strip()
    if not key:
        raise SystemExit("SIG_API_KEY not set. Put it in .env (see .env.example).")
    return key


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


class SigClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: float = 20.0, max_retries: int = 6, log: bool = True,
                 write_timeout: float = 90.0):
        self.base_url = (base_url or os.environ.get("SIG_BASE_URL") or CANDIDATE_BASE_URLS[0]).rstrip("/")
        self._key = api_key or load_api_key()
        self.timeout = timeout
        # Order writes can take 20 s+ server-side under load; timing out early costs a 90 s REQUEST_IN_FLIGHT wait.
        self.write_timeout = write_timeout
        self.max_retries = max_retries
        self.log_enabled = log
        self.read_limiter = RateLimiter(READS_PER_MIN)
        self.write_limiter = RateLimiter(WRITES_PER_MIN)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": "sig-predictions-cup-bot/0.1",
        })

    def __repr__(self) -> str:  # never expose the key
        return f"SigClient(base_url={self.base_url!r})"

    # ---------------------------------------------------------------- logging
    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***REDACTED***") if self._key else text

    def _log(self, entry: dict) -> None:
        if not self.log_enabled:
            return
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"api-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        line = self._redact(json.dumps(entry, default=str))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---------------------------------------------------------------- core
    @staticmethod
    def _backoff(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
        return random.uniform(0, min(cap, base * 2 ** attempt))  # full jitter

    @staticmethod
    def _error_of(resp: requests.Response) -> tuple[str | None, str, Any]:
        try:
            body = resp.json()
        except ValueError:
            return None, resp.text[:300], None
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return err.get("code"), err.get("message", ""), err.get("details")
        if isinstance(err, str):
            return body.get("code"), err, body.get("details")
        return None, str(body)[:300], None

    def _send(self, method: str, url: str, params: dict, json_body: dict | None) -> requests.Response:
        """One HTTP exchange with a wall-clock cap. requests' read timeout only bounds the gap between bytes,
        and on 2 Oct the SIG server trickled a /markets body for 25-40 min; this aborts at 2x the timeout."""
        timeout = self.timeout if method in ("GET", "HEAD") else self.write_timeout
        box: dict = {}

        def work():
            try:   # own session per call: an abandoned call must not share a connection with the next one
                with requests.Session() as s:
                    s.headers.update(self.session.headers)
                    box["resp"] = s.request(method, url, params=params, json=json_body, timeout=(10, timeout))
            except Exception as e:  # noqa: BLE001 - re-raised in the caller's thread
                box["error"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(2 * timeout)
        if th.is_alive():   # left to die on its own; the caller retries (orders carry idempotency keys)
            raise requests.Timeout(f"no complete response after {2 * timeout:.0f} s")
        if "error" in box:
            raise box["error"]
        return box["resp"]

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None) -> Any:
        if path.strip("/").split("?")[0].split("/")[0] in BLOCKED_ROOTS:
            raise PermissionError(f"/{path.strip('/')} is blocked: account registration info is off limits.")
        url = f"{self.base_url}/{path.lstrip('/')}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        limiter = self.read_limiter if method in ("GET", "HEAD") else self.write_limiter
        while True:
            limiter.acquire()
            t0 = time.monotonic()
            entry = {"ts": datetime.now(timezone.utc).isoformat(), "method": method, "url": url,
                     "params": params, "body": json_body, "attempt": attempt}
            try:
                resp = self._send(method, url, params, json_body)
            except (requests.ConnectionError, requests.Timeout) as e:
                entry.update(error=type(e).__name__, elapsed_ms=int((time.monotonic() - t0) * 1000))
                self._log(entry)
                # POSTs to /orders* carry an idempotency key, so a resend is a safe replay.
                if attempt >= self.max_retries:
                    raise
                time.sleep(self._backoff(attempt))
                attempt += 1
                continue

            entry.update(status=resp.status_code, elapsed_ms=int((time.monotonic() - t0) * 1000),
                         response=resp.text[:20000])
            self._log(entry)

            if resp.ok:
                return resp.json() if resp.content else None

            code, message, details = self._error_of(resp)
            retry_after = resp.headers.get("Retry-After")
            wait = None
            if code in BACKOFF_CODES or resp.status_code == 429 or (resp.status_code == 503):
                wait = float(retry_after) if retry_after and retry_after.isdigit() else self._backoff(attempt)
            elif code in UNKNOWN_CODES or resp.status_code == 502:
                wait = self._backoff(attempt)  # same payload, same idempotency key
            elif code == "REQUEST_IN_FLIGHT":
                wait = IN_FLIGHT_WAIT_S
            elif resp.status_code >= 500 and method == "GET":
                wait = self._backoff(attempt)

            if wait is None or attempt >= self.max_retries:
                raise SigAPIError(resp.status_code, code, message, details)
            time.sleep(wait)
            attempt += 1

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict) -> Any:
        return self.request("POST", path, json_body=body)

    def delete(self, path: str, **params) -> Any:
        return self.request("DELETE", path, params=params)

    def paginate(self, path: str, limit: int = 100, **params) -> Iterator[dict]:
        """Yield items across pages using pagination.hasMore / nextCursor."""
        cursor = None
        while True:
            page = self.get(path, limit=limit, cursor=cursor, **params)
            yield from page.get("data", [])
            pg = page.get("pagination") or {}
            cursor = pg.get("nextCursor")
            if not pg.get("hasMore") or not cursor:
                return
