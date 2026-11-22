"""Remote kill switch for election.py: add / remove config/STOP_ELECTION on origin/main.

  python stop_election.py on        # every election.py cancels everything and stands by within ~15 s
  python stop_election.py off       # trading resumes within ~15 s
  python stop_election.py status

Works from any clone without touching its checkout or branch (git plumbing on a temporary index, pushed without
force). From a phone: create / delete the file on main in the GitHub web page or app (ELECTION_NIGHT.md).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sigbot.lease import STOP_BRANCH, STOP_PATH, RemoteStop, _git

ROOT = Path(__file__).resolve().parent


def flip(on: bool, root: Path = ROOT, branch: str = STOP_BRANCH, path: str = STOP_PATH, tries: int = 5) -> bool:
    """Returns True once origin/branch has the file (on) or doesn't (off)."""
    rs = RemoteStop(root, branch, path)
    for _ in range(tries):
        cur = rs.check()
        if cur is None:
            raise RuntimeError(f"cannot fetch origin/{branch}")
        if cur == on:
            return True
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(d) / "index")}

            def git(*args, inp=None):
                r = _git_env(root, env, *args, inp=inp)
                if r.returncode != 0:
                    raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[:200]}")
                return r.stdout.strip()

            git("read-tree", rs.ref)
            if on:
                blob = git("hash-object", "-w", "--stdin", inp=f"stop {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
                git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
            else:
                git("update-index", "--force-remove", path)
            tree = git("write-tree")
            commit = git("commit-tree", tree, "-p", rs.ref, "-m",
                         "STOP election bot (stop_election.py on)" if on else "resume election bot (stop_election.py off)",
                         inp="")
        if _git(root, "push", "-q", "origin", f"{commit}:refs/heads/{branch}", timeout=60).returncode == 0:
            return True
        time.sleep(2)     # someone else pushed main meanwhile: re-read and retry
    return False


def _git_env(root: Path, env: dict, *args, inp: str | None = None):
    r = subprocess.run(["git", *args], cwd=root, env=env, input=None if inp is None else inp.encode("utf-8"),
                       capture_output=True, timeout=60)
    r.stdout, r.stderr = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    return r


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        s = RemoteStop(ROOT).check()
        print({True: "STOP is SET: election.py is not trading", False: "not set: election.py trades normally",
               None: "cannot reach origin"}[s])
        sys.exit(0 if s is not None else 1)
    if cmd not in ("on", "off"):
        sys.exit(__doc__)
    ok = flip(cmd == "on")
    print(("STOP set on origin/main: bots stop within ~15 s" if cmd == "on" else
           "STOP removed from origin/main: bots resume within ~15 s") if ok else "push kept failing: try again")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
