"""User-triggered refresh: balances + incremental booked transactions for every linked account."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from ...config import Settings
from ...db import store_raw
from ..dedupe import insert_transactions
from .client import USER_AGENT, EnableBankingClient, EnableBankingError
from .consent import consent_status
from .mapping import map_balances, map_transaction

log = logging.getLogger(__name__)

OVERLAP_DAYS = 7          # re-fetch recent days: late-posting and pending->booked transitions
MIN_INTERVAL = timedelta(minutes=15)  # protects the bank's per-day access budget


@dataclass
class AccountSyncResult:
    account: str
    status: str
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    message: str = ""


def psu_headers(lookup_ip: bool = True) -> dict[str, str]:
    """Headers that mark the request as customer-present (the user clicked Refresh).
    The public IP lookup sends nothing but a bare GET to ipify."""
    h = {"Psu-User-Agent": USER_AGENT}
    if lookup_ip:
        try:
            h["Psu-Ip-Address"] = httpx.get("https://api.ipify.org", timeout=5).text.strip()
        except httpx.HTTPError:
            pass
    return h


def make_client(settings: Settings, psu: bool = False) -> EnableBankingClient:
    if settings.eb_app_id is None:
        raise RuntimeError("EB_APP_ID is not set (see .env.example)")
    return EnableBankingClient(
        settings.eb_app_id.get_secret_value(),
        settings.key_path,
        settings.eb_api_url,
        psu_headers=psu_headers() if psu else None,
    )


def _date_from(con: sqlite3.Connection, account_id: int) -> date | None:
    (last,) = con.execute(
        "SELECT max(booking_date) FROM transactions WHERE account_id = ? AND source = 'eb'", (account_id,)
    ).fetchone()
    return date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS) if last else None


def sync_account(con: sqlite3.Connection, client: EnableBankingClient, acc: sqlite3.Row) -> AccountSyncResult:
    res = AccountSyncResult(account=acc["name"], status="ok")
    date_from = _date_from(con, acc["id"])
    run_id = con.execute(
        "INSERT INTO sync_runs (source, account_id, date_from) VALUES ('eb', ?, ?)",
        (acc["id"], date_from.isoformat() if date_from else None),
    ).lastrowid
    con.commit()
    try:
        bal = client.balances(acc["eb_uid"])
        store_raw(con, "balances", bal, sync_run_id=run_id, account_id=acc["id"])
        for b in map_balances(bal, date.today()):
            con.execute(
                """INSERT INTO balances (account_id, as_of, balance_type, amount_cents, currency)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(account_id, as_of, balance_type)
                   DO UPDATE SET amount_cents = excluded.amount_cents, fetched_at = datetime('now')""",
                (acc["id"], b["as_of"], b["balance_type"], b["amount_cents"], b["currency"]),
            )

        # first sync: ask for the longest history the bank allows
        strategy = None if date_from else "longest"
        for page in client.transaction_pages(acc["eb_uid"], date_from=date_from, strategy=strategy):
            raw_id = store_raw(con, "transactions", page, sync_run_id=run_id, account_id=acc["id"])
            txns = [m for t in page.get("transactions", []) if (m := map_transaction(t))]
            st = insert_transactions(con, acc["id"], "eb", txns, raw_payload_id=raw_id)
            res.fetched += st.fetched
            res.new += st.new
            res.duplicates += st.cross_source_dupes

        con.execute("UPDATE accounts SET last_synced_at = datetime('now') WHERE id = ?", (acc["id"],))
        con.execute(
            "UPDATE sync_runs SET status='ok', finished_at=datetime('now'), n_fetched=?, n_new=?, n_duplicate=? WHERE id=?",
            (res.fetched, res.new, res.duplicates, run_id),
        )
        con.commit()
    except EnableBankingError as e:
        con.rollback()
        res.status, res.message = "error", str(e)
        con.execute(
            "UPDATE sync_runs SET status='error', finished_at=datetime('now'), error=? WHERE id=?",
            (str(e)[:2000], run_id),
        )
        con.commit()
    return res


def sync_all(con: sqlite3.Connection, settings: Settings, force: bool = False) -> list[AccountSyncResult]:
    valid_sessions = {s["session_id"] for s in consent_status(con) if s["state"] != "expired"}
    accounts = con.execute(
        "SELECT * FROM accounts WHERE source='eb' AND active=1 AND eb_uid IS NOT NULL ORDER BY id"
    ).fetchall()
    results: list[AccountSyncResult] = []
    now = datetime.now(UTC)
    todo = []
    for acc in accounts:
        if acc["eb_session_id"] not in valid_sessions:
            results.append(AccountSyncResult(acc["name"], "skipped", message="consent expired — reconnect"))
            continue
        if not force and acc["last_synced_at"]:
            last = datetime.fromisoformat(acc["last_synced_at"]).replace(tzinfo=UTC)
            if now - last < MIN_INTERVAL:
                results.append(AccountSyncResult(acc["name"], "skipped", message="synced recently"))
                continue
        todo.append(acc)
    if todo:
        with make_client(settings, psu=True) as client:
            for acc in todo:
                results.append(sync_account(con, client, acc))
    return results
