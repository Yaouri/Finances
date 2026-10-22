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
    if args.qr:
        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.print_ascii(invert=True)
        print("Scan with your phone and approve in the bank's app.\n")
    else:
        print("Open this URL and authorise at your bank:\n")
    print(url)
    print(
        "\nAfter approving, the browser lands on "
        f"{s.eb_redirect_url}?code=... (the page itself fails to load — that's expected)."
        "\nCopy the full URL from the address bar (on a phone: share it to yourself) and run:"
        "\n\n  uv run fin eb finish \"<pasted url>\"\n"
    )
    if not args.no_browser and not args.qr:
        webbrowser.open(url)


def cmd_eb_finish(args) -> None:
    from .ingest.enablebanking import consent
    from .ingest.enablebanking.sync import make_client

    with make_client(get_settings()) as c:
        linked = consent.finish(_con(), c, args.url, bank=args.bank)
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


def cmd_account_add(args) -> None:
    con = _con()
    slug = "".join(ch if ch.isalnum() else "-" for ch in f"{args.institution}-{args.name}".lower())
    con.execute(
        """INSERT INTO accounts (key, name, institution, currency, kind, is_joint, my_share, source)
           VALUES (?,?,?,?,?,?,?, 'manual')""",
        (f"manual:{slug}", args.name, args.institution, args.currency, args.kind,
         int(args.kind == "joint"), 0.5 if args.kind == "joint" else 1.0),
    )
    con.commit()
    cmd_accounts(argparse.Namespace(raw=False))


def cmd_balance_set(args) -> None:
    from datetime import date

    from .ingest.model import to_cents

    con = _con()
    acc = con.execute("SELECT currency FROM accounts WHERE id = ?", (args.id,)).fetchone()
    if acc is None:
        sys.exit(f"no account {args.id}")
    con.execute(
        """INSERT INTO balances (account_id, as_of, balance_type, amount_cents, currency)
           VALUES (?,?, 'MANUAL', ?, ?)
           ON CONFLICT(account_id, as_of, balance_type) DO UPDATE SET amount_cents = excluded.amount_cents""",
        (args.id, args.date or date.today().isoformat(), to_cents(args.amount), acc["currency"]),
    )
    con.commit()
    print("ok")


def cmd_import(args) -> None:
    from pathlib import Path

    from .ingest import importers

    con = _con()
    for f in args.files:
        path = Path(f)
        fmt = importers.detect(path)
        if args.dry_run:
            res = fmt.parse(path)
            print(f"{path.name}: {res.source}")
            for b in res.batches:
                d = [t.booking_date for t in b.txns]
                print(f"  {b.role:<8} iban={b.iban or '-'} rows={len(b.txns)} {min(d)}..{max(d)} "
                      f"net={sum(t.amount_cents for t in b.txns) / 100:,.2f} balances={b.balances} "
                      f"{b.extra or ''}")
            continue
        print(f"{path.name}:")
        for o in importers.import_file(con, path, args.account):
            print(f"  -> {o.account:<24} rows={o.fetched} new={o.new} already={o.existing} matched_other_source={o.duplicates}")


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
    c.add_argument("--qr", action="store_true", help="print a QR code to open the link on your phone")
    c.set_defaults(fn=cmd_eb_connect)
    f = eb.add_parser("finish", help="complete consent with the redirected URL (or bare code)")
    f.add_argument("url")
    f.add_argument("--bank", help="disambiguate a bare code when several banks are pending")
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
    n = sub.add_parser("account-add", help="add a manual account (e.g. one PSD2 doesn't expose)")
    n.add_argument("--name", required=True)
    n.add_argument("--institution", required=True)
    n.add_argument("--kind", default="current", choices=["current", "savings", "joint", "meal", "investment"])
    n.add_argument("--currency", default="EUR")
    n.set_defaults(fn=cmd_account_add)
    m = sub.add_parser("balance-set", help="record a balance by hand")
    m.add_argument("id", type=int)
    m.add_argument("amount")
    m.add_argument("--date", help="YYYY-MM-DD (default today)")
    m.set_defaults(fn=cmd_balance_set)

    i = sub.add_parser("import", help="import statement files (Revolut CSV, Millennium PDF, Santander meal XLS)")
    i.add_argument("files", nargs="+")
    i.add_argument("--account", type=int, help="target account for the main batch (default: auto)")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(fn=cmd_import)

    y = sub.add_parser("sync", help="refresh balances and transactions")
    y.add_argument("--force", action="store_true")
    y.set_defaults(fn=cmd_sync)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
