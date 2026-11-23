"""Local or team UI/API. Security: allowed Host, anti-CSRF header, HttpOnly/SameSite cookies, roles, optional MFA (TOTP),
persistent login rate limit, TLS. Extensions (see `extensions`) add SSO, per-project roles, governed fixes and AI."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from . import auth, extensions, i18n, lineage_api, overview, runner, totp
from .i18n import tr
from .store import Store
from .http_utils import RequestError, read_body

PAGE_FILE = Path(__file__).parent / "web.html"
DEFAULT_PROJECT = "main"
ThreadingHTTPServer.request_queue_size = 256      # preserve the larger accept backlog for embedded/test servers too


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Thread-per-request HTTP server with a hard cap on concurrent handlers."""

    daemon_threads = True
    request_queue_size = 256

    def __init__(self, server_address, RequestHandlerClass, max_workers: int = 128):
        if not 1 <= max_workers <= 1024:
            raise ValueError("max_workers must be between 1 and 1024")
        self._request_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address):
        # Close excess connections instead of spawning unbounded threads or blocking the accept loop.
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        worker = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
        try:
            worker.start()
        except Exception:
            self._request_slots.release()
            self.handle_error(request, client_address)
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class _Handled(Exception):
    """The response (401/403/...) was already sent."""


SAFE_SETTING_PREFIXES = ("policy.", "scan.", "watch.", "lineage.", "dbt.")      # tuning a project admin may change...
# ...except paths the server will read from (an admin of one project could point them at another project's files) and
# security policy that applies to everyone.
GLOBAL_ONLY_SETTINGS = {"dbt.root", "dbt.target", "dbt.base_target", "dbt.base_root", "dbt.base_revision", "lineage.declared",
                        "policy.separate_approver", "policy.token_max_age_days"}


def _redact_dsn(spec) -> str:
    """Registry values may be PostgreSQL DSNs with a password: in the authority part and/or in query parameters
    (`?password=`, `sslpassword=`), or libpq key=value form."""
    import re
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    text = str(spec)
    # password up to the LAST "@" (an unescaped "/" or "@" in it must not end the redaction early)
    text = re.sub(r"(://[^:/@\s]+):\S*@(?=[^@\s]*$)", r"\1:***@", text)
    if "://" in text:
        try:
            u = urlsplit(text)
            netloc = u.netloc
            if "@" in netloc:
                cred, host = netloc.rsplit("@", 1)
                netloc = (cred.split(":", 1)[0] + ":***@" if ":" in cred else cred + "@") + host
            qs = [(k, "***" if "pass" in k.lower() or "secret" in k.lower() else v) for k, v in parse_qsl(u.query, keep_blank_values=True)]
            return urlunsplit((u.scheme, netloc, u.path, urlencode(qs, safe="*"), u.fragment))
        except ValueError:
            pass
    return re.sub(r"((?:pass\w*|secret\w*)\s*=\s*)\S+", r"\1***", text, flags=re.I)


def scope_ok(who: dict, project: str, finding: dict | None, role: str | None) -> bool:
    """Write scope: if the person has patterns for the project, they only act on findings whose file/nodes match one."""
    if role == "admin":
        return True
    pats = (who.get("scopes") or {}).get(project)
    if not pats:
        return True
    if finding is None:
        return False
    import fnmatch
    cands = [finding.get("evidence", {}).get("file")] if finding.get("evidence", {}).get("file") else []
    cands += [n.split(".")[-1] for n in finding.get("nodes", [])] + list(finding.get("nodes", []))
    return any(fnmatch.fnmatch(c, p) for c in cands if c for p in pats)


def cookie(header: str, name: str) -> str | None:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def make_handler(store_path: str, user: str = "local", token: str | None = None, **ext_opts):
    """`token`/INTENTFLOW_UI_TOKEN (legacy): a single admin token, only without users. `ext_opts`: passed to extensions (tests)."""
    legacy = token if token is not None else os.environ.get("INTENTFLOW_UI_TOKEN")
    try:
        api_rate = int(os.environ.get("INTENTFLOW_API_RATE_PER_MIN", "600"))   # per client IP; 0 disables
    except ValueError:
        api_rate = 600
    hits: dict[str, list] = {}
    hits_lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        options = ext_opts
        primary_store = store_path

        def setup(self):
            self.request.settimeout(15)
            if hasattr(self.request, "do_handshake"):      # TLS handshake in the worker thread, bounded by the timeout: never in accept()
                self.request.do_handshake()
            self._request_stores = {}
            super().setup()

        def _store(self, path):
            if path not in self._request_stores:
                self._request_stores[path] = Store(path)
            return self._request_stores[path]

        def finish(self):
            try:
                super().finish()
            finally:
                for store in self._request_stores.values():
                    store.close()

        def _rate_ok(self) -> bool:
            """Sliding one-minute window per client IP over /api/*. In memory, per process. Loopback is exempt (local mode,
            or a same-host proxy without INTENTFLOW_TRUSTED_PROXIES where every client would share one IP)."""
            if api_rate <= 0 or not self.path.startswith("/api/"):
                return True
            now, ip = time.time(), self._client_ip()
            if ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return True
            with hits_lock:
                if len(hits) > 10000:          # bound memory: drop idle clients
                    for k in [k for k, v in hits.items() if not v or v[-1] < now - 60]:
                        del hits[k]
                q = [t for t in hits.get(ip, ()) if t > now - 60]
                ok = len(q) < api_rate
                if ok:
                    q.append(now)
                hits[ip] = q
            if not ok:
                self.close_connection = True       # a POST body may be unread
                self._json(429, {"error": tr("too many requests; slow down")})
            return ok

        def log_message(self, *a):
            pass

        def log_request(self, code="-", size="-"):
            from .http_utils import access_log
            access_log(self, code)

        def cookie_key(self) -> bytes:
            st = self._store(store_path)
            k = st.get_setting("state.cookie_key")
            if not k:
                k = secrets.token_hex(32)
                st.set_setting("state.cookie_key", k)
            return bytes.fromhex(k)

        # ---- responses ----
        def _send(self, code, body, ctype="application/json", cookies=(), location=None, nonce=None):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            if nonce:
                self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
                                 "img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
            if self._secure_cookie():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            if location:
                self.send_header("Location", location)
            for c in cookies:
                self.send_header("Set-Cookie", c)
            self.end_headers()
            self.wfile.write(b)

        def _json(self, code, obj, cookies=()):
            self._send(code, json.dumps(obj, default=str), cookies=cookies)

        def _host_ok(self):
            h = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            allow = os.environ.get("INTENTFLOW_ALLOWED_HOSTS", "")
            return h in ("127.0.0.1", "localhost") or h in [x.strip() for x in allow.split(",") if x.strip()]

        def _secure_cookie(self):
            return "; Secure" if getattr(self.server, "scheme", "http") == "https" or self.headers.get("X-Forwarded-Proto") == "https" else ""

        def _legacy_sid(self) -> str:
            """The cookie never carries the legacy token itself: an HMAC of it with the server's cookie key."""
            import hmac, hashlib
            return "ifl_" + hmac.new(self.cookie_key(), legacy.encode(), hashlib.sha256).hexdigest()

        def session_cookie(self, sid: str) -> str:
            return f"if_session={sid}; HttpOnly; SameSite=Strict; Path=/{self._secure_cookie()}"

        # ---- identity ----
        def _identity(self, prim: Store):
            sid = cookie(self.headers.get("Cookie", ""), "if_session")
            bearer = (self.headers.get("Authorization") or "")
            bearer = bearer[7:] if bearer.startswith("Bearer ") else None
            if prim.has_users():
                if sid and sid.startswith("ifs_"):
                    u = prim.user_by_session(sid)
                    if u and u.get("via") == "sso" and not extensions.first("refresh_session", self, prim, sid, u["name"]):
                        return None             # SSO session without the extension that issued it, or refused by the provider
                    return prim.user_by_session(sid) if u else None
                if bearer:
                    u = prim.user_by_token(bearer)
                    return None if (u and u.get("mfa")) else u      # a bare token is not enough once MFA is on
                return None
            if legacy:
                import hmac
                ok = (sid is not None and hmac.compare_digest(sid.encode(), self._legacy_sid().encode())) or \
                     (bearer is not None and hmac.compare_digest(bearer.encode(), legacy.encode()))
                return {"name": "token-admin", "role": "admin", "project_roles": {}} if ok else None
            return {"name": user, "role": "admin", "project_roles": {}}

        def _project(self) -> str:
            q = dict(parse_qsl(urlparse(self.path).query))
            return self.headers.get("X-IntentFlow-Project") or q.get("project") or DEFAULT_PROJECT

        def _stores(self, prim: Store):
            reg = (prim.get_setting("projects", {}) or {}) if "projects" in extensions.features() else {}
            return {DEFAULT_PROJECT: store_path, **reg}

        def _data(self, prim: Store, who) -> Store:
            proj = self._project()
            reg = self._stores(prim)
            if proj not in reg:
                self._json(404, {"error": tr("project '{name}' does not exist", name=proj)})
                raise _Handled()
            if auth.role_in(who, proj) in (None, "none"):
                self._json(403, {"error": tr("no access to this project")})
                raise _Handled()
            return prim if reg[proj] == store_path else self._store(reg[proj])

        def _need(self, prim: Store, action: str):
            who = self._identity(prim)
            if who is None:
                self._json(401, {"error": tr("authentication required"), "login": True})
                raise _Handled()
            role = auth.role_in(who, self._project())
            if not auth.allowed(role, action):
                self._json(403, {"error": tr("your role ({role}) does not allow this action", role=role)})
                raise _Handled()
            return who

        def _global_admin(self, prim: Store):
            who = self._identity(prim)
            if who is None:
                self._json(401, {"error": tr("authentication required")})
                raise _Handled()
            if auth.role_in({**who, "project_roles": {}}, None) != "admin":
                self._json(403, {"error": tr("global admin only")})
                raise _Handled()
            return who

        def _lang(self):
            i18n.set_request_lang(self.headers.get("X-IntentFlow-Lang"))

        def _page(self, q: dict) -> str:
            """The UI with its message catalog inlined: `?lang=`, then the `if_lang` cookie, then Accept-Language."""
            pick = q.get("lang") or cookie(self.headers.get("Cookie", ""), "if_lang") or (self.headers.get("Accept-Language") or "")[:2]
            code = i18n.normalize(pick)
            data = json.dumps({"lang": code, "messages": i18n.catalog(code) if code != "en" else {}}, ensure_ascii=False)
            page = PAGE_FILE.read_text().replace('/*I18N*/{"lang":"en","messages":{}}', data.replace("</", "<\\/"), 1)
            return page.replace("<script>", f'<script nonce="{self._nonce}">')

        # ---- GET ----
        def do_GET(self):
            self._lang()
            if not self._host_ok():
                return self._send(403, "forbidden host", "text/plain")
            if not self._rate_ok():
                return
            u = urlparse(self.path)
            path, q = u.path, dict(parse_qsl(u.query))
            prim = self._store(store_path)
            try:
                if extensions.first("web_get", self, prim, path, q):
                    return
                if path == "/" and "token" in q:
                    return self._send(401, tr("URL token login is disabled; use the login form"), "text/plain")
                if path == "/":
                    self._nonce = secrets.token_urlsafe(16)
                    return self._send(200, self._page(q), "text/html", nonce=self._nonce)
                if path == "/api/i18n":
                    code = i18n.normalize(q.get("lang"))
                    return self._json(200, {"lang": code, "messages": i18n.catalog(code) if code != "en" else {}})
                if path == "/api/me":
                    who = self._identity(prim)
                    reg = self._stores(prim)
                    projs = [{"name": n, "role": auth.role_in(who, n)} for n in reg if who and auth.role_in(who, n) not in (None, "none")]
                    out = {"user": ({k: who[k] for k in ("name", "role", "mfa", "via") if k in who} if who else None),
                           "auth_required": prim.has_users() or legacy is not None, "team": prim.has_users(), "sso": False,
                           "projects": projs, "default_project": DEFAULT_PROJECT, "features": extensions.features()}
                    out.update(extensions.first("me", self, prim, who) or {})
                    return self._json(200, out)
                mtok = os.environ.get("INTENTFLOW_METRICS_TOKEN")
                bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if path == "/metrics" and mtok and secrets.compare_digest(bearer.encode(), mtok.encode()):
                    from . import promexport
                    return self._send(200, promexport.render(prim), "text/plain; version=0.0.4")      # main project only
                self._need(prim, "read")
                who = self._identity(prim)
                st = self._data(prim, who)
                if path == "/metrics":
                    from . import promexport
                    return self._send(200, promexport.render(st), "text/plain; version=0.0.4")
                if path == "/api/team":
                    return self._json(200, {"team": [x["name"] for x in prim.users() if not x["disabled"]]})
                if path == "/api/users":
                    if auth.role_in(who, None) != "admin":
                        return self._json(403, {"error": tr("global admin only")})
                    return self._json(200, {"users": prim.users_admin(), "projects": list(self._stores(prim))})
                if path == "/api/projects":
                    return self._json(200, {"projects": {n: _redact_dsn(p) for n, p in self._stores(prim).items()}} if auth.allowed(auth.role_in(who, None), "users") else
                                      {"projects": {n: "" for n in self._stores(prim) if auth.role_in(who, n) not in (None, "none")}})
                if path.startswith("/api/notes/"):
                    return self._json(200, {"notes": st.notes(path.rsplit("/", 1)[1])})
                if path.startswith("/api/lineage/"):
                    return self._lineage(st, path, q)
                if path == "/api/changes":
                    from . import change_review
                    target, base = st.get_setting("dbt.target"), st.get_setting("dbt.base_target")
                    if not target or not base:
                        return self._json(400, {"error": tr("change comparison requires candidate and baseline targets")})
                    try:
                        return self._json(200, change_review.acknowledge(change_review.load(target, base, baseline_revision=st.get_setting("dbt.base_revision"))))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                if path == "/api/rules":
                    from . import business_rules, dbt_loader, dbt_results
                    target = st.get_setting("dbt.target")
                    if not target:
                        return self._json(400, {"error": tr("configure dbt.target")})
                    try:
                        project = dbt_loader.load(target, st.get_setting("dbt.root") or Path(target).parent)
                        return self._json(200, business_rules.build(project, dbt_results.load(target), st))
                    except (ValueError, OSError, KeyError) as e:
                        return self._json(400, {"error": str(e)})
                if path == "/api/settings":
                    vis = {k: v for k, v in st.settings().items()
                           if k != "projects" and not k.startswith(("state.", "secret.", "sso."))}
                    return self._json(200, {"settings": vis, "last_check": st.get_setting("state.last_check"), "running": st.get_setting("state.running"),
                                            "store_backend": st.backend})
                if path == "/api/integrations":
                    from . import integrations
                    return self._json(200, integrations.build(st))
                if path == "/api/overview":
                    return self._json(200, overview.build_cached(st) if st.path != ":memory:" else overview.build(st))
                self._json(404, {"error": "not found"})
            except _Handled:
                return

        def _lineage(self, st: Store, path: str, q: dict):
            target = st.get_setting("dbt.target")
            if not target:
                return self._json(400, {"error": tr("configure dbt.target")})
            try:
                proj, res = lineage_api.load(target, st.get_setting("dbt.root"), cache=st)
                what = path.rsplit("/", 1)[1]
                depth = max(1, min(6, int(q.get("depth", 3))))
                if what == "models":
                    out = lineage_api.models(proj, st.findings("open"), bool(st.get_setting("scan.include_packages")))
                elif what == "model":
                    d = lineage_api.model_detail(proj, q.get("model", ""))
                    if not d:
                        raise ValueError(tr("model not found"))
                    out = {**d, "graph": lineage_api.model_graph(proj, q["model"], min(depth, 3))}
                elif what == "column":
                    from . import external
                    declared = st.get_setting("lineage.declared") or str(Path(st.get_setting("dbt.root") or Path(target).parent) / external.DECLARED_FILE)
                    out = {"graph": lineage_api.column_graph(proj, res, q.get("model", ""), q.get("column", ""), depth),
                           "impact": lineage_api.impact(proj, res, q.get("model", ""), q.get("column", ""),
                                                        external=external.load(st, declared, proj))}
                else:
                    raise ValueError(tr("unknown route"))
                return self._json(200, out)
            except (ValueError, KeyError, FileNotFoundError) as e:
                return self._json(400, {"error": str(e)})

        def _drain(self) -> None:
            """After refusing a request without reading its body: closing with unread bytes makes the kernel reset the connection and
            the client may lose the response. Read what is already queued, bounded in bytes and total time (a client that keeps
            sending must not keep this thread)."""
            deadline, left = time.monotonic() + 0.5, 256 * 1024
            try:
                while left > 0 and time.monotonic() < deadline:
                    self.connection.settimeout(max(0.01, deadline - time.monotonic()))
                    chunk = self.connection.recv(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
            except OSError:
                pass

        def _client_ip(self) -> str:
            peer = self.client_address[0]
            trusted = [x.strip() for x in os.environ.get("INTENTFLOW_TRUSTED_PROXIES", "").split(",") if x.strip()]
            if peer in trusted:
                xff = self.headers.get("X-Forwarded-For", "")
                # proxies append to the header, so the first hop is client-controlled: take the right-most hop that is not a trusted proxy
                hops = [h.strip() for h in xff.split(",") if h.strip()]
                for hop in reversed(hops):
                    if hop not in trusted:
                        return hop
            return peer

        # ---- POST ----
        def do_POST(self):
            self.prefetched_body = None
            self._lang()
            if not self._host_ok():
                return self._send(403, "forbidden host", "text/plain")
            if not self._rate_ok():
                return
            u = urlparse(self.path)
            query = dict(parse_qsl(u.query))
            parts = u.path.strip("/").split("/")
            try:
                self.prefetched_body = read_body(self)    # before taking a database connection: a slow client must not hold a pool slot
            except RequestError as e:
                self.close_connection = True
                self._json(e.status, {"error": str(e)})
                return self._drain()
            prim = self._store(store_path)
            try:
                if extensions.first("web_post_raw", self, prim, parts):      # provider callbacks without the anti-CSRF header
                    return
            except _Handled:
                return
            if self.headers.get("X-IntentFlow") != "1":
                return self._json(400, {"error": "missing X-IntentFlow header"})
            try:
                body = json.loads(read_body(self) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError(tr("the JSON body must be an object"))
            except (ValueError, UnicodeDecodeError) as e:
                self.close_connection = True
                return self._json(e.status if isinstance(e, RequestError) else 400, {"error": tr("invalid request") if not isinstance(e, RequestError) else str(e)})
            try:
                if extensions.first("web_post", self, prim, parts, body, query):
                    return
                if parts == ["api", "login"]:
                    return self._login(prim, body)
                if parts == ["api", "logout"]:
                    sid = cookie(self.headers.get("Cookie", ""), "if_session")
                    out = {"ok": True}
                    if sid:
                        info = prim.session_info(sid)
                        if info and info["via"] == "sso":
                            out["logout_url"] = extensions.first("logout_url", self, prim, info)
                        prim.end_session(sid)
                    return self._json(200, out, cookies=["if_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"])
                if parts == ["api", "mfa"]:
                    return self._mfa(prim, body)
                if parts == ["api", "users"]:
                    return self._users(prim, body)
                if parts == ["api", "settings"]:
                    who = self._need(prim, "settings")
                    st = self._data(prim, who)
                    key = str(body.get("key", "")).strip()
                    # llm.*/jev.* go through their own endpoint, which checks who may point the server at a custom AI endpoint
                    if not key or key == "projects" or key.startswith(("state.", "secret.", "sso.", "llm.", "jev.")):
                        raise ValueError(tr("invalid key"))
                    # Connection/path keys (notify.sinks, dbt.root, lineage.declared, *.url, *_env ...) make the server reach hosts, read paths or send
                    # environment secrets: only a global admin, never someone who is admin of just one project.
                    if (not key.startswith(SAFE_SETTING_PREFIXES) or key in GLOBAL_ONLY_SETTINGS) and auth.role_in({**who, "project_roles": {}}, None) != "admin":
                        return self._json(403, {"error": tr("global admin only")})
                    if body.get("value") in (None, ""):
                        st.unset_setting(key)
                    else:
                        v = body["value"]
                        if key == "policy.pii_allowed_models" and isinstance(v, str):
                            v = [x.strip() for x in v.split(",") if x.strip()]
                        st.set_setting(key, v)
                    st.audit(who["name"], "config_set", {"key": key})
                    return self._json(200, {"ok": True})
                if parts == ["api", "diff"]:
                    return self._diff(prim, body)
                if parts == ["api", "run", "check"] or parts == ["api", "run", "runs"]:
                    return self._run(prim, parts[2])
                if parts[:2] == ["api", "findings"] and len(parts) == 4:
                    return self._finding_action(prim, parts[2], parts[3], body)
                self._json(404, {"error": "not found"})
            except _Handled:
                return
            except ValueError as e:
                self._json(400, {"error": str(e)})

        def _login(self, prim: Store, body: dict):
            ip = self._client_ip()
            if prim.attempts_recent(ip) >= 10:
                return self._json(429, {"error": tr("too many attempts; wait 1 minute")})
            tok = str(body.get("token", ""))
            if prim.has_users():
                who = prim.user_by_token(tok)
                if not who:
                    prim.attempt_failed(ip)
                    return self._json(401, {"error": tr("invalid token")})
                if who.get("mfa"):
                    code = str(body.get("code", ""))
                    if not code:
                        return self._json(401, {"error": tr("enter the authenticator code"), "mfa_required": True})
                    if not (prim.mfa_check(who["name"], code) or prim.mfa_use_recovery(who["name"], code)):
                        prim.attempt_failed(ip)
                        return self._json(401, {"error": tr("invalid code"), "mfa_required": True})
                sid = prim.create_session(who["name"], "token")
                prim.audit(who["name"], "login", {"via": "token", "mfa": bool(who.get("mfa"))})
                return self._json(200, {"user": {"name": who["name"], "role": who["role"]}}, cookies=[self.session_cookie(sid)])
            import hmac
            if legacy and hmac.compare_digest(tok.encode(), legacy.encode()):
                prim.audit("token-admin", "login", {"via": "legacy_token"})
                return self._json(200, {"user": {"name": "token-admin", "role": "admin"}}, cookies=[self.session_cookie(self._legacy_sid())])
            prim.attempt_failed(ip)
            return self._json(401, {"error": tr("invalid token")})

        def _mfa(self, prim: Store, body: dict):
            """MFA for the person themself; an admin may disable it for others."""
            who = self._identity(prim)
            if who is None or not prim.has_users():
                return self._json(401, {"error": tr("authentication required")})
            act = body.get("action")
            target = str(body.get("name") or who["name"])
            if act == "begin" and target == who["name"]:
                secret = prim.mfa_begin(who["name"])
                return self._json(200, {"secret": secret, "uri": totp.uri(secret, who["name"])})
            if act == "confirm" and target == who["name"]:
                if not prim.mfa_confirm(who["name"], str(body.get("code", ""))):
                    return self._json(400, {"error": tr("invalid code")})
                prim.audit(who["name"], "mfa_enabled", {})
                return self._json(200, {"ok": True, "recovery_codes": prim.mfa_recovery_codes(who["name"]),
                                        "note": tr("keep these codes: each works once and they are not shown again")})
            if act == "recovery" and target == who["name"]:
                if not prim.mfa_check(who["name"], str(body.get("code", ""))):
                    return self._json(400, {"error": tr("enter the current authenticator code")})
                prim.audit(who["name"], "mfa_recovery_regenerated", {})
                return self._json(200, {"recovery_codes": prim.mfa_recovery_codes(who["name"])})
            if act == "disable":
                if target != who["name"] and who["role"] != "admin":
                    return self._json(403, {"error": tr("only an admin can disable someone else's MFA")})
                if target == who["name"] and not prim.mfa_check(who["name"], str(body.get("code", ""))):
                    return self._json(400, {"error": tr("enter the current code to disable")})
                prim.mfa_disable(target)
                prim.audit(who["name"], "mfa_disabled", {"user": target})
                return self._json(200, {"ok": True})
            return self._json(400, {"error": tr("invalid action")})

        def _users(self, prim: Store, body: dict):
            """Global administration of people: admin on the main project."""
            who = self._global_admin(prim)
            actor, action, name = who["name"], body.get("action"), str(body.get("name", ""))
            if action == "add":
                t = prim.add_user(name, str(body.get("role", "viewer")), expires_days=float(body["expires_days"]) if body.get("expires_days") else
                                  (float(prim.get_setting("policy.token_max_age_days")) if prim.get_setting("policy.token_max_age_days") else None))
                prim.audit(actor, "user_add", {"name": name, "role": body.get("role")})
                return self._json(200, {"token": t, "note": tr("save it now: the token is not shown again")})
            if action == "rotate":
                t = prim.rotate_token(name)
                prim.audit(actor, "user_rotate", {"name": name})
                return self._json(200, {"token": t})
            if action in ("disable", "enable", "role"):
                admins = [x for x in prim.users() if x["role"] == "admin" and not x["disabled"] and x["name"] != name]
                if (action == "disable" or (action == "role" and body.get("role") != "admin")) and not admins:
                    raise ValueError(tr("cannot remove the last active admin"))
                prim.set_user(name, role=body.get("role") if action == "role" else None,
                              disabled=(action == "disable") if action in ("disable", "enable") else None)
                prim.audit(actor, "user_" + action, {"name": name})
                return self._json(200, {"ok": True})
            raise ValueError(tr("unknown action"))

        def _diff(self, prim: Store, body: dict):
            who = self._need(prim, "diff")
            if not scope_ok(who, self._project(), None, auth.role_in(who, self._project())):
                return self._json(403, {"error": tr("this action is outside your scope (allowed folders/models)")})      # samples rows of any relation
            st = self._data(prim, who)
            from . import datadiff, warehouses
            if not (st.get_setting("warehouse.type") or st.get_setting("warehouse.duckdb") or st.get_setting("warehouse.postgres_env")):
                raise ValueError(tr("configure the warehouse (warehouse.type, warehouse.duckdb or warehouse.postgres_env) in Settings"))
            key = [k.strip() for k in str(body.get("key", "")).split(",") if k.strip()]
            if not (body.get("base") and body.get("head") and key):
                raise ValueError(tr("enter the base relation, the new relation and the key"))
            wh = warehouses.connect(st.get_setting)
            try:
                res = datadiff.diff_relations(wh, str(body["base"]), str(body["head"]), key, int(body.get("sample", 5)),
                                             sample_pct=float(body["sample_pct"]) if body.get("sample_pct") else None)
            except Exception as e:
                raise ValueError(tr("diff failed: {error}", error=str(e)[:300])) from e
            finally:
                if wh is not None:
                    wh.close()
            st.audit(who["name"], "data_diff", {"base": body["base"], "head": body["head"], "identical": res["identical"]})
            return self._json(200, res)

        def _run(self, prim: Store, what: str):
            who = self._need(prim, "run_check" if what == "check" else "run_collect")
            st = self._data(prim, who)
            if not st.claim_setting("state.running", what):      # atomic: two clicks/requests cannot both start a run
                raise ValueError(tr("a run is already in progress"))
            spath, code = st.path, i18n.lang()

            def job():
                i18n.set_request_lang(code)
                s2 = None
                try:
                    s2 = Store(spath)          # inside the try: if it fails (pool exhausted) the flag is still cleared below
                    if what == "check":
                        runner.run_check(s2)
                    else:
                        runner.collect_runs(s2)
                        runner.collect_samples(s2)
                except Exception as e:
                    try:
                        (s2 or Store(spath)).audit("system", "run_failed", {"what": what, "error": str(e)[:300]})
                    except Exception:
                        pass
                finally:
                    try:
                        (s2 or Store(spath)).unset_setting("state.running")
                    finally:
                        if s2 is not None:
                            s2.close()
            threading.Thread(target=job, daemon=True).start()
            return self._json(200, {"started": what})

        def _finding_action(self, prim: Store, fid: str, act: str, body: dict):
            need = {"ack": "ack", "false-positive": "false_positive", "assign": "assign", "note": "note"}.get(act)
            if need is None:
                return self._json(404, {"error": "not found"})
            who = self._need(prim, need)
            st = self._data(prim, who)
            actor = who["name"]
            frec = st.finding(fid)
            if not scope_ok(who, self._project(), frec, auth.role_in(who, self._project())):
                return self._json(403, {"error": tr("this finding is outside your scope (allowed folders/models)")})
            if act == "ack":
                st.ack(fid, actor, tr("acknowledged in the UI"))
            elif act == "false-positive":
                st.mark_false_positive(fid, actor, tr("marked in the UI"))
            elif act == "assign":
                a = body.get("assignee") or None
                if a and prim.has_users() and not any(x["name"] == a and not x["disabled"] for x in prim.users()):
                    raise ValueError(tr("user not found"))
                st.assign(fid, a, actor)
            else:
                st.add_note(fid, actor, str(body.get("text", "")))
            return self._json(200, {"ok": True})
    return H


def build_server(store_path: str, port: int = 8787, host: str = "127.0.0.1", tls_cert: str | None = None, tls_key: str | None = None,
                 behind_proxy: bool = False):
    """Create the server applying the exposure rules: outside loopback requires users and TLS (or a declared proxy)."""
    loopback = host in ("127.0.0.1", "localhost", "::1")
    with Store(store_path) as st:
        has_users = st.has_users()
        st.unset_setting("state.running")      # a run interrupted by a crash/restart would otherwise block new runs forever
    if behind_proxy and not os.environ.get("INTENTFLOW_TRUSTED_PROXIES"):
        import sys
        print(tr("warning: --behind-proxy without INTENTFLOW_TRUSTED_PROXIES: every client shares the proxy IP for the login rate limit"), file=sys.stderr)
    if not loopback:
        if not has_users:
            raise SystemExit(tr("refused: to listen outside loopback, add users first (`intentflow user add NAME --role admin`)"))
        if not (tls_cert and tls_key) and not behind_proxy:
            raise SystemExit(tr("refused: outside loopback use HTTPS (--tls-cert/--tls-key) or declare --behind-proxy (reverse proxy with TLS)"))
    from .http_utils import tls_context
    ctx = tls_context(tls_cert, tls_key) if tls_cert and tls_key else None
    try:
        max_workers = int(os.environ.get("INTENTFLOW_UI_MAX_WORKERS", "128"))
    except ValueError:
        raise SystemExit(tr("INTENTFLOW_UI_MAX_WORKERS must be an integer between 1 and 1024")) from None
    try:
        srv = BoundedThreadingHTTPServer((host, port), make_handler(store_path), max_workers=max_workers)
    except ValueError:
        raise SystemExit(tr("INTENTFLOW_UI_MAX_WORKERS must be an integer between 1 and 1024")) from None
    if ctx:
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        srv.scheme = "https"
    else:
        srv.scheme = "http"
    return srv


def serve(store_path: str, port: int = 8787, host: str = "127.0.0.1", tls_cert: str | None = None, tls_key: str | None = None,
          behind_proxy: bool = False):
    srv = build_server(store_path, port, host, tls_cert, tls_key, behind_proxy)
    with Store(store_path) as st:
        team = st.has_users()
    print(f"IntentFlow UI: {srv.scheme}://{host}:{srv.server_address[1]}/  " + (tr("(team mode: token login)") if team else tr("(local mode)")))
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
