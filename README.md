# fin: local personal finance dashboard

This dashboard runs on your own machine and only reads data, never writes to a bank. It is free to run.
Bank data comes from Enable Banking (PSD2, restricted production mode). Older history comes from
statement files. Investments come from DEGIRO exports plus yfinance prices. No transaction data
ever leaves the machine: the only outbound calls are to Enable Banking and the bank login pages,
Yahoo Finance (ticker symbols only) and ipify (your public IP, sent as a PSD2 header).

```powershell
uv sync                                        # Python 3.12 env from uv.lock
uv run streamlit run app/streamlit_app.py      # dashboard on http://localhost:8501
uv run pytest                                  # tests
```

## First-time setup

1. **Enable Banking.**
   1. In the Control Panel, choose *Add application* → **Production**, with redirect URL
      `https://localhost:8501/callback`.
   2. The browser downloads `<app-id>.pem`. Put it in `secrets/`.
   3. On the app page, click **Activate by linking accounts** and link every account.
2. **Environment.** `copy .env.example .env`, then fill in `EB_APP_ID` and `EB_KEY_PATH`.
3. **Connect each bank** (Settings → Banks, or the CLI below).
   1. Approve the consent at the bank. Use your phone for Millennium, because its desktop login stalls.
   2. The browser then lands on an address that doesn't load. Paste that address back into the app.
   3. Consent lasts 180 days. The app warns 14 days before it expires, and renewing keeps all history.
4. **Import history** in Settings → Import, or with `fin import <files>`. Supported formats:
   - Revolut account CSV (includes the Savings sub-account, which PSD2 does not expose)
   - Millennium *Extrato combinado* PDFs (each must reconcile to its closing balance or the import fails)
   - Santander meal card *descarga.xls*
   - DEGIRO `Transactions.csv` (lots) and `Account.csv` (cash, dividends, interest)

   Re-importing is safe: matches against existing data, by bank ID, content hash or a fuzzy
   cross-source match, are never inserted twice.

## Daily use

The **Refresh** button (Settings, or `fin refresh`) does the following. It never runs on its own.

1. Pulls balances and new booked transactions from each linked account (incremental, with a 7-day overlap).
2. Fetches prices.
3. Re-categorises everything.
4. Raises alerts.

## Pages

| Page | What it shows |
|---|---|
| Overview | net worth, balances, month so far, **spending pace** vs a typical month, **budgets**, alerts |
| Cash flow | income vs expenses, net savings, savings rate |
| Spending | category → sub-category → merchant → transactions drill-down, monthly heatmap |
| Recurring | subscriptions and fixed costs; **mark cancelled** (warns if they charge again) or *not recurring* |
| Investments | value vs contributions, positions, realised gains, dividends, lots |
| Projections & FIRE | **Monte Carlo** (5,000 paths): baseline vs *what if* (invest ±%, one-off buys, stop after N years), FIRE probability |
| Transactions | search, inline edit, **review queue** (ML suggestions), **by-merchant bulk rules** |
| Trips & tags | trip builder: in-range spending (recurring and home costs excluded) + flights/hotels booked before |
| Settings & data | refresh, banks, imports, rules, categories, budgets, thresholds, data quality, **HTML export** |

## Categorising (and why it sticks)

- **One transaction.** Transactions → change the *Category* cell → **Save**. It is stored in `tx_user`, which
  Refresh and reprocessing never touch. Tick *create a rule* to make future ones from that merchant follow.
- **A whole merchant.** Transactions → *By merchant* → set a category → **Save**. This creates a rule that applies
  to every past and future transaction from that merchant. A single transaction you set by hand still wins.
- **Pattern rules** (e.g. anything containing `PADEL`): Settings → Rules.

Priority: manual > internal transfer > rule > ML (≥ 70% confidence) > review queue.

## On your phone

**Recommended: Tailscale.** It's private, nothing leaves your devices, and you get the live app.

1. Start Tailscale on the PC and sign in. Install the Tailscale app on the phone and sign in with the same account.
2. On the PC, run `tailscale serve --bg 8501`. This serves the running app over HTTPS, inside your tailnet only.
3. On the phone, open `https://<pc-name>.<tailnet>.ts.net`. The command in step 2 prints this address.

**Offline snapshot.** Settings → Export (or `fin report`) writes one HTML file (~5 MB, works offline).
Emailing it puts your data on the mail provider's servers, so prefer USB, a local share, or Tailscale's
`tailscale file cp`.

## How data flows

```
bank API / files ──► raw_payloads (gzip, immutable) ──► transactions (deduped)
                                                          │
       rules ► transfers ► ML fallback ◄── tx_user (your edits; never overwritten)
                                                          ▼
                                     tx_enriched (rebuilt on every reprocess)
                                                          ▼
                     metrics (pure functions) ──► Streamlit views (app/views/*.py)
```

- **Categorisation.** Priority order: manual > internal transfer > rule > ML (applied only at ≥ 70%
  confidence, ~99% precise on held-out data) > review queue.
  - Recategorising in the app can create a merchant rule.
  - Rules and categories are editable in Settings.
- **Transfers.** Legs between your own accounts are paired and excluded from income and spending.
  Partner deposits into the joint account count as *partner contributions*. DEGIRO and Trading 212
  movements count as *investing*.
- **Joint account.** Shown at your share by default. Switch between my share, full and exclude in the sidebar.
- **One-offs.** Tag a transaction `one-off` and it still counts in net worth but is left out of
  income, expenses and the savings rate.
- **Data quality.** `fin check` (also on the *Data quality* tab) compares every balance snapshot
  with the transactions in between. Any unexplained difference means missing statements.

## CLI

| command | |
|---|---|
| `fin refresh [--force] [--no-banks]` | everything the Refresh button does |
| `fin eb status` / `banks` / `connect <bank> [--qr]` / `finish <url>` | bank connections |
| `fin import <files> [--dry-run]` | statement and broker files |
| `fin txns [--uncategorised] [--search x]`, `fin summary` | quick looks |
| `fin categorize <id> "<Category > Sub>" [--tag one-off] [--rule]` | manual decision |
| `fin invest [refresh\|lots\|add\|supersede]` | lots and prices |
| `fin reprocess [--reseed]`, `fin check` | rebuild derived data, reconcile |
| `fin report [--months 12] [--cdn] [--out f.html]` | static HTML snapshot |

## Hosting on the home server later

```bash
docker compose up -d --build     # binds 127.0.0.1:8501; data/ and secrets/ are mounted volumes
```

For phone access, put Tailscale in front rather than exposing the port. The app has no login of its own.

## Layout

```
src/fin/ingest/        Enable Banking client + consent + sync, file importers, dedupe
src/fin/normalize/     PT descriptor parser (COMPRA, MB WAY, TRF, DD, PAGSERV, fees), merchant normalisation
src/fin/categorize/    taxonomy.yaml, seed_rules.yaml, rules engine, ML fallback, manual edits
src/fin/transfers.py   internal transfer / partner / investing detection
src/fin/invest/        lots (FIFO), yfinance price cache, DEGIRO account statement
src/fin/insights/      recurring payments, alerts (large, new merchant, outliers, duplicates, fees, drift)
src/fin/metrics/       everything the dashboard shows, as DataFrames
app/                   Streamlit UI (one module per page; ui.py holds theme, colours and filters)
```

Secrets live in `.env` and `secrets/`, both gitignored and never logged. `data/` (the database,
imports and raw payloads) is gitignored too.
