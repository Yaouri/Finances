"""Command line entry point: `uv run fin --help`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from pathlib import Path

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
    from .invest import degiro_account, lots

    con = _con()
    for f in args.files:
        path = Path(f)
        if degiro_account.detect(path):
            print(f"{path.name}: {degiro_account.import_account(con, path)}")
            continue
        if lots.detect(path):
            if args.dry_run:
                rows = lots.parse_degiro(path)
                print(f"{path.name}: degiro lots rows={len(rows)} transfers_in={sum(r['kind'] == 'transfer_in' for r in rows)}")
            else:
                print(f"{path.name}: {lots.import_degiro(con, path)}")
            continue
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


def cmd_reprocess(args) -> None:
    from .pipeline import reprocess, reseed

    con = _con()
    if args.reseed:
        reseed(con)
    st = reprocess(con)
    print(f"{st.total} txns: manual={st.manual} transfer={st.transfer} rule={st.rule} ml={st.ml} "
          f"uncategorised={st.uncategorised} ({st.uncategorised / max(st.total, 1):.0%})")


def cmd_txns(args) -> None:
    con = _con()
    where, params = ["t.duplicate_of IS NULL"], []
    if args.account:
        where.append("t.account_id = ?")
        params.append(args.account)
    if args.uncategorised:
        where.append("e.category_id IS NULL")
    if args.search:
        where.append("(t.description LIKE ? OR m.name LIKE ? OR c.path LIKE ?)")
        params += [f"%{args.search}%"] * 3
    if args.flow:
        where.append("e.flow = ?")
        params.append(args.flow)
    rows = con.execute(
        f"""SELECT t.id, t.booking_date, a.name acc, t.amount_cents, t.description, e.descriptor_type dtype,
                   m.name merchant, c.path category, e.category_source src, e.transfer_group grp
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            LEFT JOIN tx_enriched e ON e.tx_id = t.id LEFT JOIN merchants m ON m.id = e.merchant_id
            LEFT JOIN categories c ON c.id = e.category_id
            WHERE {' AND '.join(where)} ORDER BY t.booking_date DESC, t.id DESC LIMIT ?""",
        (*params, args.limit),
    ).fetchall()
    for r in rows:
        print(f"{r['id']:>5} {r['booking_date']} {r['acc'][:16]:<16} {r['amount_cents'] / 100:>10.2f}  "
              f"{r['description'][:44]:<44} {r['dtype'] or '':<14} {(r['merchant'] or '')[:20]:<20} "
              f"{r['category'] or '-'} [{r['src'] or ''}{' ' + r['grp'] if r['grp'] else ''}]")


def cmd_summary(args) -> None:
    con = _con()
    rows = con.execute(
        """SELECT substr(t.booking_date, 1, 7) month,
                  CASE WHEN EXISTS (SELECT 1 FROM tx_tags x JOIN tags g ON g.id = x.tag_id
                                    WHERE x.tx_id = t.id AND g.name = 'one-off') THEN 'one-off' ELSE e.flow END flow,
                  sum(e.my_amount_eur_cents) cents
           FROM transactions t JOIN tx_enriched e ON e.tx_id = t.id
           WHERE t.duplicate_of IS NULL GROUP BY 1, 2 ORDER BY 1"""
    ).fetchall()
    months: dict[str, dict] = {}
    for r in rows:
        months.setdefault(r["month"], {})[r["flow"]] = (r["cents"] or 0) / 100
    print(f"{'month':<8} {'income':>10} {'expense':>10} {'net':>10} {'rate':>6} {'invest':>11} {'one-off':>10}")
    for m, f in list(months.items())[-args.months:]:
        inc, exp = f.get("income", 0), f.get("expense", 0)
        rate = (inc + exp) / inc if inc else 0
        print(f"{m:<8} {inc:>10.2f} {exp:>10.2f} {inc + exp:>10.2f} {rate:>6.0%} "
              f"{f.get('investing', 0):>11.2f} {f.get('one-off', 0):>10.2f}")


def cmd_invest(args) -> None:
    from .invest import lots, portfolio, prices

    con = _con()
    if args.action == "refresh":
        print(prices.refresh(con))
    elif args.action == "lots":
        for r in con.execute("SELECT * FROM lots ORDER BY trade_date, id"):
            flag = " SUPERSEDED" if r["superseded"] else (" [transfer-in]" if r["kind"] == "transfer_in" else "")
            print(f"{r['id']:>4} {r['trade_date']} {r['broker']:<14} {r['ticker']:<8} {r['quantity']:>8g} "
                  f"@ {r['price']:>9.4f} {r['currency']} fees={r['fees']:.2f} total={r['total_eur']:>10.2f}{flag}")
    elif args.action == "add":
        print("lot", lots.add_lot(con, trade_date=args.date, isin=args.isin, quantity=args.qty, price=args.price,
                                  fees=args.fees, broker=args.broker, note=args.note))
    elif args.action == "supersede":
        con.execute("UPDATE lots SET superseded = 1, locked = 1 WHERE id = ?", (args.id,))
        con.commit()
    s = portfolio.summary(con)
    print(f"\nvalue {s['value_eur']:,.2f}  invested {s['invested_eur']:,.2f}  unrealised {s['unrealised_eur']:+,.2f} "
          f"({s['unrealised_pct']:+.1%})  realised {s['realised_eur']:+,.2f}  fees {s['fees_eur']:,.2f}")
    for p in s["positions"]:
        print(f"  {p.name:<32} {p.quantity:>8g} @ {p.price or 0:>8.2f} ({p.price_date})  value {p.value_eur or 0:>11,.2f}  "
              f"cost {p.cost_eur:>11,.2f}  {p.unrealised_eur or 0:+,.2f}")
    if s["has_transfer_placeholder"]:
        print("\n! An in-specie transfer still uses market value as cost. Add the original lots "
              "(fin invest add ...) and then `fin invest supersede <transfer lot id>`.")


def cmd_categorize(args) -> None:
    from .categorize.manual import set_category
    from .pipeline import reprocess

    con = _con()
    rule = set_category(con, args.id, args.category, note=args.note, tags=tuple(args.tag or ()),
                        my_share=args.share, make_rule=args.rule)
    reprocess(con)
    print("ok" + (f" (rule {rule} added)" if rule else ""))


def cmd_check(args) -> None:
    from .quality import balance_gaps

    gaps = balance_gaps(_con())
    if not gaps:
        print("all balances reconcile")
    for g in gaps:
        print(f"{g.account:<24} {g.from_date} -> {g.to_date}  unexplained {g.delta_cents / 100:+,.2f}")


def cmd_refresh(args) -> None:
    from .service import refresh

    res = refresh(_con(), get_settings(), force=args.force, banks=not args.no_banks)
    for r in res.banks:
        print(f"{r.account:<28} {r.status:<8} new={r.new} dupes={r.duplicates} {r.message}")
    print(f"prices: {res.prices}")
    st = res.stats
    print(f"categorised {st.total - st.uncategorised}/{st.total} (ml={st.ml}) · new alerts: {res.new_alerts}")
    for e in res.errors:
        print("ERROR", e)


def cmd_report(args) -> None:
    from datetime import date

    import pandas as pd

    from . import metrics, report

    today = date.today()
    start = (pd.Timestamp(today.replace(day=1)) - pd.DateOffset(months=args.months - 1)).date()
    html = report.build(_con(), metrics.Filters(start=start, end=today), cdn=args.cdn)
    out = Path(args.out or get_settings().data_dir / "reports" / f"fin-{today}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


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

    rp = sub.add_parser("reprocess", help="re-run parsing, transfers and categorisation")
    rp.add_argument("--reseed", action="store_true", help="reload default taxonomy/rules (keeps your edits)")
    rp.set_defaults(fn=cmd_reprocess)
    t = sub.add_parser("txns", help="list transactions")
    t.add_argument("--account", type=int)
    t.add_argument("--uncategorised", action="store_true")
    t.add_argument("--search")
    t.add_argument("--flow", choices=["expense", "income", "transfer", "investing"])
    t.add_argument("--limit", type=int, default=40)
    t.set_defaults(fn=cmd_txns)
    u = sub.add_parser("summary", help="monthly income / expense / savings rate")
    u.add_argument("--months", type=int, default=12)
    u.set_defaults(fn=cmd_summary)

    sub.add_parser("check", help="reconcile balances against transactions").set_defaults(fn=cmd_check)

    cz = sub.add_parser("categorize", help="set a transaction's category / tags / note")
    cz.add_argument("id", type=int)
    cz.add_argument("category", nargs="?", help='e.g. "Food > Groceries"')
    cz.add_argument("--tag", action="append", help="repeatable; 'one-off' excludes it from savings rate")
    cz.add_argument("--note")
    cz.add_argument("--share", type=float, help="your share (joint account)")
    cz.add_argument("--rule", action="store_true", help="also create a merchant rule")
    cz.set_defaults(fn=cmd_categorize)

    iv = sub.add_parser("invest", help="investments: refresh prices, list/add lots, summary")
    iv.add_argument("action", nargs="?", default="summary", choices=["summary", "refresh", "lots", "add", "supersede"])
    iv.add_argument("--date")
    iv.add_argument("--isin")
    iv.add_argument("--qty", type=float)
    iv.add_argument("--price", type=float)
    iv.add_argument("--fees", type=float, default=0.0)
    iv.add_argument("--broker", default="Millennium BCP")
    iv.add_argument("--note")
    iv.add_argument("--id", type=int)
    iv.set_defaults(fn=cmd_invest)

    rf = sub.add_parser("refresh", help="bank sync + prices + reprocess + alerts (what the app button does)")
    rf.add_argument("--force", action="store_true")
    rf.add_argument("--no-banks", action="store_true", help="skip bank calls (prices/reprocess only)")
    rf.set_defaults(fn=cmd_refresh)

    rp2 = sub.add_parser("report", help="write a self-contained HTML snapshot")
    rp2.add_argument("--out")
    rp2.add_argument("--months", type=int, default=12)
    rp2.add_argument("--cdn", action="store_true", help="smaller file; charts need internet")
    rp2.set_defaults(fn=cmd_report)

    y = sub.add_parser("sync", help="refresh balances and transactions")
    y.add_argument("--force", action="store_true")
    y.set_defaults(fn=cmd_sync)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
