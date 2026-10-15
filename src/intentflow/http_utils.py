"""Bounded request framing shared by the local HTTP endpoints."""
from __future__ import annotations
from .i18n import tr


class RequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def read_body(handler, limit: int = 1024 * 1024) -> bytes:
    """Reject ambiguous framing before allocating/reading a client-controlled body."""
    if handler.headers.get("Transfer-Encoding") is not None:
        raise RequestError(400, tr("Transfer-Encoding not supported"))
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) > 1:
        raise RequestError(400, tr("duplicated Content-Length"))
    raw = lengths[0] if lengths else "0"
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise RequestError(400, tr("invalid Content-Length"))
    size = int(raw)
    if size > limit:
        raise RequestError(413, tr("request body exceeds the limit"))
    try:
        body = handler.rfile.read(size)
    except TimeoutError:
        raise RequestError(408, tr("timed out reading the request")) from None
    if len(body) != size:
        raise RequestError(400, tr("incomplete request body"))
    return body
