"""TOTP (RFC 6238) with the standard library: SHA-1, 6 digits, 30 s step."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, step: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(n).zfill(digits)


def verify(secret: str, code: str, now: float | None = None, window: int = 1, last_step: int = 0) -> int | None:
    """Return the accepted step (to prevent reuse) or None. Accepts ±`window` steps of clock tolerance."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    cur = int((now if now is not None else time.time()) // 30)
    for s in range(cur - window, cur + window + 1):
        if s > last_step and hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def uri(secret: str, account: str, issuer: str = "IntentFlow") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
