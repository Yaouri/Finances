"""RS256 application JWTs for the Enable Banking API."""

from __future__ import annotations

import time
from pathlib import Path

import jwt

MAX_TTL = 86_400  # API rejects tokens living longer than 24h


def make_jwt(app_id: str, private_key_pem: bytes, ttl: int = 3600, now: int | None = None) -> str:
    if not 0 < ttl <= MAX_TTL:
        raise ValueError(f"ttl must be in (0, {MAX_TTL}]")
    iat = int(now if now is not None else time.time())
    return jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": iat, "exp": iat + ttl},
        private_key_pem,
        algorithm="RS256",
        headers={"kid": app_id},
    )


def load_key(path: Path) -> bytes:
    return path.read_bytes()
