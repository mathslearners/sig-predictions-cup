"""Trading lease for election night: exactly one election.py trades, any other runs as a hot standby.

Two routines start election.py on the night (the election routine at :40 of odd hours, and the 2-hourly maker
routine, whose maker.py hands over to election.py after 3 Nov 21:37Z), so a crashed, killed or never-started run is
covered by the other within ~ttl. The lease is a JSON file on branch `election-lease`, written with git plumbing (no
working-tree changes) and pushed WITHOUT force: a push that is not a fast-forward of what we read is rejected, so two
bots can never both believe they won the same race.

  try_acquire() -> "held" (we hold it, renewed for ttl s), "other" (someone else holds an unexpired lease),
                   "error" (git unreachable: the holder keeps trading, a standby keeps waiting; if it lasts
                   > ttl + 30 s the primary run trades without the lease, see election.LeaseKeeper)
  release()     -> mark it expired so the next bot takes over at once (end of a run's slot)

Remote kill switch (RemoteStop): the file config/STOP_ELECTION on origin/main, fetched by every bot with each lease
check. While it exists no bot trades: the holder cancels everything and gives up the lease, standbys don't take it.
It can be added or deleted from anywhere (GitHub web/app, gh api, a push), so it reaches a running cloud session.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

BRANCH = "election-lease"
STOP_BRANCH, STOP_PATH = "main", "config/STOP_ELECTION"


def _git(root: Path, *args, inp: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    # bytes, not text=True: on Windows text mode turns the mktree "\n" into "\r\n" (a file named "lease.json\r",
    # which read() never finds, so every bot wins the lease)
    r = subprocess.run(["git", *args], cwd=root, input=None if inp is None else inp.encode("utf-8"),
                       capture_output=True, timeout=timeout)
    r.stdout, r.stderr = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    return r


class RemoteStop:
    """check() -> True (STOP_PATH exists on origin/STOP_BRANCH), False (it doesn't), None (git unreachable: the
    caller keeps its last answer). Fetched into a private ref, so the checkout and origin/main stay untouched."""

    def __init__(self, root: Path, branch: str = STOP_BRANCH, path: str = STOP_PATH):
        self.root, self.branch, self.path = root, branch, path
        self.ref = f"refs/sigbot/stop-{branch}"

    def check(self) -> bool | None:
        try:
            if _git(self.root, "fetch", "-q", "origin", f"+refs/heads/{self.branch}:{self.ref}").returncode != 0:
                return None
            return _git(self.root, "cat-file", "-e", f"{self.ref}:{self.path}").returncode == 0
        except Exception:  # noqa: BLE001 - timeout, git missing
            return None


class Lease:
    def __init__(self, owner: str, root: Path, ttl_s: float = 60.0, branch: str = BRANCH):
        self.owner, self.root, self.ttl, self.branch = owner, root, ttl_s, branch
        self.last: dict | None = None
        self.state = "none"
        self.extra: dict = {}     # written with every renewal (election.py: the night's sweep ledger)

    def _git(self, *args, inp: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        return _git(self.root, *args, inp=inp, timeout=timeout)

    def read(self) -> tuple[dict | None, str | None]:
        """(lease, head commit) from origin; (None, None) when the branch does not exist yet."""
        r = self._git("fetch", "-q", "origin", f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}")
        if r.returncode != 0:
            if "couldn't find remote ref" in (r.stderr or "").lower():
                return None, None
            raise RuntimeError(f"fetch: {r.stderr.strip()[:200]}")
        head = self._git("rev-parse", f"refs/remotes/origin/{self.branch}").stdout.strip()
        show = self._git("show", f"{head}:lease.json")
        return (json.loads(show.stdout) if show.returncode == 0 and show.stdout.strip() else None), head

    def _write(self, lease: dict, parent: str | None) -> bool:
        blob = self._git("hash-object", "-w", "--stdin", inp=json.dumps(lease)).stdout.strip()
        tree = self._git("mktree", inp=f"100644 blob {blob}\tlease.json\n").stdout.strip()
        args = ["commit-tree", tree, "-m", f"lease {lease['owner']} until {lease['until']:.0f}"]
        if parent:
            args += ["-p", parent]
        commit = self._git(*args, inp="").stdout.strip()
        if not commit:
            raise RuntimeError("commit-tree failed")
        r = self._git("push", "-q", "origin", f"{commit}:refs/heads/{self.branch}", timeout=60)
        return r.returncode == 0

    def try_acquire(self) -> str:
        try:
            cur, head = self.read()
            now = time.time()
            self.last = cur
            if cur and cur.get("owner") != self.owner and cur.get("until", 0) > now:
                self.state = "other"
                return self.state
            ok = self._write({**self.extra, "owner": self.owner, "until": now + self.ttl, "at": now}, head)
            self.state = "held" if ok else "other"     # rejected push: someone else wrote first
            return self.state
        except Exception:  # noqa: BLE001 - git down: callers decide (holder keeps trading, standby waits)
            self.state = "error"
            return self.state

    def release(self):
        try:
            cur, head = self.read()
            if cur and cur.get("owner") == self.owner:
                self._write({**self.extra, "owner": self.owner, "until": 0, "at": time.time(), "released": True}, head)
        except Exception:  # noqa: BLE001
            pass
