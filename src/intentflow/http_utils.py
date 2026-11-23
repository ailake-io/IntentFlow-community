"""Bounded request framing shared by the local HTTP endpoints."""
from __future__ import annotations
from .i18n import tr


def tls_context(cert: str, key: str):
    """Server TLS (>= 1.2). Unreadable files end the process with an explanation instead of a traceback."""
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as e:      # e.g. key mode 600 owned by another uid inside a container
        raise SystemExit(tr("refused: cannot load the TLS certificate/key ({err}); the files must be readable by the user running the UI",
                            err=e)) from None
    return ctx


class RequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def read_body(handler, limit: int = 1024 * 1024) -> bytes:
    """Reject ambiguous framing before allocating/reading a client-controlled body.
    A handler that already read the request (`handler.prefetched_body`) is served from that."""
    if getattr(handler, "prefetched_body", None) is not None:
        return handler.prefetched_body
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
