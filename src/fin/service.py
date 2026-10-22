"""One entry point for the Refresh button (and `fin refresh`)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from .config import Settings
from .ingest.enablebanking.sync import AccountSyncResult, sync_all
from .insights import alerts
from .invest import prices
from .pipeline import Stats, reprocess

log = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    banks: list[AccountSyncResult] = field(default_factory=list)
    prices: dict[str, int] = field(default_factory=dict)
    stats: Stats | None = None
    new_alerts: int = 0
    errors: list[str] = field(default_factory=list)


def refresh(con: sqlite3.Connection, settings: Settings, force: bool = False, banks: bool = True) -> RefreshResult:
    res = RefreshResult()
    if banks:
        try:
            res.banks = sync_all(con, settings, force=force)
        except Exception as e:
            log.exception("bank sync failed")
            res.errors.append(f"bank sync: {e}")
    try:
        res.prices = prices.refresh(con)
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"prices: {e}")
    res.stats = reprocess(con)
    res.new_alerts = alerts.generate(con)
    return res
