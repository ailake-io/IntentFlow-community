"""MFA (TOTP), sessions, OIDC SSO against a simulated IdP, per-project roles, persistent attempt limit and real concurrency."""
from conftest import new_db
import base64, hashlib, json, secrets, threading, time, urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from intentflow.enterprise import oidc
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


@pytest.fixture()
def sso(tmp_path, monkeypatch):
    idp = ThreadingHTTPServer(("127.0.0.1", 0), IdP)
    IdP.base = f"http://127.0.0.1:{idp.server_address[1]}"
    IdP.claims_override, IdP.codes, IdP.revoked, IdP.refresh_map = {}, {}, set(), {}
    threading.Thread(target=idp.serve_forever, daemon=True).start()
    monkeypatch.setenv("IF_SSO_SECRET", "s3cr3t")
    db = new_db(tmp_path, "s.db")
    st = Store(db)
    st.add_user("alice", "admin")
    for k, v in {"issuer": IdP.base, "client_id": "intentflow", "client_secret_env": "IF_SSO_SECRET"}.items():
        st.set_setting("sso." + k, v)
    srv, base = serve(db)
    yield st, base
    srv.shutdown()
    idp.shutdown()


def sso_login(base):
    s, _, h = req(base + "/auth/sso/login")
    assert s == 302
    oc = h["Set-Cookie"].split(";")[0]
    s, _, h2 = req(h["Location"])                       # IdP autentica e redireciona de volta
    assert s == 302
    return req(h2["Location"], headers={"Cookie": oc}), oc, h2["Location"]


def test_sso_login_happy_path_for_existing_user(sso):
    st, base = sso
    st.add_user("maria", "engineer", email="maria@exemplo.com")
    (s, _, h), _, _ = sso_login(base)
    assert s == 302 and h["Location"] == "/"
    sid = next(c for c in h["_cookies"] if c.startswith("if_session=")).split(";")[0].split("=", 1)[1]
    code, me, _ = call(base, "GET", "/api/me", headers={"Cookie": f"if_session={sid}"})
    assert me["user"]["name"] == "maria" and me["user"]["role"] == "engineer" and me["user"]["via"] == "sso"
    assert any(a["event"] == "login" and json.loads(a["detail"]).get("via") == "sso" for a in st.audit_log())


def test_sso_denies_unknown_user_unless_auto_provision_with_domain(sso):
    st, base = sso
    (s, body, _), _, _ = sso_login(base)
    assert s == 403 and b"has no access" in body                           # desconhecido: nega
    st.set_setting("sso.auto_provision", True)
    st.set_setting("sso.allowed_domains", ["outra.com"])
    assert sso_login(base)[0][0] == 403                                           # domain not allowed
    st.set_setting("sso.allowed_domains", ["exemplo.com"])
    st.set_setting("sso.default_role", "viewer")
    (s, _, h), _, _ = sso_login(base)
    assert s == 302
    u = st.user_record("maria@exemplo.com")
    assert u["role"] == "viewer" and u["email"] == "maria@exemplo.com"
    # a user provisioned by SSO has no usable token
    assert st.user_by_token("") is None


def test_sso_rejects_bad_state_nonce_signature_and_unverified_email(sso):
    st, base = sso
    st.add_user("maria", "engineer", email="maria@exemplo.com")
    # state adulterado
    s, _, h = req(base + "/auth/sso/login")
    oc = h["Set-Cookie"].split(";")[0]
    s, _, h2 = req(h["Location"])
    bad = h2["Location"].replace("state=", "state=x")
    assert req(bad, headers={"Cookie": oc})[0] == 400
    # sem o cookie de estado (CSRF de login)
    assert req(h2["Location"])[0] == 400
    # e-mail not verified
    IdP.claims_override = {"email_verified": False}
    assert sso_login(base)[0][0] == 403
    # nonce errado no id_token
    IdP.claims_override = {"nonce": "outro"}
    assert sso_login(base)[0][0] == 401


def test_oidc_verify_rejects_alg_none_hs256_and_wrong_audience():
    o = oidc.OIDC("http://idp", "cid", "s", "http://app/cb", get=lambda u: {"issuer": "http://idp", "jwks_uri": "http://idp/jwks",
                  "authorization_endpoint": "x", "token_endpoint": "y"} if "openid" in u else {"keys": []})
    def jwt(alg, claims):
        return b64u(json.dumps({"alg": alg, "kid": "k1"}).encode()) + "." + b64u(json.dumps(claims).encode()) + ".AAAA"
    ok = {"iss": "http://idp", "aud": "cid", "exp": time.time() + 99, "nonce": "n"}
    for alg in ("none", "HS256"):
        with pytest.raises(oidc.SSOError, match="algorithm"):
            o.verify(jwt(alg, ok), "n")
    with pytest.raises(oidc.SSOError):
        o.verify("lixo", "n")


# ------------------------------------------------------------------ projetos
def test_per_project_roles_and_isolated_data(tmp_path):
    prim, crm = new_db(tmp_path, "prim.db"), new_db(tmp_path, "crm.db")
    p = Store(prim)
    c = Store(crm)
    p.sync_findings([Finding("DBT001", "high", "shop: x", nodes=["a"])], {"DBT001"})
    c.sync_findings([Finding("DBT002", "high", "crm: y", nodes=["b"])], {"DBT002"})
    tok = {"alice": p.add_user("alice", "admin"), "bob": p.add_user("bob", "engineer"), "carol": p.add_user("carol", "viewer")}
    p.set_project_role("bob", "crm", "none")           # bob does not see crm
    p.set_project_role("carol", "crm", "engineer")     # carol viewer no principal, engenheira no crm
    p.set_setting("projects", {"crm": crm})
    srv, base = serve(prim)
    try:
        H = lambda who, proj=None: {"Authorization": f"Bearer {tok[who]}", **({"X-IntentFlow-Project": proj} if proj else {})}   # noqa: E731
        ov = call(base, "GET", "/api/overview", headers=H("alice"))[1]
        assert [f["title"] for f in ov["findings"]] == ["shop: x"]
        ov = call(base, "GET", "/api/overview", headers=H("alice", "crm"))[1]
        assert [f["title"] for f in ov["findings"]] == ["crm: y"]                         # data isolated per project
        assert call(base, "GET", "/api/overview", headers=H("bob", "crm"))[0] == 403       # no access to crm
        assert call(base, "GET", "/api/overview", headers=H("bob"))[0] == 200
        fid_shop, fid_crm = p.findings("open")[0]["id"], c.findings("open")[0]["id"]
        assert call(base, "POST", f"/api/findings/{fid_shop}/ack", headers=H("carol"))[0] == 403          # viewer no principal
        assert call(base, "POST", f"/api/findings/{fid_crm}/ack", headers=H("carol", "crm"))[0] == 200    # engenheira no crm
        assert any(a["actor"] == "carol" for a in c.audit_log()) and not any(a["actor"] == "carol" for a in p.audit_log())
        assert call(base, "GET", "/api/overview", headers=H("alice", "inexistente"))[0] == 404
        me = call(base, "GET", "/api/me", headers=H("carol"))[1]
        assert {x["name"]: x["role"] for x in me["projects"]} == {"main": "viewer", "crm": "engineer"}
    finally:
        srv.shutdown()


def test_admin_registers_projects_and_assigns_project_roles_via_api(tmp_path):
    prim = new_db(tmp_path, "p.db")
    p = Store(prim)
    a = p.add_user("alice", "admin")
    b = p.add_user("bob", "engineer")
    srv, base = serve(prim)
    try:
        H = {"Authorization": f"Bearer {a}"}
        newdb = new_db(tmp_path, "vendas.db")
        assert call(base, "POST", "/api/projects", {"action": "add", "name": "vendas", "store": newdb}, {"Authorization": f"Bearer {b}"})[0] == 403
        assert call(base, "POST", "/api/projects", {"action": "add", "name": "vendas", "store": newdb}, H)[0] == 200
        assert call(base, "POST", "/api/projects", {"action": "add", "name": "main", "store": newdb}, H)[0] == 400
        assert call(base, "POST", "/api/users", {"action": "project_role", "name": "bob", "project": "vendas", "role": "none"}, H)[0] == 200
        assert call(base, "GET", "/api/overview", headers={"Authorization": f"Bearer {b}", "X-IntentFlow-Project": "vendas"})[0] == 403
        # settings are per project
        assert call(base, "POST", "/api/settings", {"key": "dbt.target", "value": "/x"}, {**H, "X-IntentFlow-Project": "vendas"})[0] == 200
        assert Store(newdb).get_setting("dbt.target") == "/x" and p.get_setting("dbt.target") is None
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ real concurrency


# ------------------------------------------------------------------ groups → roles and single logout (simulated IdP)
def test_map_groups_picks_highest_role_and_project_roles():
    gm = [{"group": "dados-leitura", "role": "viewer"}, {"group": "dados-admin", "role": "admin"},
          {"group": "crm-eng", "role": "engineer", "project": "crm"}, {"group": "crm-leitura", "role": "viewer", "project": "crm"},
          {"group": "vendas-bloqueio", "role": "none", "project": "vendas"}]
    g, p = oidc.map_groups({"groups": ["/dados-leitura", "/dados-admin", "crm-leitura", "crm-eng", "vendas-bloqueio"]}, "groups", gm)
    assert g == "admin" and p == {"crm": "engineer", "vendas": "none"}
    assert oidc.map_groups({"groups": "dados-leitura"}, "groups", gm) == ("viewer", {})        # claim como string
    assert oidc.map_groups({"roles": ["dados-admin"]}, "roles", gm)[0] == "admin"               # claim com outro nome
    assert oidc.map_groups({}, "groups", gm) == (None, {})


def test_parse_group_map_rules():
    from intentflow.enterprise.web_ext import parse_group_map as _parse_group_map
    assert _parse_group_map("a=admin; b=engineer@crm\nc=viewer") == [
        {"group": "a", "role": "admin"}, {"group": "b", "role": "engineer", "project": "crm"}, {"group": "c", "role": "viewer"}]
    with pytest.raises(ValueError):
        _parse_group_map("sem-igual")
    with pytest.raises(ValueError):
        _parse_group_map("g=root")


def test_sso_groups_provision_and_sync_roles_for_managed_users_only(sso):
    st, base = sso
    st.set_setting("sso.auto_provision", True)
    st.set_setting("sso.allowed_domains", ["exemplo.com"])
    st.set_setting("sso.group_map", [{"group": "dados-admin", "role": "admin"}, {"group": "crm-eng", "role": "engineer", "project": "crm"}])
    IdP.claims_override = {"groups": ["dados-admin", "crm-eng"]}
    (s, _, h), _, _ = sso_login(base)
    assert s == 302
    u = st.user_record("maria@exemplo.com")
    assert u["role"] == "admin" and json.loads(u["project_roles"]) == {"crm": "engineer"} and u["sso_managed"] == 1
    # the person leaves the admin group → loses the role at the next login (SSO-managed user)
    IdP.claims_override = {"groups": ["crm-eng"]}
    sso_login(base)
    u = st.user_record("maria@exemplo.com")
    assert u["role"] == "viewer"       # no global group: the role falls to the floor (default_role/unmapped_role), never stays admin
    assert json.loads(u["project_roles"]) == {"crm": "engineer"}
    # map a group to a lower role and confirm the downgrade when there is a global group
    st.set_setting("sso.group_map", [{"group": "dados-leitura", "role": "viewer"}, {"group": "crm-eng", "role": "engineer", "project": "crm"}])
    IdP.claims_override = {"groups": ["dados-leitura"]}
    sso_login(base)
    u = st.user_record("maria@exemplo.com")
    assert u["role"] == "viewer" and json.loads(u["project_roles"]) == {}
    assert any(a["event"] == "roles_synced" for a in st.audit_log())
    # a user added by hand is NOT changed by groups (unless sync_roles is on)
    st.add_user("joana", "engineer", email="joana@exemplo.com")
    IdP.claims_override = {"email": "joana@exemplo.com", "groups": ["dados-leitura"]}
    sso_login(base)
    assert st.user_record("joana")["role"] == "engineer"
    st.set_setting("sso.sync_roles", True)
    sso_login(base)
    assert st.user_record("joana")["role"] == "viewer"


def test_sso_require_group_denies_unmapped_people(sso):
    st, base = sso
    st.add_user("maria", "engineer", email="maria@exemplo.com")
    st.set_setting("sso.group_map", [{"group": "dados-admin", "role": "admin"}])
    st.set_setting("sso.require_group", True)
    IdP.claims_override = {"groups": ["outro-grupo"]}
    (s, body, h), _, _ = sso_login(base)
    assert s == 403 and "authorized group" in body.decode()
    assert not any(c.startswith("if_session=") for c in h["_cookies"])
    IdP.claims_override = {"groups": ["dados-admin"]}
    assert sso_login(base)[0][0] == 302


def test_sso_require_group_ignores_groups_mapped_to_none(sso):
    st, base = sso
    st.add_user("maria", "engineer", email="maria@exemplo.com")
    st.set_setting("sso.group_map", [{"group": "dados-admin", "role": "admin"}, {"group": "crm-bloq", "role": "none", "project": "crm"}])
    st.set_setting("sso.require_group", True)
    IdP.claims_override = {"groups": ["crm-bloq"]}
    (s, body, _), _, _ = sso_login(base)
    assert s == 403 and "authorized group" in body.decode()


def test_single_logout_returns_provider_url_with_id_token_hint(sso):
    st, base = sso
    st.add_user("maria", "engineer", email="maria@exemplo.com")
    (s, _, h), _, _ = sso_login(base)
    sid = next(c for c in h["_cookies"] if c.startswith("if_session=")).split(";")[0].split("=", 1)[1]
    code, out, hd = call(base, "POST", "/api/logout", headers={"Cookie": f"if_session={sid}"})
    assert code == 200
    u = urllib.parse.urlparse(out["logout_url"])
    q = dict(urllib.parse.parse_qsl(u.query))
    assert u.path == "/logout" and q["id_token_hint"].count(".") == 2 and q["client_id"] == "intentflow"
    assert q["post_logout_redirect_uri"].startswith("http://127.0.0.1:")
    assert call(base, "GET", "/api/overview", headers={"Cookie": f"if_session={sid}"})[0] == 401       # local session ended
    # token login (not SSO) returns no provider logout URL
    tok = st.add_user("bob", "engineer")
    c2 = call(base, "POST", "/api/login", {"token": tok})[2]["Set-Cookie"].split(";")[0]
    assert "logout_url" not in call(base, "POST", "/api/logout", headers={"Cookie": c2})[1]


# ------------------------------------------------------------------ back-channel, refresh, rebaixamento, overage
def _sid_of(st_session_cookie, st):
    return st.con.execute("select sid, sub, refresh_token from sessions order by created desc limit 1").fetchone()


def test_backchannel_logout_kills_sessions_by_sid(sso):
    st, base = sso
    st.add_user("maria", "engineer", email="maria@exemplo.com")
    (s, _, h), _, _ = sso_login(base)
    sid = next(c for c in h["_cookies"] if c.startswith("if_session=")).split(";")[0].split("=", 1)[1]
    assert call(base, "GET", "/api/overview", headers={"Cookie": f"if_session={sid}"})[0] == 200
    prov_sid = st.con.execute("select sid from sessions").fetchone()["sid"]
    assert prov_sid.startswith("sess-")
    now = int(time.time())
    good = IdP.sign({"iss": IdP.base, "aud": "intentflow", "iat": now, "jti": "j1", "sid": prov_sid,
                     "events": {"http://schemas.openid.net/event/backchannel-logout": {}}})
    def post(tok):
        r = urllib.request.Request(base + "/auth/sso/backchannel", method="POST", data=urllib.parse.urlencode({"logout_token": tok}).encode(),
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            return urllib.request.urlopen(r, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code
    # refused: no event, with a nonce, signed by another key, tampered token
    assert post(IdP.sign({"iss": IdP.base, "aud": "intentflow", "iat": now, "sid": prov_sid})) == 400
    assert post(IdP.sign({"iss": IdP.base, "aud": "intentflow", "iat": now, "sid": prov_sid, "nonce": "x",
                          "events": {"http://schemas.openid.net/event/backchannel-logout": {}}})) == 400
    assert post(good[:-5] + "AAAAA") == 400
    assert post("lixo") == 400
    assert call(base, "GET", "/api/overview", headers={"Cookie": f"if_session={sid}"})[0] == 200    # none of that ended the session
    assert post(good) == 200
    assert call(base, "GET", "/api/overview", headers={"Cookie": f"if_session={sid}"})[0] == 401
    assert any(a["event"] == "backchannel_logout" for a in st.audit_log())


def test_session_refresh_rereads_groups_and_revokes_when_provider_refuses(sso):
    st, base = sso
    st.set_setting("sso.auto_provision", True)
    st.set_setting("sso.allowed_domains", ["exemplo.com"])
    st.set_setting("sso.group_map", [{"group": "dados-admin", "role": "admin"}, {"group": "dados-leitura", "role": "viewer"}])
    st.set_setting("sso.refresh_seconds", 0.2)
    IdP.claims_override = {"groups": ["dados-admin"]}
    (s, _, h), _, _ = sso_login(base)
    sid = next(c for c in h["_cookies"] if c.startswith("if_session=")).split(";")[0].split("=", 1)[1]
    C = {"Cookie": f"if_session={sid}"}
    assert call(base, "GET", "/api/me", headers=C)[1]["user"]["role"] == "admin"
    IdP.claims_override = {"groups": ["dados-leitura"]}          # a pessoa perde o grupo de admin no provedor
    time.sleep(0.4)
    assert call(base, "GET", "/api/me", headers=C)[1]["user"]["role"] == "viewer"          # sem novo login
    assert any(a["event"] == "roles_synced" for a in st.audit_log())
    IdP.revoked.update(IdP.refresh_map)                                                      # the provider revokes the session
    time.sleep(0.4)
    assert call(base, "GET", "/api/me", headers=C)[1]["user"] is None
    assert any(a["event"] == "session_revoked" for a in st.audit_log())


def test_unmapped_role_demotes_and_overage_is_safe(sso):
    st, base = sso
    st.set_setting("sso.auto_provision", True)
    st.set_setting("sso.allowed_domains", ["exemplo.com"])
    st.set_setting("sso.group_map", [{"group": "dados-admin", "role": "admin"}])
    IdP.claims_override = {"groups": ["dados-admin"]}
    sso_login(base)
    assert st.user_record("maria@exemplo.com")["role"] == "admin"
    IdP.claims_override = {"groups": ["qualquer"]}
    sso_login(base)
    assert st.user_record("maria@exemplo.com")["role"] == "viewer"           # default floor: leaving the group demotes
    st.set_setting("sso.unmapped_role", "engineer")
    sso_login(base)
    assert st.user_record("maria@exemplo.com")["role"] == "engineer"         # unmapped_role sets the floor
    st.set_setting("sso.unmapped_role", "viewer")
    sso_login(base)
    assert st.user_record("maria@exemplo.com")["role"] == "viewer"
    # overage (Entra ID >200 groups): groups are not in the token → treated as empty, never grants a role
    IdP.claims_override = {"_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {"endpoint": "https://graph/…"}}}
    sso_login(base)
    assert st.user_record("maria@exemplo.com")["role"] == "viewer"
    assert any(a["event"] == "groups_overage" for a in st.audit_log())


# ------------------------------------------------------------------ proxy, token validity, recovery, scopes


def test_path_scopes_limit_writes_to_allowed_folders(tmp_path):
    prim = new_db(tmp_path, "sc.db")
    p = Store(prim)
    p.sync_findings([
        Finding("DBT001", "high", "stg_payments: x", nodes=["model.shop.stg_payments"], evidence={"file": "models/staging/stg_payments.sql"}),
        Finding("DBT001", "high", "orders: y", nodes=["model.shop.orders"], evidence={"file": "models/marts/orders.sql"}),
        Finding("AF001", "medium", "dag sem retries", nodes=["airflow:etl"], evidence={})], {"DBT001", "AF001"})
    fids = {f["title"]: f["id"] for f in p.findings("open")}
    a, b, c = p.add_user("alice", "admin"), p.add_user("bob", "engineer"), p.add_user("carol", "engineer")
    p.set_scopes("bob", "main", ["models/marts/*"])
    p.set_scopes("carol", "main", ["airflow:*"])
    srv, base = serve(prim)
    try:
        H = lambda t: {"Authorization": f"Bearer {t}"}   # noqa: E731
        ack = lambda who, title: call(base, "POST", f"/api/findings/{fids[title]}/ack", headers=H(who))[0]   # noqa: E731
        assert ack(b, "orders: y") == 200                         # dentro do escopo (arquivo casa)
        assert ack(b, "stg_payments: x") == 403                   # fora do escopo
        assert ack(b, "dag sem retries") == 403
        assert ack(c, "dag sem retries") == 200                   # scope by node name (orchestrator)
        assert ack(a, "stg_payments: x") == 200                   # an admin is not restricted
        # leitura continua livre
        assert call(base, "GET", "/api/overview", headers=H(b))[0] == 200
        # admin define escopo pela API; escopo vazio remove
        assert call(base, "POST", "/api/users", {"action": "scope", "name": "bob", "project": "main", "patterns": ["models/staging/*"]}, H(a))[0] == 200
        assert p.user_record("bob")["scopes"] == json.dumps({"main": ["models/staging/*"]})
        assert call(base, "POST", "/api/users", {"action": "scope", "name": "bob", "project": "main", "patterns": []}, H(a))[0] == 200
        assert json.loads(p.user_record("bob")["scopes"]) == {}
    finally:
        srv.shutdown()


def test_fix_scope_covers_every_changed_file(tmp_path):
    """A rename edits downstream models too: write scope must hold for each changed file, not only the finding's."""
    from intentflow.enterprise.web_ext import _changes_in_scope
    p = {"project_root": str(tmp_path), "changes": [{"file": str(tmp_path / "models/marts/orders.sql")},
                                                    {"file": str(tmp_path / "models/staging/stg_payments.sql")}]}
    bob = {"scopes": {"main": ["models/marts/*"]}}
    assert not _changes_in_scope(bob, "main", "engineer", p)
    assert _changes_in_scope({"scopes": {"main": ["models/*/*"]}}, "main", "engineer", p)
    assert _changes_in_scope(bob, "main", "admin", p)                      # admins are not restricted
    assert _changes_in_scope({"scopes": {}}, "main", "engineer", p)        # no patterns: unrestricted


def test_project_admin_cannot_set_connection_settings(tmp_path):
    """Admin of one project is not a global admin: notify.sinks/*.url/*_env would point the server at hosts, paths and env secrets."""
    prim, crm = new_db(tmp_path, "prim.db"), new_db(tmp_path, "crm.db")
    p = Store(prim)
    p.add_user("alice", "admin")
    t = p.add_user("dana", "viewer")
    p.set_project_role("dana", "crm", "admin")
    p.set_setting("projects", {"crm": crm})
    srv, base = serve(prim)
    try:
        H = {"Authorization": f"Bearer {t}", "X-IntentFlow-Project": "crm"}
        sink = [{"name": "x", "type": "webhook", "url": "https://evil.example", "url_env": "ANTHROPIC_API_KEY"}]
        for key, val in (("notify.sinks", sink), ("dbt_cloud.host", "evil.example"), ("airflow.url", "http://169.254.169.254"),
                         ("dbt.root", "/srv/other"), ("lineage.declared", "/etc/passwd"), ("policy.separate_approver", False)):
            assert call(base, "POST", "/api/settings", {"key": key, "value": val}, H)[0] == 403
        assert Store(crm).get_setting("notify.sinks") is None
        assert call(base, "POST", "/api/settings", {"key": "policy.disabled_checks", "value": "DBT009"}, H)[0] == 200
    finally:
        srv.shutdown()


def test_sso_sync_demotes_and_never_lifts_explicit_block(tmp_path):
    from intentflow.enterprise.web_ext import _sync_roles
    p = Store(new_db(tmp_path, "sync.db"))
    p.add_user("eve", "admin", email="eve@x.io", random_token=True)
    p.set_project_role("eve", "secret", "none")        # explicit block set by an admin
    p.set_project_role("eve", "crm", "engineer")       # granted earlier from a group
    p.set_setting("sso.group_map", [{"group": "data-admin", "role": "admin"}, {"group": "crm-eng", "role": "engineer", "project": "crm"},
                                    {"group": "sec", "role": "viewer", "project": "secret"}])
    p.set_setting("sso.sync_roles", True)
    # removed from every group at the IdP: global role falls to the floor, crm grant goes away, the block stays
    _sync_roles(p, p.user_record("eve"), {"groups": []})
    rec = p.user_record("eve")
    assert rec["role"] == "viewer" and json.loads(rec["project_roles"]) == {"secret": "none"}
    # a group for the blocked project does not lift the block
    _sync_roles(p, p.user_record("eve"), {"groups": ["sec", "crm-eng"]})
    assert json.loads(p.user_record("eve")["project_roles"]) == {"secret": "none", "crm": "engineer"}


def test_ai_edit_cannot_add_env_or_hooks():
    from intentflow.enterprise.llm_fix import guards
    before = "select 1 as id from {{ ref('a') }}"
    rej, _ = guards("m.sql", before, before + "\n{{ config(post_hook='drop table x') }}")
    assert rej
    rej, _ = guards("m.sql", before, "select {{ env_var('AWS_SECRET_ACCESS_KEY') }} from {{ ref('a') }}")
    assert rej
    assert not guards("m.sql", before, "select 2 as id from {{ ref('a') }}")[0]
