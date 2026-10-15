"""MFA (TOTP), sessions, OIDC SSO against a simulated IdP, per-project roles, persistent attempt limit and real concurrency."""
from conftest import new_db
import base64, hashlib, json, os, secrets, subprocess, sys, threading, time, urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from intentflow import runner, totp
from intentflow.model import Finding
from intentflow.store import Store
from intentflow.web import make_handler

ROOT = Path(__file__).resolve().parent.parent


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPEN = urllib.request.build_opener(NoRedirect)


def req(url, method="GET", headers=None, body=None):
    r = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with OPEN.open(r, timeout=20) as resp:
            return resp.status, resp.read(), {**dict(resp.headers), "_cookies": resp.headers.get_all("Set-Cookie") or []}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {**dict(e.headers), "_cookies": e.headers.get_all("Set-Cookie") or []}


def serve(db, **kw):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, **kw))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def call(base, method, path, body=None, headers=None):
    h = {"X-IntentFlow": "1", **(headers or {})}
    st, raw, hd = req(base + path, method, h, json.dumps(body if body is not None else {}).encode() if method == "POST" else None)
    return st, json.loads(raw or b"{}"), hd


# ------------------------------------------------------------------ MFA and session
def test_totp_rfc6238_vectors_and_replay_protection():
    sec = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(sec, 59 // 30, 8) == "94287082"
    assert totp.verify(sec, totp.code_at(sec, 1000), now=1000 * 30 + 5) == 1000
    assert totp.verify(sec, totp.code_at(sec, 1000), now=1000 * 30 + 5, last_step=1000) is None     # reuso
    assert totp.verify(sec, "000000", now=1000 * 30) is None
    assert totp.verify(sec, totp.code_at(sec, 1005), now=1000 * 30) is None                          # fora da janela


def test_mfa_login_flow_and_session_cookie_hides_the_token(tmp_path):
    db = new_db(tmp_path, "m.db")
    st = Store(db)
    tok = st.add_user("alice", "admin")
    srv, base = serve(db)
    try:
        # ativa MFA pela API (autenticado por token)
        auth = {"Authorization": f"Bearer {tok}"}
        code, begin, _ = call(base, "POST", "/api/mfa", {"action": "begin"}, auth)
        assert code == 200 and begin["uri"].startswith("otpauth://totp/")
        secret = begin["secret"]
        assert call(base, "POST", "/api/mfa", {"action": "confirm", "code": "000000"}, auth)[0] == 400
        assert call(base, "POST", "/api/mfa", {"action": "confirm", "code": totp.code_at(secret, int(time.time() // 30))}, auth)[0] == 200
        # a bare token no longer works (MFA on)
        assert call(base, "GET", "/api/overview", headers=auth)[0] == 401
        # login requires the code
        code, body, _ = call(base, "POST", "/api/login", {"token": tok})
        assert code == 401 and body["mfa_required"] is True
        assert call(base, "POST", "/api/login", {"token": tok, "code": "123456"})[0] == 401
        # the step used in the confirmation cannot be reused; use the next one
        nxt = totp.code_at(secret, int(time.time() // 30) + 1)
        code, body, hd = call(base, "POST", "/api/login", {"token": tok, "code": nxt})
        assert code == 200
        cookie = hd["Set-Cookie"].split(";")[0]
        assert cookie.startswith("if_session=ifs_") and tok not in cookie             # cookie = session, not the token
        assert call(base, "GET", "/api/overview", headers={"Cookie": cookie})[0] == 200
        assert call(base, "POST", "/api/login", {"token": tok, "code": nxt})[0] == 401   # replay of the same code
        # logout invalidates the session
        call(base, "POST", "/api/logout", headers={"Cookie": cookie})
        assert call(base, "GET", "/api/overview", headers={"Cookie": cookie})[0] == 401
        assert any(a["event"] == "mfa_enabled" for a in st.audit_log())
    finally:
        srv.shutdown()


def test_token_cannot_be_used_as_cookie_value(tmp_path):
    db = new_db(tmp_path, "c.db")
    st = Store(db)
    tok = st.add_user("bob", "engineer")
    srv, base = serve(db)
    try:
        assert call(base, "GET", "/api/overview", headers={"Cookie": f"if_session={tok}"})[0] == 401
        assert call(base, "GET", "/api/overview", headers={"Authorization": f"Bearer {tok}"})[0] == 200
    finally:
        srv.shutdown()


def test_login_throttle_survives_restart(tmp_path):
    db = new_db(tmp_path, "t.db")
    Store(db).add_user("alice", "admin")
    a, base_a = serve(db)
    codes = [call(base_a, "POST", "/api/login", {"token": "if_x"})[0] for _ in range(11)]
    a.shutdown()
    assert codes[:10] == [401] * 10 and codes[10] == 429
    b, base_b = serve(db)                                           # "restart": new process, same store
    try:
        assert call(base_b, "POST", "/api/login", {"token": "if_x"})[0] == 429
    finally:
        b.shutdown()


# ------------------------------------------------------------------ SSO OIDC
class IdP(BaseHTTPRequestHandler):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    codes: dict = {}
    claims_override: dict = {}
    base = ""

    def log_message(self, *a):
        pass

    def _json(self, o, code=200):
        b = json.dumps(o).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/.well-known/openid-configuration":
            return self._json({"issuer": IdP.base, "authorization_endpoint": IdP.base + "/authorize", "token_endpoint": IdP.base + "/token",
                               "jwks_uri": IdP.base + "/jwks", "end_session_endpoint": IdP.base + "/logout"})
        if u.path == "/jwks":
            n = IdP.key.public_key().public_numbers()
            return self._json({"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                                         "n": b64u(n.n.to_bytes(256, "big")), "e": b64u(n.e.to_bytes(3, "big"))}]})
        if u.path == "/authorize":
            code = secrets.token_hex(8)
            IdP.codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"], "client": q["client_id"], "redirect": q["redirect_uri"]}
            self.send_response(302)
            self.send_header("Location", q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]}))
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    revoked: set = set()
    refresh_map: dict = {}

    @staticmethod
    def sign(claims):
        h = b64u(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        p = b64u(json.dumps(claims).encode())
        sig = IdP.key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{b64u(sig)}"

    def do_POST(self):
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        now = int(time.time())
        if form.get("grant_type") == "refresh_token":
            rt = form.get("refresh_token")
            if rt in IdP.revoked or rt not in IdP.refresh_map:
                return self._json({"error": "invalid_grant"}, 400)
            base = IdP.refresh_map[rt]
            claims = {**base, "exp": now + 300, "iat": now, **IdP.claims_override}
            claims.pop("nonce", None)
            new_rt = rt + "+"
            IdP.refresh_map[new_rt] = base
            return self._json({"id_token": IdP.sign(claims), "refresh_token": new_rt, "access_token": "x", "token_type": "Bearer"})
        c = IdP.codes.pop(form.get("code"), None)
        if not c or c["client"] != form["client_id"] or form.get("client_secret") != "s3cr3t":
            return self._json({"error": "invalid_grant"}, 400)
        if b64u(hashlib.sha256(form["code_verifier"].encode()).digest()) != c["challenge"]:
            return self._json({"error": "pkce"}, 400)
        sid = "sess-" + secrets.token_hex(4)
        base = {"iss": IdP.base, "aud": c["client"], "sub": "u-123", "sid": sid, "email": "maria@exemplo.com", "email_verified": True}
        claims = {**base, "exp": now + 300, "iat": now, "nonce": c["nonce"], **IdP.claims_override}
        rt = "rt-" + secrets.token_hex(6)
        IdP.refresh_map[rt] = {**base, **{k: v for k, v in IdP.claims_override.items() if k in ("email",)}}
        self._json({"id_token": IdP.sign(claims), "refresh_token": rt, "access_token": "x", "token_type": "Bearer"})


# ------------------------------------------------------------------ projetos


# ------------------------------------------------------------------ real concurrency
def test_parallel_cli_processes_share_one_store(tmp_path):
    db = new_db(tmp_path, "pc.db")
    st = Store(db)
    st.sync_findings([Finding("DBT001", "high", "t", nodes=["a"])], {"DBT001"})
    fid = st.findings("open")[0]["id"]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    procs = [subprocess.Popen([sys.executable, "-m", "intentflow.cli", "--store", db, "note", fid, f"nota {i}", "--user", f"u{i}"],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(12)]
    procs += [subprocess.Popen([sys.executable, "-m", "intentflow.cli", "--store", db, "config", "set", f"k{i}", str(i)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(12)]
    outs = [(p.wait(timeout=120), p.stderr.read().decode()) for p in procs]
    assert [o for o in outs if o[0] != 0] == []
    assert len(Store(db).notes(fid)) == 12 and len(Store(db).settings("k")) == 12


def test_load_with_many_simultaneous_users_has_no_errors(tmp_path):
    db = new_db(tmp_path, "ld.db")
    st = Store(db)
    st.sync_findings([Finding("DBT001", "high", f"t{i}", nodes=[f"n{i}"]) for i in range(20)], {"DBT001"})
    fids = [f["id"] for f in st.findings("open")]
    toks = [st.add_user(f"u{i}", "engineer") for i in range(30)]
    srv, base = serve(db)
    errs, lat = [], []
    lock = threading.Lock()

    def person(i):
        h = {"Authorization": f"Bearer {toks[i]}"}
        for j in range(15):
            t = time.time()
            try:
                c1 = call(base, "GET", "/api/overview", headers=h)[0]
                c2 = call(base, "POST", f"/api/findings/{fids[(i + j) % len(fids)]}/note", {"text": f"{i}-{j}"}, h)[0]
            except Exception as e:
                with lock:
                    errs.append((i, j, repr(e)[:60]))
                continue
            with lock:
                lat.append(time.time() - t)
                if c1 != 200 or c2 != 200:
                    errs.append((i, j, c1, c2))
    try:
        ts = [threading.Thread(target=person, args=(i,)) for i in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        srv.shutdown()
    assert errs == []
    lat.sort()
    print(f"\n30 usuários × 15 ciclos: p50={lat[len(lat)//2]*1000:.0f} ms p95={lat[int(len(lat)*.95)]*1000:.0f} ms max={lat[-1]*1000:.0f} ms")
    assert lat[int(len(lat) * .95)] < 10.0


# ------------------------------------------------------------------ groups → roles and single logout (simulated IdP)


# ------------------------------------------------------------------ back-channel, refresh, rebaixamento, overage


# ------------------------------------------------------------------ proxy, token validity, recovery, scopes
def test_throttle_uses_forwarded_ip_only_from_trusted_proxy(tmp_path, monkeypatch):
    db = new_db(tmp_path, "px.db")
    Store(db).add_user("alice", "admin")
    monkeypatch.setenv("INTENTFLOW_TRUSTED_PROXIES", "127.0.0.1")
    srv, base = serve(db)
    try:
        for i in range(10):                                               # 10 failures of client A (through a trusted proxy)
            assert call(base, "POST", "/api/login", {"token": "if_x"}, {"X-Forwarded-For": "203.0.113.7"})[0] == 401
        assert call(base, "POST", "/api/login", {"token": "if_x"}, {"X-Forwarded-For": "203.0.113.7"})[0] == 429
        assert call(base, "POST", "/api/login", {"token": "if_x"}, {"X-Forwarded-For": "203.0.113.8"})[0] == 401   # another client is not blocked
    finally:
        srv.shutdown()
    monkeypatch.setenv("INTENTFLOW_TRUSTED_PROXIES", "")
    db2 = new_db(tmp_path, "px2.db")
    Store(db2).add_user("alice", "admin")
    srv, base = serve(db2)                                                # without a trusted proxy: XFF is ignored (the limit cannot be dodged)
    try:
        codes = [call(base, "POST", "/api/login", {"token": "if_x"}, {"X-Forwarded-For": f"198.51.100.{i}"})[0] for i in range(11)]
        assert codes[:10] == [401] * 10 and codes[10] == 429
    finally:
        srv.shutdown()


def test_token_expiry_and_rotation(tmp_path):
    st = Store(new_db(tmp_path, "e.db"))
    t = st.add_user("bob", "engineer", expires_days=1)
    assert st.user_by_token(t)["name"] == "bob"
    st.con.execute("update users set token_expires=? where name='bob'", (time.time() - 5,))
    st.con.commit()
    assert st.user_by_token(t) is None                                    # expirou
    t2 = st.rotate_token("bob", expires_days=30)
    assert st.user_by_token(t2)["name"] == "bob" and st.user_by_token(t) is None
    st.set_setting("policy.token_max_age_days", 7)
    t3 = st.rotate_token("bob")                                           # inherits the policy validity
    exp = st.user_record("bob")["token_expires"]
    assert 6.9 * 86400 < exp - time.time() < 7.1 * 86400 and st.user_by_token(t3)
    # rotating ends open sessions
    st.create_session("bob")
    st.rotate_token("bob")
    assert st.con.execute("select count(*) from sessions where user='bob'").fetchone()[0] == 0


def test_recovery_codes_work_once_and_are_regenerated_with_totp(tmp_path):
    db = new_db(tmp_path, "r.db")
    st = Store(db)
    tok = st.add_user("alice", "admin")
    srv, base = serve(db)
    try:
        A = {"Authorization": f"Bearer {tok}"}
        secret = call(base, "POST", "/api/mfa", {"action": "begin"}, A)[1]["secret"]
        step = int(time.time() // 30)
        code, out, _ = call(base, "POST", "/api/mfa", {"action": "confirm", "code": totp.code_at(secret, step)}, A)
        rc = out["recovery_codes"]
        assert code == 200 and len(rc) == 10 and len(set(rc)) == 10
        assert call(base, "POST", "/api/login", {"token": tok, "code": rc[0]})[0] == 200          # a recovery code works as the 2nd factor
        assert call(base, "POST", "/api/login", {"token": tok, "code": rc[0]})[0] == 401          # single use
        assert call(base, "POST", "/api/login", {"token": tok, "code": rc[1]})[0] == 200
        assert st.con.execute("select recovery_hashes from users").fetchone()[0].count(",") == 7   # 8 restantes
        assert rc[2] not in st.con.execute("select recovery_hashes from users").fetchone()[0]       # only hashes in the database
        # regenerar exige o TOTP atual e invalida os antigos
        s2, hdr = call(base, "POST", "/api/login", {"token": tok, "code": totp.code_at(secret, step + 1)})[0:3:2]
        assert s2 == 200
        cookie = hdr["Set-Cookie"].split(";")[0]
        assert call(base, "POST", "/api/mfa", {"action": "recovery", "code": "000000"}, {"Cookie": cookie})[0] == 400
        st.con.execute("update users set totp_last_step=0")      # emulate the next 30 s window (replay protection already used this one)
        st.con.commit()
        new = call(base, "POST", "/api/mfa", {"action": "recovery", "code": totp.code_at(secret, step + 1)}, {"Cookie": cookie})[1]["recovery_codes"]
        assert len(new) == 10 and call(base, "POST", "/api/login", {"token": tok, "code": rc[3]})[0] == 401
    finally:
        srv.shutdown()


def test_load_100_users_with_overview_cache(tmp_path):
    db = new_db(tmp_path, "l100.db")
    st = Store(db)
    st.sync_findings([Finding("DBT001", "high", f"t{i}", nodes=[f"n{i}"]) for i in range(30)], {"DBT001"})
    fids = [f["id"] for f in st.findings("open")]
    toks = [st.add_user(f"u{i}", "engineer") for i in range(100)]
    srv, base = serve(db)
    errs, lat = [], []
    lock = threading.Lock()

    def person(i):
        h = {"Authorization": f"Bearer {toks[i]}"}
        for j in range(8):
            t = time.time()
            try:
                c1 = call(base, "GET", "/api/overview", headers=h)[0]
                c2 = call(base, "POST", f"/api/findings/{fids[(i + j) % len(fids)]}/note", {"text": f"{i}-{j}"}, h)[0]
            except Exception as e:      # a dropped connection counts as an error (it used to be only a warning)
                with lock:
                    errs.append((i, j, repr(e)[:60]))
                continue
            with lock:
                lat.append(time.time() - t)
                if c1 != 200 or c2 != 200:
                    errs.append((i, j, c1, c2))
    try:
        ts = [threading.Thread(target=person, args=(i,)) for i in range(100)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        srv.shutdown()
    lat.sort()
    print(f"\n100 usuários × 8 ciclos: p50={lat[len(lat)//2]*1000:.0f} ms p95={lat[int(len(lat)*.95)]*1000:.0f} ms max={lat[-1]*1000:.0f} ms")
    assert errs == []
    assert lat[int(len(lat) * .95)] < 15.0


def test_overview_cache_coalesces_simultaneous_builds(tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from intentflow import overview

    db = new_db(tmp_path, "overview-herd.db")
    with Store(db) as store:
        store.set_setting("test.cache_generation", time.time())
        entered = 0
        guard = threading.Lock()
        original = overview.build
        def slow_build(s):
            nonlocal entered
            with guard:
                entered += 1
            time.sleep(0.15)
            return original(s)
        monkeypatch.setattr(overview, "build", slow_build)
        barrier = threading.Barrier(24)
        def fetch(_):
            barrier.wait(timeout=60)           # they start together WITHOUT holding a connection (the PostgreSQL pool is smaller than 24)
            with Store(db) as local:
                return overview.build_cached(local, ttl=60)
        with ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(fetch, range(24)))
    assert entered == 1
    assert all(result is results[0] for result in results)

def test_overview_cache_invalidates_on_write(tmp_path):
    from intentflow import overview
    st = Store(new_db(tmp_path, "oc.db"))
    a = overview.build_cached(st, ttl=60)
    assert overview.build_cached(st, ttl=60) is a                 # mesmo objeto: veio do cache
    st.sync_findings([Finding("DBT001", "high", "novo", nodes=["x"])], {"DBT001"})
    b = overview.build_cached(st, ttl=60)
    assert b is not a and b["summary"]["open_findings"] == 1      # escrita invalida


def test_ui_exposes_security_controls_and_js_is_valid(tmp_path):
    import re, shutil, subprocess
    html = (Path(__file__).resolve().parent.parent / "src/intentflow/web.html").read_text()
    for marker in ("Scope per folder", "recoveryModal", "expires_days", "Generate new codes", "Token expires"):
        assert marker in html, marker
    if shutil.which("node"):
        js = tmp_path / "ui.js"
        js.write_text(re.search(r"<script>(.*)</script>", html, re.S).group(1))
        assert subprocess.run(["node", "--check", str(js)], capture_output=True).returncode == 0
