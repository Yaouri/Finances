"""Refresh, bank connections, imports, rules & categories, thresholds, data quality."""

import io
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
import ui

from fin import prefs
from fin.categorize import taxonomy
from fin.categorize.rules import FIELDS
from fin.config import get_settings
from fin.ingest.enablebanking import consent
from fin.ingest.enablebanking.sync import make_client
from fin.pipeline import reprocess
from fin.quality import balance_gaps
from fin.service import refresh
from views.overview import render_alerts


def _refresh_section():
    c = ui.con()
    st.subheader("Refresh")
    a, b = st.columns([1, 3], vertical_alignment="center")
    force = b.checkbox("Force (ignore the 15-minute guard)", help="Banks allow only a few pulls per day")
    if a.button("Refresh now", type="primary", icon=":material/refresh:", width="stretch"):
        with st.status("Refreshing…", expanded=True) as status:
            res = refresh(c, get_settings(), force=force)
            for r in res.banks:
                st.write(f"{r.account}: {r.status} · {r.new} new {('· ' + r.message) if r.message else ''}")
            st.write(f"Prices: {', '.join(f'{k} ({v})' for k, v in res.prices.items()) or 'none'}")
            st.write(f"Categorised: {res.stats.total - res.stats.uncategorised}/{res.stats.total} · "
                     f"{res.new_alerts} new alert(s)")
            for e in res.errors:
                st.error(e)
            status.update(label="Refresh complete", state="complete" if not res.errors else "error")
        ui.invalidate()
    acc = pd.read_sql_query(
        """SELECT a.name, a.institution, a.source, a.last_synced_at,
                  (SELECT max(booking_date) FROM transactions t WHERE t.account_id = a.id) AS latest_txn
           FROM accounts a WHERE active = 1 ORDER BY id""", c)
    st.dataframe(acc, hide_index=True, width="stretch")


def _banks_section():
    c = ui.con()
    st.subheader("Bank connections")
    rows = consent.consent_status(c)
    if rows:
        st.dataframe(pd.DataFrame([{"bank": r["bank"], "valid until": r["valid_until"].date(),
                                    "days left": round(r["days_left"]), "state": r["state"]} for r in rows]),
                     hide_index=True, width="stretch")
    with st.expander("Connect / renew a bank", icon=":material/link:"):
        st.caption("Opens the bank's own login (on your phone for Millennium). After approving, the browser lands "
                   "on a page that doesn't load — copy that page's address and paste it here.")
        bank = st.selectbox("Bank", ["Millennium BCP", "Revolut"] + ["Other…"])
        if bank == "Other…":
            bank = st.text_input("Bank name as listed by Enable Banking")
        if st.button("Start", disabled=not bank):
            s = get_settings()
            with make_client(s) as cl:
                st.session_state["auth_url"] = consent.start(c, cl, bank, "PT", s.eb_redirect_url)
        url = st.session_state.get("auth_url")
        if url:
            import qrcode

            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            q, l_ = st.columns([1, 3])
            q.image(buf.getvalue(), caption="Scan with your phone", width=180)
            l_.link_button("Open bank login", url)
            l_.code(url, language=None)
        pasted = st.text_input("Paste the redirected URL (or just the code)", key="auth_paste")
        if st.button("Finish connection", type="primary", disabled=not pasted):
            try:
                with make_client(get_settings()) as cl:
                    linked = consent.finish(c, cl, pasted)
                st.success(f"Linked {len(linked)} account(s): {', '.join(a['name'] for a in linked)}")
                st.session_state.pop("auth_url", None)
                ui.invalidate()
            except Exception as e:  # noqa: BLE001
                st.error(str(e))


def _import_section():
    c = ui.con()
    st.subheader("Import files")
    st.caption("Revolut CSV · Millennium 'Extrato combinado' PDF · Santander meal card XLS · DEGIRO Transactions.csv "
               "and Account.csv. Re-importing is safe (duplicates are matched).")
    files = st.file_uploader("Files", accept_multiple_files=True, label_visibility="collapsed")
    if files and st.button("Import", type="primary"):
        from fin.ingest import importers
        from fin.invest import degiro_account, lots

        inbox = get_settings().data_dir / "inbox" / "uploads"
        inbox.mkdir(parents=True, exist_ok=True)
        for up in files:
            path = inbox / Path(up.name).name
            path.write_bytes(up.getvalue())
            try:
                if degiro_account.detect(path):
                    st.write(up.name, degiro_account.import_account(c, path))
                elif lots.detect(path):
                    st.write(up.name, lots.import_degiro(c, path))
                else:
                    for o in importers.import_file(c, path):
                        st.write(f"{up.name} → {o.account}: {o.new} new, {o.existing + o.duplicates} already known")
            except Exception as e:  # noqa: BLE001
                st.error(f"{up.name}: {e}")
        reprocess(c)
        ui.invalidate()

    st.markdown("**Manual balance**")
    acc = pd.read_sql_query("SELECT id, name FROM accounts WHERE source = 'manual' AND active = 1", c)
    a, b, d, e = st.columns([3, 2, 2, 1], vertical_alignment="bottom")
    aid = a.selectbox("Account", acc.id, format_func=dict(zip(acc.id, acc.name)).get)
    amount = b.number_input("Balance (€)", step=10.0, format="%.2f")
    when = d.date_input("As of", value=date.today())
    if e.button("Save", key="bal_save") and aid is not None:
        c.execute("""INSERT INTO balances (account_id, as_of, balance_type, amount_cents, currency) VALUES (?,?, 'MANUAL', ?, 'EUR')
                     ON CONFLICT(account_id, as_of, balance_type) DO UPDATE SET amount_cents = excluded.amount_cents""",
                  (int(aid), when.isoformat(), round(amount * 100)))
        c.commit()
        ui.invalidate()
        st.toast("Balance saved")


def _rules_section():
    c = ui.con()
    st.subheader("Rules")
    st.caption("First match wins, lowest priority number first. Your rules (priority 100) run before the "
               "defaults (1000+). Patterns are case-insensitive regular expressions.")
    paths = taxonomy.path_ids(c)
    by_id = {v: k for k, v in paths.items()}
    df = pd.read_sql_query("""SELECT id, priority, field, pattern, account_kind, sign, min_cents / 100.0 AS min_eur,
                                     max_cents / 100.0 AS max_eur, category_id, enabled, name FROM rules
                              ORDER BY priority, id""", c)
    df["category"] = df.category_id.map(by_id)
    df["enabled"] = df.enabled.astype(bool)
    df = df.drop(columns="category_id")
    edited = st.data_editor(
        df, num_rows="dynamic", hide_index=True, width="stretch", key="rules_editor", height=420,
        column_config={
            "id": st.column_config.NumberColumn(disabled=True, width="small"),
            "field": st.column_config.SelectboxColumn(options=list(FIELDS), required=True),
            "category": st.column_config.SelectboxColumn(options=sorted(paths), required=True, width="large"),
            "sign": st.column_config.SelectboxColumn(options=[-1, 1], help="-1 money out, 1 money in, empty any"),
            "account_kind": st.column_config.SelectboxColumn(options=["current", "savings", "joint", "meal", "investment"]),
            "priority": st.column_config.NumberColumn(default=100),
            "enabled": st.column_config.CheckboxColumn(default=True),
        },
    )
    if st.button("Save rules & reprocess", type="primary"):
        bad = [p for p in edited.pattern.dropna() if not _valid(p)]
        if bad:
            st.error(f"Invalid pattern(s): {bad}")
            return
        keep = set(edited.id.dropna().astype(int))
        for rid in set(df.id) - keep:
            c.execute("DELETE FROM rules WHERE id = ?", (int(rid),))
        for r in edited.itertuples():
            if not r.pattern or not r.category:
                continue
            vals = (int(r.priority or 100), r.field, r.pattern, r.account_kind or None,
                    int(r.sign) if pd.notna(r.sign) else None,
                    round(r.min_eur * 100) if pd.notna(r.min_eur) else None,
                    round(r.max_eur * 100) if pd.notna(r.max_eur) else None,
                    paths[r.category], int(bool(r.enabled)), r.name if pd.notna(r.name) else None)
            if pd.isna(r.id):
                c.execute("""INSERT INTO rules (priority, field, pattern, account_kind, sign, min_cents, max_cents,
                             category_id, enabled, name) VALUES (?,?,?,?,?,?,?,?,?,?)""", vals)
            else:
                c.execute("""UPDATE rules SET priority=?, field=?, pattern=?, account_kind=?, sign=?, min_cents=?,
                             max_cents=?, category_id=?, enabled=?, name=? WHERE id=?""", (*vals, int(r.id)))
        c.commit()
        st.toast(f"Rules saved · {reprocess(c).uncategorised} uncategorised")
        ui.invalidate()


def _valid(p: str) -> bool:
    try:
        re.compile(p)
        return True
    except re.error:
        return False


def _categories_section():
    c = ui.con()
    st.subheader("Categories")
    cats = pd.read_sql_query("SELECT id, path, kind FROM categories ORDER BY kind, path", c)
    st.dataframe(cats.drop(columns="id"), hide_index=True, width="stretch", height=300)
    a, b, d = st.columns([3, 3, 1], vertical_alignment="bottom")
    parent = a.selectbox("Parent", ["(new top level)"] + cats.path.tolist())
    name = b.text_input("New category name")
    kind = None
    if parent == "(new top level)":
        kind = st.segmented_control("Kind", ["expense", "income", "transfer", "investing"], default="expense")
    if d.button("Add", disabled=not name.strip()):
        if parent == "(new top level)":
            path, pid = name.strip(), None
        else:
            row = cats[cats.path == parent].iloc[0]
            path, pid, kind = f"{parent}{taxonomy.SEP}{name.strip()}", int(row.id), row.kind
        c.execute("INSERT INTO categories (parent_id, name, path, kind) VALUES (?,?,?,?)", (pid, name.strip(), path, kind))
        c.commit()
        ui.invalidate()
        st.rerun()

    st.markdown("**Rename**")
    a, b, d = st.columns([3, 3, 1], vertical_alignment="bottom")
    old = a.selectbox("Category", cats.path.tolist(), key="ren_old")
    new = b.text_input("New name", key="ren_new")
    if d.button("Rename", disabled=not new.strip()):
        new_path = old.rsplit(taxonomy.SEP, 1)[0] + taxonomy.SEP + new.strip() if taxonomy.SEP in old else new.strip()
        c.execute("UPDATE categories SET name = ?, path = ? WHERE path = ?", (new.strip(), new_path, old))
        c.execute("UPDATE categories SET path = ? || substr(path, ?) WHERE path LIKE ?",
                  (new_path, len(old) + 1, old + taxonomy.SEP + "%"))
        c.commit()
        ui.invalidate()
        st.rerun()


def _prefs_section():
    c = ui.con()
    st.subheader("Alert thresholds")
    vals = prefs.all_prefs(c)
    cols = st.columns(3)
    new = {}
    for i, (k, v) in enumerate(vals.items()):
        new[k] = cols[i % 3].number_input(prefs.LABELS[k], value=float(v), step=1.0 if v >= 5 else 0.05, key=f"p_{k}")
    if st.button("Save thresholds"):
        for k, v in new.items():
            prefs.set_pref(c, k, v)
        from fin.insights import alerts

        st.toast(f"Saved · {alerts.generate(c)} new alert(s)")
        ui.invalidate()


def _quality_section():
    st.subheader("Data quality")
    gaps = balance_gaps(ui.con())
    if not gaps:
        st.success("Every balance snapshot reconciles with the transactions.", icon=":material/verified:")
    else:
        st.caption("Unexplained balance changes between two snapshots = missing (or duplicated) transactions in "
                   "that window — typically months without a statement.")
        st.dataframe(pd.DataFrame([{"account": g.account, "from": g.from_date, "to": g.to_date,
                                    "unexplained €": g.delta_cents / 100} for g in gaps]),
                     hide_index=True, width="stretch")


def render():
    st.title("Settings & data")
    t = st.tabs(["Refresh", "Alerts", "Banks", "Import", "Rules", "Categories", "Thresholds", "Data quality"])
    with t[0]:
        _refresh_section()
    with t[1]:
        st.subheader("New alerts")
        render_alerts(statuses=("new",))
        with st.expander("Seen / OK"):
            render_alerts(statuses=("seen", "ok"))
    with t[2]:
        _banks_section()
    with t[3]:
        _import_section()
    with t[4]:
        _rules_section()
    with t[5]:
        _categories_section()
    with t[6]:
        _prefs_section()
    with t[7]:
        _quality_section()
