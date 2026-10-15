"""Encryption at rest of secrets kept in the store (TOTP secret, SSO id_token and refresh_token).

AES-128-CBC + HMAC-SHA256 (Fernet, `cryptography` library). Format: `enc:v1:<token>`; values without the prefix are read as they are
(old stores keep working; `intentflow secrets migrate` encrypts what already exists).

Key, in order: `INTENTFLOW_SECRET_KEY` (any long text; turned into a key by SHA-256) → file `<store>.key` (0600, created on the
first write; only for a SQLite file). Persisting new secrets is refused without encryption.
The `:memory:` store may keep plain text in memory; legacy values stay readable for migration.
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path
from .i18n import tr

PREFIX = "enc:v1:"


def _fernet(key_bytes: bytes):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest()))


class Box:
    def __init__(self, store_path: str | None, backend: str = "sqlite"):
        self._f = None
        self._persistent = backend != "sqlite" or bool(store_path and store_path != ":memory:")
        self.source = "none"
        env = os.environ.get("INTENTFLOW_SECRET_KEY")
        try:
            if env:
                self._f, self.source = _fernet(env.encode()), "env"
            elif backend == "sqlite" and store_path and store_path != ":memory:":
                kp = Path(str(store_path) + ".key")
                if not kp.exists():
                    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
                    with os.fdopen(fd, "wb") as f:
                        f.write(base64.urlsafe_b64encode(os.urandom(32)))
                self._f, self.source = _fernet(kp.read_bytes().strip()), "file"
        except ImportError:                       # legacy reads still possible; enc() refuses to persist new secrets
            self._f, self.source = None, "unavailable"

    @property
    def enabled(self) -> bool:
        return self._f is not None

    def enc(self, v: str | None) -> str | None:
        if v is None or v == "" or str(v).startswith(PREFIX):
            return v
        if self._f is None:
            if self._persistent:
                raise RuntimeError(tr("encryption unavailable: install intentflow[secrets] and set INTENTFLOW_SECRET_KEY for PostgreSQL"))
            return v
        return PREFIX + self._f.encrypt(str(v).encode()).decode()

    def dec(self, v: str | None) -> str | None:
        if v is None or not str(v).startswith(PREFIX):
            return v
        if self._f is None:
            raise RuntimeError(tr("encrypted value and no key available (set INTENTFLOW_SECRET_KEY)"))
        from cryptography.fernet import InvalidToken
        try:
            return self._f.decrypt(str(v)[len(PREFIX):].encode()).decode()
        except InvalidToken:
            raise RuntimeError(tr("could not decrypt: INTENTFLOW_SECRET_KEY differs from the one used to write")) from None

    def status(self) -> dict:
        return {"encrypted_at_rest": self.enabled, "key_source": self.source}
