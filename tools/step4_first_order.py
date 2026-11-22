"""Step 4: place ONE small resting limit order (dry-run unless --live), check it, cancel on request.

  python tools/step4_first_order.py place                 # dry-run: shows the exact payload, sends nothing
  python tools/step4_first_order.py place --live          # actually POST /orders, then show status
  python tools/step4_first_order.py status <orderId>
  python tools/step4_first_order.py cancel <orderId>
  python tools/step4_first_order.py open                  # list my open orders in the tournament

The order is priced `--offset` below the touch on its own side so it rests instead of filling.
Every live placement is recorded to data/orders/ with its idempotency key.
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root: sigbot/ and the top-level bots
import argparse
import csv
import glob
import json
import math
import sys
from datetime import datetime, timezone

from sigbot.client import ROOT, SigAPIError, SigClient, new_idempotency_key
from sigbot.markets import TOURNAMENT_SLUG, book_stats, fetch_markets

TICK = 0.005
MIN_PX, MAX_PX = 0.005, 0.995
MAX_QTY = 100          # hard safety cap for this script
MIN_GAP_FROM_TOUCH = 0.05  # refuse to send if the price is within this of crossing
ORDERS_DIR = ROOT / "data" / "orders"


def floor_tick(p: float) -> float:
    return round(max(MIN_PX, min(MAX_PX, math.floor(round(p / TICK, 6)) * TICK)), 3)


def tournament_id(c: SigClient) -> str:
    # Resolved from market contexts (same as step 2); cheap enough to repeat.
    return fetch_markets(c)[0].id


def default_exchange() -> tuple[str, str]:
    """Two-sided market from the latest snapshot with mid closest to 0.5."""
    snaps = sorted(glob.glob(str(ROOT / "data" / "snapshots" / "markets_*.csv")))
    if not snaps:
        raise SystemExit("No snapshot found; run tools/step2_snapshot.py or pass --exchange-id.")
    with open(snaps[-1], encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["mid"]]
    r = min(rows, key=lambda r: abs(float(r["mid"]) - 0.5))
    return r["exchange_id"], r["title"]


def show_order(c: SigClient, order_id) -> dict:
    o = c.get(f"/orders/{order_id}")
    print(json.dumps(o, indent=2))
    return o


def cmd_place(c: SigClient, args):
    if args.qty <= 0 or args.qty > MAX_QTY:
        raise SystemExit(f"--qty must be 1..{MAX_QTY}")
    tid = tournament_id(c)
    ex_id, title = (args.exchange_id, None) if args.exchange_id else default_exchange()

    book = book_stats(c.get(f"/exchanges/{ex_id}/orderbook", tournamentId=tid, depth=200))
    # Book is YES-normalised. Touch on our side: YES bid for buy-yes; NO bid (= 1 - YES ask) for buy-no.
    if args.side == "yes":
        touch, opposite = book["best_bid"], book["best_ask"]
    else:
        touch = round(1 - book["best_ask"], 3) if book["best_ask"] is not None else None
        opposite = round(1 - book["best_bid"], 3) if book["best_bid"] is not None else None
    ref = touch if touch is not None else (opposite - 0.10 if opposite is not None else 0.5)
    price = args.price if args.price is not None else floor_tick(ref - args.offset)
    if abs(price / TICK - round(price / TICK)) > 1e-9 or not MIN_PX <= price <= MAX_PX:
        raise SystemExit(f"price {price} is off-tick or outside [{MIN_PX}, {MAX_PX}]")
    if opposite is not None and price > opposite - MIN_GAP_FROM_TOUCH:
        raise SystemExit(f"price {price} is within {MIN_GAP_FROM_TOUCH} of the ask {opposite:.3f}; would not rest safely")

    payload = {"idempotencyKey": new_idempotency_key(), "exchangeId": str(ex_id), "side": args.side,
               "action": "buy", "quantity": args.qty, "price": price, "tournamentId": tid}

    print(f"Market:    {title or '(exchange ' + str(ex_id) + ')'}")
    print(f"Book (YES): bid {book['best_bid']} x {book['bid_size']}  /  ask {book['best_ask']} x {book['ask_size']}")
    print(f"Order:     BUY {args.qty} {args.side.upper()} @ {price}  (touch {touch}, {args.offset} away)"
          f"  max cost {args.qty * price:.2f} SUSQies")
    print("Payload:\n" + json.dumps(payload, indent=2))

    if not args.live:
        print("\nDRY RUN - nothing sent. Re-run with --live to place it.")
        return

    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    rec_path = ORDERS_DIR / f"order_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    record = {"payload": payload, "sentAt": datetime.now(timezone.utc).isoformat()}
    rec_path.write_text(json.dumps(record, indent=2))  # written before sending so the key survives a crash
    try:
        resp = c.post("/orders", payload)  # client retries 502/503/409-in-flight with the same key
    except SigAPIError as e:
        record["error"] = {"status": e.status, "code": e.code, "message": e.message, "details": e.details}
        rec_path.write_text(json.dumps(record, indent=2))
        print(f"\nORDER REJECTED: {e.status} {e.code}: {e.message}")
        if e.code == "TERMS_NOT_ACKNOWLEDGED":
            print("You need to accept these documents yourself on the website before trading:")
            for d in (e.details or {}).get("missingDocuments", []):
                print(f"  - {d.get('kind')} v{d.get('version')} ({d.get('organizationName')}): {d.get('url')}")
        elif e.details:
            print(json.dumps(e.details, indent=2))
        sys.exit(1)

    record["response"] = resp
    rec_path.write_text(json.dumps(record, indent=2))
    print("\nPOST /orders response:\n" + json.dumps(resp, indent=2))
    oid = resp.get("orderId")
    if oid is None:
        return
    print(f"\nGET /orders/{oid}:")
    o = show_order(c, oid)
    print(f"\nRecorded to {rec_path.relative_to(ROOT)}")
    if o.get("open") and sys.stdin.isatty():
        if input(f"Cancel order {oid} now? [y/N] ").strip().lower() == "y":
            cmd_cancel(c, argparse.Namespace(order_id=oid))
    elif o.get("open"):
        print(f"To cancel:  python tools/step4_first_order.py cancel {oid}")


def cmd_status(c: SigClient, args):
    show_order(c, args.order_id)


def cmd_cancel(c: SigClient, args):
    try:
        resp = c.delete(f"/orders/{args.order_id}")
    except SigAPIError as e:
        raise SystemExit(f"Cancel failed: {e}")
    print("DELETE response:\n" + json.dumps(resp, indent=2))
    print("\nStatus after cancel:")
    show_order(c, args.order_id)


def cmd_open(c: SigClient, args):
    tid = tournament_id(c)
    orders = list(c.paginate("/orders", status="open", tournamentId=tid))
    print(f"{len(orders)} open order(s) in {TOURNAMENT_SLUG}")
    for o in orders:
        print(json.dumps(o))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("place")
    p.add_argument("--live", action="store_true", help="actually send the order (default: dry-run)")
    p.add_argument("--exchange-id", help="default: two-sided market with mid nearest 0.5 from latest snapshot")
    p.add_argument("--side", choices=["yes", "no"], default="yes")
    p.add_argument("--qty", type=int, default=10)
    p.add_argument("--offset", type=float, default=0.20, help="distance below our-side touch (default 0.20)")
    p.add_argument("--price", type=float, help="explicit limit price (overrides --offset)")
    for name in ("status", "cancel"):
        s = sub.add_parser(name)
        s.add_argument("order_id")
    sub.add_parser("open")
    args = ap.parse_args()

    c = SigClient()
    {"place": cmd_place, "status": cmd_status, "cancel": cmd_cancel, "open": cmd_open}[args.cmd](c, args)


if __name__ == "__main__":
    main()
