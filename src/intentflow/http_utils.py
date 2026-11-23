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


def urlopen(req, timeout: float):
    """urllib urlopen with retries on transient failures (connection errors, 429, 5xx); read-only calls only.
    INTENTFLOW_HTTP_RETRIES (default 2) extra attempts, exponential backoff from 0.5s."""
    import os
    import time
    import urllib.error
    import urllib.request
    try:
        retries = max(0, int(os.environ.get("INTENTFLOW_HTTP_RETRIES", "2")))
    except ValueError:
        retries = 2
    for n in range(retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if n == retries or not (e.code == 429 or e.code >= 500):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if n == retries:
                raise
        time.sleep(0.5 * 2 ** n)


def access_log(handler, code) -> None:
    """One line per request on stderr when INTENTFLOW_ACCESS_LOG is `json` or `text` (default: off).
    The query string is dropped: it may carry tokens."""
    import os
    import sys
    import time
    mode = os.environ.get("INTENTFLOW_ACCESS_LOG", "").lower()
    if mode not in ("json", "text"):
        return
    rec = {"ts": round(time.time(), 3), "client": handler.client_address[0], "method": handler.command,
           "path": (handler.path or "").split("?")[0], "status": int(code) if isinstance(code, int) else code}
    if mode == "json":
        import json
        line = json.dumps(rec)
    else:
        line = f'{rec["client"]} {rec["method"]} {rec["path"]} {rec["status"]}'
    print(line, file=sys.stderr, flush=True)


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
