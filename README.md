# fin — local personal finance dashboard

Local-only, free, read-only. Bank data via Enable Banking (PSD2, restricted production mode),
investments via a DEGIRO lot table + yfinance. Nothing about your transactions leaves the machine.

## Setup

```powershell
# once: install uv (https://docs.astral.sh/uv/)
uv sync                      # creates .venv from uv.lock (Python 3.12)
copy .env.example .env       # then fill in EB_APP_ID / EB_KEY_PATH
uv run pytest
```

### Enable Banking (one-off)

1. Control Panel → *Add application* → **Production**, redirect URL `https://localhost:8501/callback`.
   The browser downloads `<app-id>.pem` → move it to `secrets/`.
2. On the app page click **Activate by linking accounts** and link every account at each bank
   (Millennium BCP, then Revolut). This whitelists them; the app becomes *active (restricted)*.
3. `uv run fin eb status` should now say `active=True`.

### Connecting a bank (and renewing consent)

```powershell
uv run fin eb banks --search revolut          # exact bank name + max consent length
uv run fin eb connect "Millennium BCP"         # opens the bank's login
# after approving, the browser lands on https://localhost:8501/callback?code=...  — the page
# not loading is expected. Copy the full URL:
uv run fin eb finish "https://localhost:8501/callback?state=...&code=..."
uv run fin accounts                            # what the bank exposed
```

Renewal is the same two commands. Accounts are keyed by IBAN, so history carries over to the
new session. `fin eb status` shows days left per consent (warns from 14 days).

### Refresh

```powershell
uv run fin sync          # balances + booked transactions, incremental (7-day overlap)
```

Refresh is manual only. Accounts synced in the last 15 minutes are skipped unless `--force`.

## Layout

```
src/fin/ingest/enablebanking/   auth (RS256 JWT), client, consent, mapping, sync
src/fin/ingest/model.py         RawTxn, cents parsing, fingerprints
src/fin/ingest/dedupe.py        id / fingerprint / cross-source (CSV vs API) matching
src/fin/db/schema.sql           SQLite schema (raw is immutable; tx_enriched is rebuilt; tx_user is yours)
```

Secrets live in `.env` and `secrets/` (both gitignored) and are never logged.
