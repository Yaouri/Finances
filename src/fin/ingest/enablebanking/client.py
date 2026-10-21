"""Thin, read-only Enable Banking API client (AIS only — no payment endpoints exist here)."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx

from .auth import load_key, make_jwt

log = logging.getLogger(__name__)

USER_AGENT = "fin-local/0.1 (personal, read-only)"


class EnableBankingError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"Enable Banking {status} on {path}: {body[:500]}")
        self.status = status
        self.body = body


class EnableBankingClient:
    def __init__(
        self,
        app_id: str,
        key_path,
        base_url: str = "https://api.enablebanking.com",
        psu_headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._app_id = app_id
        self._key = load_key(key_path)
        self._token: str | None = None
        self._token_exp = 0.0
        # PSU headers mark a request as customer-present, which avoids the PSD2 cap on
        # unattended (background) access. Only send them for user-triggered refreshes.
        self._psu_headers = psu_headers or {}
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    # -- plumbing ---------------------------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        now = time.time()
        if self._token is None or now > self._token_exp - 60:
            self._token = make_jwt(self._app_id, self._key, ttl=3600)
            self._token_exp = now + 3600
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, *, psu: bool = False, **kw) -> Any:
        headers = self._auth_header() | (self._psu_headers if psu else {})
        for attempt in range(4):
            r = self._http.request(method, path, headers=headers, **kw)
            if r.status_code == 429 and attempt < 3:
                wait = float(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                log.warning("429 on %s, retrying in %.0fs", path, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise EnableBankingError(r.status_code, r.text, path)
            return r.json() if r.content else None
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- application / directory ----------------------------------------------------------
    def application(self) -> dict:
        return self._request("GET", "/application")

    def aspsps(self, country: str | None = None) -> list[dict]:
        params = {"country": country} if country else {}
        return self._request("GET", "/aspsps", params=params)["aspsps"]

    # -- consent ----------------------------------------------------------------------------
    def start_auth(
        self,
        aspsp_name: str,
        country: str,
        valid_until: str,
        redirect_url: str,
        state: str,
        psu_type: str = "personal",
    ) -> dict:
        body = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": psu_type,
        }
        return self._request("POST", "/auth", json=body)

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")

    # -- data -------------------------------------------------------------------------------
    def account_details(self, uid: str) -> dict:
        return self._request("GET", f"/accounts/{uid}/details", psu=True)

    def balances(self, uid: str) -> dict:
        return self._request("GET", f"/accounts/{uid}/balances", psu=True)

    def transaction_pages(
        self,
        uid: str,
        date_from: date | None = None,
        date_to: date | None = None,
        strategy: str | None = None,
    ) -> Iterator[dict]:
        """Yield raw response pages, following continuation_key until exhausted."""
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()
        if strategy:
            params["strategy"] = strategy
        while True:
            page = self._request("GET", f"/accounts/{uid}/transactions", psu=True, params=params)
            yield page
            key = page.get("continuation_key")
            if not key:
                return
            params["continuation_key"] = key
