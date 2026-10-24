-- v1 schema. Raw data (raw_payloads, transactions core columns) is immutable;
-- tx_enriched is rebuilt by the pipeline; tx_user holds manual decisions and is never wiped.

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,      -- stable across sessions: iban:<IBAN> | eb:<identification_hash> | manual:<slug>
    name            TEXT NOT NULL,
    institution     TEXT NOT NULL,
    iban            TEXT,
    currency        TEXT NOT NULL DEFAULT 'EUR',
    kind            TEXT NOT NULL DEFAULT 'current',  -- current | savings | joint | meal | investment
    is_joint        INTEGER NOT NULL DEFAULT 0,
    my_share        REAL NOT NULL DEFAULT 1.0,       -- default share of joint spending
    source          TEXT NOT NULL DEFAULT 'eb',      -- eb | manual
    eb_uid          TEXT,                            -- uid in the *current* session (changes on re-consent)
    eb_session_id   TEXT,
    eb_raw          TEXT,                            -- AccountResource JSON from latest session
    color           TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    last_synced_at  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS eb_pending_auth (
    state           TEXT PRIMARY KEY,
    aspsp_name      TEXT NOT NULL,
    aspsp_country   TEXT NOT NULL,
    valid_until     TEXT NOT NULL,
    auth_url        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS eb_sessions (
    session_id      TEXT PRIMARY KEY,
    aspsp_name      TEXT NOT NULL,
    aspsp_country   TEXT NOT NULL,
    status          TEXT,
    valid_until     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at      TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,            -- eb | import:<format>
    account_id      INTEGER REFERENCES accounts(id),
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
    date_from       TEXT,
    n_fetched       INTEGER DEFAULT 0,
    n_new           INTEGER DEFAULT 0,
    n_duplicate     INTEGER DEFAULT 0,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id              INTEGER PRIMARY KEY,
    sync_run_id     INTEGER REFERENCES sync_runs(id),
    account_id      INTEGER REFERENCES accounts(id),
    kind            TEXT NOT NULL,            -- transactions | balances | details | session | file
    name            TEXT,                     -- e.g. original filename
    sha256          TEXT NOT NULL,
    body            BLOB NOT NULL,            -- gzip-compressed bytes
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_raw_sha ON raw_payloads(sha256);

CREATE TABLE IF NOT EXISTS balances (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    as_of           TEXT NOT NULL,            -- date
    balance_type    TEXT NOT NULL,            -- CLBD, ITAV, ... or MANUAL
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, as_of, balance_type)
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    source          TEXT NOT NULL,            -- eb | csv:millennium | csv:revolut | ...
    external_id     TEXT,                     -- bank transaction_id / entry_reference
    fingerprint     TEXT NOT NULL,            -- stable content hash (fallback identity)
    booking_date    TEXT NOT NULL,
    value_date      TEXT,
    amount_cents    INTEGER NOT NULL,         -- signed, account currency (negative = outflow)
    currency        TEXT NOT NULL,
    orig_amount_cents INTEGER,                -- instructed amount for FX transactions
    orig_currency   TEXT,
    description     TEXT NOT NULL,
    counterparty_name TEXT,
    counterparty_iban TEXT,
    bank_tx_code    TEXT,
    mcc             TEXT,
    balance_after_cents INTEGER,
    raw_json        TEXT,                     -- this record as received
    raw_payload_id  INTEGER REFERENCES raw_payloads(id),
    duplicate_of    INTEGER REFERENCES transactions(id),  -- set when matched to a row from another source
    imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, fingerprint)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tx_external ON transactions(account_id, source, external_id)
    WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tx_date ON transactions(account_id, booking_date);

CREATE TABLE IF NOT EXISTS categories (
    id              INTEGER PRIMARY KEY,
    parent_id       INTEGER REFERENCES categories(id),
    name            TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,     -- "Housing > Utilities > Electricity"
    kind            TEXT NOT NULL,            -- expense | income | transfer | investing
    color           TEXT
);

CREATE TABLE IF NOT EXISTS merchants (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    default_category_id INTEGER REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS merchant_aliases (
    alias           TEXT PRIMARY KEY,         -- normalised descriptor key
    merchant_id     INTEGER NOT NULL REFERENCES merchants(id)
);

CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY,
    priority        INTEGER NOT NULL DEFAULT 100,   -- lower runs first
    name            TEXT,
    field           TEXT NOT NULL DEFAULT 'description',  -- description | merchant | counterparty_name | counterparty_iban | descriptor_type
    pattern         TEXT NOT NULL,            -- case-insensitive regex
    account_id      INTEGER REFERENCES accounts(id),
    account_kind    TEXT,                     -- restrict to accounts of this kind (e.g. meal)
    sign            INTEGER,                  -- -1 outflow, 1 inflow, NULL any
    min_cents       INTEGER,
    max_cents       INTEGER,
    category_id     INTEGER REFERENCES categories(id),
    merchant_name   TEXT,
    tags            TEXT,                     -- comma separated
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tx_enriched (
    tx_id           INTEGER PRIMARY KEY REFERENCES transactions(id),
    descriptor_type TEXT,                     -- card | mbway | transfer | direct_debit | atm | fee | salary | other
    merchant_key    TEXT,
    merchant_id     INTEGER REFERENCES merchants(id),
    category_id     INTEGER REFERENCES categories(id),
    category_source TEXT,                     -- manual | rule | merchant | ml | transfer | none
    confidence      REAL,
    rule_id         INTEGER REFERENCES rules(id),
    transfer_group  TEXT,                     -- shared id for the two legs of an internal transfer
    flow            TEXT,                     -- expense | income | transfer | investing
    amount_eur_cents INTEGER,
    my_amount_eur_cents INTEGER,              -- after joint share
    ml_category_id  INTEGER REFERENCES categories(id),  -- low-confidence suggestion for the review queue
    ml_confidence   REAL
);

CREATE TABLE IF NOT EXISTS tx_user (
    tx_id           INTEGER PRIMARY KEY REFERENCES transactions(id),
    category_id     INTEGER REFERENCES categories(id),
    merchant_name   TEXT,
    my_share        REAL,
    notes           TEXT,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'tag'   -- tag | trip
);

CREATE TABLE IF NOT EXISTS tx_tags (
    tx_id           INTEGER NOT NULL REFERENCES transactions(id),
    tag_id          INTEGER NOT NULL REFERENCES tags(id),
    source          TEXT NOT NULL DEFAULT 'manual',   -- manual | rule
    PRIMARY KEY (tx_id, tag_id)
);

CREATE TABLE IF NOT EXISTS own_ibans (
    iban            TEXT PRIMARY KEY,
    label           TEXT,
    kind            TEXT NOT NULL DEFAULT 'own'   -- own | investing (e.g. DEGIRO/flatex deposit IBAN)
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT NOT NULL UNIQUE,     -- stable so dismissed alerts stay dismissed
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'info',
    tx_id           INTEGER REFERENCES transactions(id),
    title           TEXT NOT NULL,
    detail          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'new'   -- new | seen | ok
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    isin            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    ticker          TEXT,                     -- yfinance symbol (EUR listing preferred, e.g. VWCE.DE)
    currency        TEXT NOT NULL DEFAULT 'EUR',
    asset_class     TEXT NOT NULL DEFAULT 'equity'
);

CREATE TABLE IF NOT EXISTS lots (
    id              INTEGER PRIMARY KEY,
    trade_date      TEXT NOT NULL,            -- original acquisition date (kept for in-specie transfers)
    trade_time      TEXT,
    isin            TEXT NOT NULL,
    ticker          TEXT NOT NULL,            -- yfinance symbol, e.g. VWCE.DE
    quantity        REAL NOT NULL,            -- negative for sells
    price           REAL NOT NULL,            -- per unit, in currency
    fees            REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'EUR',
    broker          TEXT NOT NULL DEFAULT 'DEGIRO',
    source          TEXT NOT NULL DEFAULT 'manual',   -- manual | degiro_csv
    external_id     TEXT UNIQUE,              -- DEGIRO order id for idempotent imports
    locked          INTEGER NOT NULL DEFAULT 0,       -- manual edits win over CSV re-imports
    total_eur       REAL,                     -- signed EUR cash flow incl. fees (negative = buy)
    kind            TEXT NOT NULL DEFAULT 'trade',    -- trade | transfer_in
    superseded      INTEGER NOT NULL DEFAULT 0,       -- 1 = replaced by original-cost lots
    note            TEXT
);

CREATE TABLE IF NOT EXISTS invest_income (
    id              INTEGER PRIMARY KEY,
    date            TEXT NOT NULL,
    isin            TEXT,
    kind            TEXT NOT NULL,            -- dividend | dividend_tax | interest
    amount          REAL NOT NULL,            -- signed, in `currency`
    currency        TEXT NOT NULL,
    source          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
    ticker          TEXT NOT NULL,
    date            TEXT NOT NULL,
    close           REAL NOT NULL,
    currency        TEXT,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date            TEXT NOT NULL,
    currency        TEXT NOT NULL,            -- units of currency per 1 EUR (ECB convention)
    rate            REAL NOT NULL,
    PRIMARY KEY (date, currency)
);

-- user decisions about detected recurring series (matched by merchant + account + amount within 25%)
CREATE TABLE IF NOT EXISTS recurring_overrides (
    id              INTEGER PRIMARY KEY,
    merchant        TEXT NOT NULL,
    account         TEXT NOT NULL,
    typical         REAL NOT NULL,
    status          TEXT NOT NULL,            -- cancelled | ignored (not actually recurring)
    since           TEXT,                     -- cancellation date
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- monthly budget per top-level expense category
CREATE TABLE IF NOT EXISTS budgets (
    category        TEXT PRIMARY KEY,
    monthly_eur     REAL NOT NULL
);
