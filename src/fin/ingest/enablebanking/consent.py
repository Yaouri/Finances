"""PSD2 consent lifecycle: start -> (bank SCA in browser) -> finish -> accounts upserted.

The redirect target does not need to be served by anything: after SCA the browser lands on
the registered redirect URL with ?code=...&state=...; pasting that URL into `finish` (CLI or
Settings page) completes the flow. If the app is served on that URL, it is caught directly.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from .client import EnableBankingClient

WARN_DAYS = 14


class ConsentError(RuntimeError):
    pass


def parse_callback(url_or_code: str) -> tuple[str, str | None]:
    """Return (code, state) from a pasted redirect URL, or (code, None) for a bare code."""
    s = url_or_code.strip()
    if "://" not in s and "?" not in s:
        return s, None
    qs = parse_qs(urlparse(s).query)
    if "error" in qs:
        raise ConsentError(f"bank returned error: {qs['error'][0]} {qs.get('error_description', [''])[0]}")
    if "code" not in qs:
        raise ConsentError("no ?code= in the pasted URL")
    return qs["code"][0], qs.get("state", [None])[0]


def find_aspsp(client: EnableBankingClient, name: str, country: str) -> dict:
    aspsps = client.aspsps(country)
    exact = [a for a in aspsps if a["name"].lower() == name.lower()]
    if exact:
        return exact[0]
    partial = [a for a in aspsps if name.lower() in a["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(a["name"] for a in (partial or aspsps))
    raise ConsentError(f"bank '{name}' not unique/not found in {country}. Candidates: {names}")


def start(
    con: sqlite3.Connection,
    client: EnableBankingClient,
    bank: str,
    country: str,
    redirect_url: str,
    max_days: int | None = None,
) -> str:
    aspsp = find_aspsp(client, bank, country)
    seconds = int(aspsp.get("maximum_consent_validity") or 90 * 86400)
    if max_days:
        seconds = min(seconds, max_days * 86400)
    # a small margin so the bank never sees a request beyond its maximum
    valid_until = (datetime.now(UTC) + timedelta(seconds=seconds) - timedelta(minutes=10)).isoformat()
    state = secrets.token_urlsafe(24)
    resp = client.start_auth(aspsp["name"], aspsp["country"], valid_until, redirect_url, state)
    con.execute(
        "INSERT INTO eb_pending_auth (state, aspsp_name, aspsp_country, valid_until, auth_url) VALUES (?,?,?,?,?)",
        (state, aspsp["name"], aspsp["country"], valid_until, resp["url"]),
    )
    con.commit()
    return resp["url"]


def account_key(acc: dict) -> str:
    iban = ((acc.get("account_id") or {}).get("iban") or "").replace(" ", "").upper()
    return f"iban:{iban}" if iban else f"eb:{acc['identification_hash']}"


def _guess_kind(acc: dict) -> str:
    text = f"{acc.get('name') or ''} {acc.get('product') or ''} {acc.get('details') or ''}".lower()
    if acc.get("cash_account_type") == "SVGS" or "saving" in text or "poupan" in text:
        return "savings"
    if "joint" in text or "conjunta" in text:
        return "joint"
    return "current"


def finish(con: sqlite3.Connection, client: EnableBankingClient, url_or_code: str) -> list[dict]:
    code, state = parse_callback(url_or_code)
    if state is not None:
        pending = con.execute("SELECT * FROM eb_pending_auth WHERE state = ?", (state,)).fetchone()
        if pending is None:
            raise ConsentError("unknown state — start the connection again (possible stale or forged redirect)")
    else:
        pending = con.execute("SELECT * FROM eb_pending_auth ORDER BY created_at DESC LIMIT 1").fetchone()
        if pending is None:
            raise ConsentError("no pending connection; run connect first")

    sess = client.create_session(code)
    session_id = sess["session_id"]
    valid_until = (sess.get("access") or {}).get("valid_until") or pending["valid_until"]
    aspsp = sess.get("aspsp") or {"name": pending["aspsp_name"], "country": pending["aspsp_country"]}

    # superseded sessions for the same bank are kept for audit but marked revoked
    con.execute(
        "UPDATE eb_sessions SET revoked_at = datetime('now') WHERE aspsp_name = ? AND revoked_at IS NULL",
        (aspsp["name"],),
    )
    con.execute(
        "INSERT INTO eb_sessions (session_id, aspsp_name, aspsp_country, status, valid_until) VALUES (?,?,?,?,?)",
        (session_id, aspsp["name"], aspsp["country"], "AUTHORIZED", valid_until),
    )

    linked = []
    for acc in sess.get("accounts", []):
        key = account_key(acc)
        iban = key[5:] if key.startswith("iban:") else None
        name = acc.get("name") or acc.get("product") or acc.get("details") or iban or key[-8:]
        existing = con.execute("SELECT id FROM accounts WHERE key = ?", (key,)).fetchone()
        if existing:
            # keep user-edited name/kind/share; only refresh the session binding
            con.execute(
                "UPDATE accounts SET eb_uid=?, eb_session_id=?, eb_raw=?, active=1 WHERE id=?",
                (acc.get("uid"), session_id, json.dumps(acc), existing["id"]),
            )
        else:
            kind = _guess_kind(acc)
            con.execute(
                """INSERT INTO accounts (key, name, institution, iban, currency, kind, is_joint, my_share,
                                         source, eb_uid, eb_session_id, eb_raw)
                   VALUES (?,?,?,?,?,?,?,?, 'eb', ?,?,?)""",
                (key, name, aspsp["name"], iban, acc.get("currency") or "EUR", kind,
                 int(kind == "joint"), 0.5 if kind == "joint" else 1.0,
                 acc.get("uid"), session_id, json.dumps(acc)),
            )
        if iban:
            con.execute("INSERT OR IGNORE INTO own_ibans (iban, label) VALUES (?, ?)", (iban, name))
        linked.append({"key": key, "name": name, "uid": acc.get("uid"), "raw": acc})

    con.execute("DELETE FROM eb_pending_auth WHERE state = ?", (pending["state"],))
    con.commit()
    return linked


def consent_status(con: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """One row per bank with its active consent and how many days remain."""
    now = now or datetime.now(UTC)
    out = []
    for r in con.execute(
        "SELECT * FROM eb_sessions WHERE revoked_at IS NULL ORDER BY aspsp_name"
    ).fetchall():
        until = datetime.fromisoformat(r["valid_until"].replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        days = (until - now).total_seconds() / 86400
        out.append(
            {
                "bank": r["aspsp_name"],
                "session_id": r["session_id"],
                "valid_until": until,
                "days_left": days,
                "state": "expired" if days <= 0 else "expiring" if days <= WARN_DAYS else "ok",
            }
        )
    return out
