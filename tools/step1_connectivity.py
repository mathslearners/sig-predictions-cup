"""Step 1: find the working base URL and dump the raw /markets shape.

Connectivity is tested with GET /markets?limit=1 (never /account — off limits).
Never prints the API key. Raw responses are saved to data/raw/.
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots
import json
from datetime import datetime, timezone

from sigbot.client import CANDIDATE_BASE_URLS, ROOT, SigAPIError, SigClient, load_api_key


def shape(x, depth=0, max_depth=4):
    """Type skeleton of a JSON value (first element of lists)."""
    if depth > max_depth:
        return "..."
    if isinstance(x, dict):
        return {k: shape(v, depth + 1) for k, v in x.items()}
    if isinstance(x, list):
        return [shape(x[0], depth + 1)] if x else []
    return type(x).__name__ if x is not None else "null"


def main():
    key = load_api_key()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = ROOT / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    working = None
    print("== GET /markets?limit=1 against candidate base URLs ==")
    for base in CANDIDATE_BASE_URLS:
        c = SigClient(base, key, max_retries=2)
        try:
            c.get("/markets", limit=1)
            print(f"  OK    {base}")
            working = working or (base, c)
        except SigAPIError as e:
            print(f"  FAIL  {base}  -> {e.status} {e.code}: {e.message}")
        except Exception as e:  # network-level
            print(f"  FAIL  {base}  -> {type(e).__name__}: {e}")

    if not working:
        raise SystemExit("No base URL accepted the key.")
    base, c = working
    print(f"\nUsing {base}\n")

    page = c.get("/markets", limit=5)
    (raw_dir / f"markets_page_{ts}.json").write_text(json.dumps(page, indent=2))
    print("== GET /markets?limit=5 — type shape ==")
    print(json.dumps(shape(page), indent=2))
    print("\n== first market (raw) ==")
    print(json.dumps(page.get("data", [None])[0], indent=2))
    print("\n== pagination (raw) ==")
    print(json.dumps(page.get("pagination"), indent=2))
    print(f"\nRaw files saved under {raw_dir}")


if __name__ == "__main__":
    main()
