"""User-tunable settings with defaults (stored in the `settings` table, edited in the app)."""

from __future__ import annotations

import sqlite3

from .db import get_setting, set_setting

DEFAULTS: dict[str, float | int] = {
    "large_txn_eur": 250.0,          # alert: single expense at or above this
    "first_merchant_eur": 50.0,      # alert: first-ever purchase at a merchant at or above this
    "merchant_outlier_z": 4.0,       # alert: robust z-score vs the merchant's history
    "drift_pct": 0.35,               # alert: category month vs trailing 6-month mean
    "drift_min_eur": 50.0,           # ...and at least this many euros above it
    "alert_lookback_days": 120,      # only raise transaction alerts this recent
    "consent_warn_days": 14,
}

LABELS = {
    "large_txn_eur": "Large transaction (€)",
    "first_merchant_eur": "First-time merchant above (€)",
    "merchant_outlier_z": "Unusual-for-merchant z-score",
    "drift_pct": "Category drift vs 6-month average (fraction)",
    "drift_min_eur": "Category drift minimum (€)",
    "alert_lookback_days": "Alert look-back (days)",
    "consent_warn_days": "Warn before consent expiry (days)",
}


def get(con: sqlite3.Connection, key: str):
    return get_setting(con, f"pref.{key}", DEFAULTS[key])


def all_prefs(con: sqlite3.Connection) -> dict:
    return {k: get(con, k) for k in DEFAULTS}


def set_pref(con: sqlite3.Connection, key: str, value) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    set_setting(con, f"pref.{key}", type(DEFAULTS[key])(value))
    con.commit()
