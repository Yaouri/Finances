"""Command line entry point: `uv run fin --help`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser

from . import db
from .config import get_settings


def _con():
    return db.connect(get_settings().db_path)


def cmd_eb_status(args) -> None:
    from .ingest.enablebanking.consent import consent_status
    from .ingest.enablebanking.sync import make_client

    with make_client(get_settings()) as c:
        app = c.application()
    print(f"app: {app['name']}  env={app['environment']}  active={app['active']}")
    print(f"redirect_urls: {', '.join(app['redirect_urls'])}")
    if not app["active"]:
        print("\n! App is inactive: Control Panel -> your app -> 'Activate by linking accounts'.")
    rows = consent_status(_con())
    print("\nconsents:" if rows else "\nconsents: none yet — run `fin eb connect <bank>`")
    for r in rows:
        print(f"  {r['bank']:<28} {r['state']:<9} until {r['valid_until']:%Y-%m-%d} ({r['days_left']:.0f} days)")


def cmd_eb_banks(args) -> None:
    from .ingest.enablebanking.sync import make_client

    with make_client(get_settings()) as c:
        for a in c.aspsps(args.country):
            if args.search and args.search.lower() not in a["name"].lower():
                continue
            days = int(a.get("maximum_consent_validity") or 0) // 86400
            print(f"{a['name']:<40} max_consent={days}d psu={a.get('psu_types')} "
                  f"headers={a.get('required_psu_headers') or []}{' BETA' if a.get('beta') else ''}")


def cmd_eb_connect(args) -> None:
    from .ingest.enablebanking import consent
    from .ingest.enablebanking.sync import make_client

    s = get_settings()
    with make_client(s) as c:
        url = consent.start(_con(), c, args.bank, args.country, s.eb_redirect_url, args.days)
    print("Open this URL and authorise at your bank:\n")
    print(url)
    print(
        "\nAfter approving, your browser lands on "
        f"{s.eb_redirect_url}?code=... (the page itself may fail to load — that's expected)."
        "\nCopy the full URL from the address bar and run:\n\n  uv run fin eb finish \"<pasted url>\"\n"
    )
    if not args.no_browser:
        webbrowser.open(url)


def cmd_eb_finish(args) -> None:
    from .ingest.enablebanking import consent
    from .ingest.enablebanking.sync import make_client

    with make_client(get_settings()) as c:
        linked = consent.finish(_con(), c, args.url)
    print(f"Linked {len(linked)} account(s):")
    for a in linked:
        r = a["raw"]
        print(f"  {a['name']:<30} {a['key']:<40} type={r.get('cash_account_type')} "
              f"product={r.get('product')!r} ccy={r.get('currency')}")


def cmd_accounts(args) -> None:
    con = _con()
    if args.raw:
        for r in con.execute("SELECT id, name, eb_raw FROM accounts ORDER BY id"):
            print(f"# {r['id']} {r['name']}\n{json.dumps(json.loads(r['eb_raw'] or '{}'), indent=2)}")
        return
    rows = con.execute(
        """SELECT a.*, (SELECT count(*) FROM transactions t WHERE t.account_id=a.id AND t.duplicate_of IS NULL) n,
                  (SELECT min(booking_date) FROM transactions t WHERE t.account_id=a.id) first_tx
           FROM accounts a ORDER BY a.id"""
    ).fetchall()
    for r in rows:
        print(f"{r['id']:>3}  {r['name']:<28} {r['institution']:<20} {r['kind']:<8} "
              f"joint={r['is_joint']} share={r['my_share']:.2f} txns={r['n']:<5} since={r['first_tx'] or '-'} "
              f"synced={r['last_synced_at'] or 'never'}")


def cmd_accounts_set(args) -> None:
    con = _con()
    updates = {k: v for k, v in {"name": args.name, "kind": args.kind, "my_share": args.share}.items() if v is not None}
    if args.joint is not None:
        updates["is_joint"] = int(args.joint)
    if not updates:
        sys.exit("nothing to update")
    con.execute(f"UPDATE accounts SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?", (*updates.values(), args.id))
    con.commit()
    cmd_accounts(argparse.Namespace(raw=False))


def cmd_sync(args) -> None:
    from .ingest.enablebanking.sync import sync_all

    for r in sync_all(_con(), get_settings(), force=args.force):
        print(f"{r.account:<28} {r.status:<8} fetched={r.fetched} new={r.new} dupes={r.duplicates} {r.message}")


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="fin")
    sub = p.add_subparsers(required=True)

    eb = sub.add_parser("eb", help="Enable Banking").add_subparsers(required=True)
    eb.add_parser("status", help="app state and consent expiry").set_defaults(fn=cmd_eb_status)
    b = eb.add_parser("banks", help="list banks (ASPSPs)")
    b.add_argument("--country", default="PT")
    b.add_argument("--search")
    b.set_defaults(fn=cmd_eb_banks)
    c = eb.add_parser("connect", help="start consent for a bank")
    c.add_argument("bank")
    c.add_argument("--country", default="PT")
    c.add_argument("--days", type=int, help="cap consent length (default: bank maximum)")
    c.add_argument("--no-browser", action="store_true")
    c.set_defaults(fn=cmd_eb_connect)
    f = eb.add_parser("finish", help="complete consent with the redirected URL")
    f.add_argument("url")
    f.set_defaults(fn=cmd_eb_finish)

    a = sub.add_parser("accounts", help="list accounts")
    a.add_argument("--raw", action="store_true", help="dump the bank's account resources")
    a.set_defaults(fn=cmd_accounts)
    s = sub.add_parser("account-set", help="edit an account")
    s.add_argument("id", type=int)
    s.add_argument("--name")
    s.add_argument("--kind", choices=["current", "savings", "joint", "meal", "investment"])
    s.add_argument("--joint", type=lambda v: v.lower() in ("1", "true", "yes"))
    s.add_argument("--share", type=float)
    s.set_defaults(fn=cmd_accounts_set)

    y = sub.add_parser("sync", help="refresh balances and transactions")
    y.add_argument("--force", action="store_true")
    y.set_defaults(fn=cmd_sync)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
