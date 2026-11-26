"""SSO against a REAL Keycloak (local container). Skipped without INTENTFLOW_KEYCLOAK_URL.
Test realm in docs/DEPLOYMENT.md (client `intentflow`, redirect http://127.0.0.1:8790/auth/sso/callback)."""
import html, http.cookiejar, json, os, re, threading, urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer

import pytest

from intentflow.store import Store
from intentflow.web import make_handler

KC = os.environ.get("INTENTFLOW_KEYCLOAK_URL")
pytestmark = pytest.mark.skipif(not KC, reason="local Keycloak not configured")
APP = "http://127.0.0.1:8790"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch(url, jar=None, method="GET", data=None, headers=None):
    ops = [NoRedirect]
    if jar is not None:
        ops.append(urllib.request.HTTPCookieProcessor(jar))
    op = urllib.request.build_opener(*ops)
    r = urllib.request.Request(url, method=method, headers=headers or {}, data=urllib.parse.urlencode(data).encode() if data else None)
    def relax():     # browsers accept Secure cookies on 127.0.0.1; Python's cookiejar would not send them over http
        for c in (jar or []):
            c.secure = False
    try:
        relax()
        resp = op.open(r, timeout=30)
        relax()
        return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as e:
        relax()
        return e.code, e.read().decode("utf-8", "replace"), e.headers


def browser_login(username, password, jar=None):
    """Simulates the browser: /auth/sso/login → Keycloak form → callback. Returns (status, body, headers) of the callback."""
    s, _, h = fetch(APP + "/auth/sso/login")
    assert s == 302, s
    oc = h["Set-Cookie"].split(";")[0]
    jar = jar if jar is not None else http.cookiejar.CookieJar()
    s, page, h = fetch(h["Location"], jar)
    assert s == 200 and "<form" in page, (s, page[:200])
    action = html.unescape(re.search(r'<form[^>]*action="([^"]+)"', page).group(1))
    s, body, h = fetch(action, jar, "POST", {"username": username, "password": password, "credentialId": ""},
                       {"Content-Type": "application/x-www-form-urlencoded"})
    if s == 200:                                      # wrong password: Keycloak shows the form again
        return "keycloak-denied", body, h
    for _ in range(6):
        loc = h["Location"]
        if loc.startswith(APP + "/auth/sso/callback"):
            return fetch(loc, None, headers={"Cookie": oc})
        s, body, h = fetch(loc, jar)
    raise AssertionError("não chegou ao callback")


def session_of(headers):
    for c in headers.get_all("Set-Cookie") or []:
        if c.startswith("if_session="):
            return c.split(";")[0].split("=", 1)[1]
    return None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("IF_KC_SECRET", "kc-secret-test")
    db = str(tmp_path / "kc.db")
    st = Store(db)
    st.add_user("dave", "admin")
    st.add_user("maria", "engineer", email="maria@exemplo.com")
    for k, v in {"issuer": KC.rstrip("/") + "/realms/dados", "client_id": "intentflow", "client_secret_env": "IF_KC_SECRET"}.items():
        st.set_setting("sso." + k, v)
    srv = ThreadingHTTPServer(("127.0.0.1", 8790), make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield st
    srv.shutdown()
    srv.server_close()


def me(session):
    s, body, _ = fetch(APP + "/api/me", None, headers={"Cookie": f"if_session={session}"})
    return json.loads(body)


def test_real_keycloak_login_maps_to_existing_user(app):
    s, body, h = browser_login("maria", "senha-teste-1")
    assert s == 302 and h["Location"] == "/", (s, body[:200])
    sid = session_of(h)
    assert sid and sid.startswith("ifs_")
    u = me(sid)["user"]
    assert u["name"] == "maria" and u["role"] == "engineer"
    assert any(a["event"] == "login" and "sso" in a["detail"] for a in app.audit_log())


def test_real_keycloak_wrong_password_is_denied_by_the_idp(app):
    s, body, _ = browser_login("maria", "senha-errada")
    assert s == "keycloak-denied"


def test_real_keycloak_unverified_email_is_refused(app):
    s, body, h = browser_login("joao", "senha-teste-2")
    assert s == 403 and "missing or not verified" in body, (s, body[:200])
    assert session_of(h) is None


def test_real_keycloak_unknown_user_denied_then_provisioned_by_domain(app):
    s, body, _ = browser_login("ana", "senha-teste-3")
    assert s == 403 and "has no access" in body                          # desconhecido: nega
    app.set_setting("sso.auto_provision", True)
    app.set_setting("sso.allowed_domains", ["exemplo.com"])
    assert browser_login("ana", "senha-teste-3")[0] == 403                # domain not in the list
    app.set_setting("sso.allowed_domains", ["outra.com"])
    app.set_setting("sso.default_role", "viewer")
    s, body, h = browser_login("ana", "senha-teste-3")
    assert s == 302
    assert me(session_of(h))["user"] == {"name": "ana@outra.com", "role": "viewer", "mfa": False, "via": "sso"}
    assert app.user_record("ana@outra.com")["email"] == "ana@outra.com"


def test_real_keycloak_disabled_local_user_cannot_use_sso(app):
    app.set_user("maria", disabled=True)
    s, body, h = browser_login("maria", "senha-teste-1")
    assert s == 403 and session_of(h) is None


def test_real_keycloak_groups_map_to_roles_and_projects(app):
    app.set_setting("sso.group_map", [{"group": "dados-admin", "role": "admin"}, {"group": "crm-eng", "role": "engineer", "project": "crm"}])
    # maria was added by hand as engineer: with sync_roles on, the dados-admin group promotes her
    s, _, h = browser_login("maria", "senha-teste-1")
    assert s == 302 and app.user_record("maria")["role"] == "engineer"          # sem sync_roles: cadastro manual vence
    app.set_setting("sso.sync_roles", True)
    s, _, h = browser_login("maria", "senha-teste-1")
    assert s == 302 and app.user_record("maria")["role"] == "admin"
    assert me(session_of(h))["user"]["role"] == "admin"
    # ana is provisioned with the project role from the crm-eng group
    app.set_setting("sso.auto_provision", True)
    app.set_setting("sso.allowed_domains", ["outra.com"])
    s, _, h = browser_login("ana", "senha-teste-3")
    assert s == 302
    u = app.user_record("ana@outra.com")
    assert u["role"] == "viewer" and json.loads(u["project_roles"]) == {"crm": "engineer"}


def test_real_keycloak_require_group_denies_people_without_a_mapped_group(app):
    app.set_setting("sso.group_map", [{"group": "dados-admin", "role": "admin"}])
    app.set_setting("sso.require_group", True)
    app.add_user("luiz", "engineer", email="luiz@exemplo.com")
    s, body, h = browser_login("luiz", "senha-teste-4")
    assert s == 403 and "not in any authorized group" in body and session_of(h) is None
    assert browser_login("maria", "senha-teste-1")[0] == 302                    # maria is in dados-admin


def test_real_keycloak_single_logout_ends_the_provider_session_too(app):
    jar = http.cookiejar.CookieJar()
    s, _, h = browser_login("maria", "senha-teste-1", jar)
    assert s == 302
    sid = session_of(h)
    # the provider session is active: a new /auth/sso/login returns straight to the app (silent login), no password asked
    s2, _, h2 = fetch(APP + "/auth/sso/login")
    oc = h2["Set-Cookie"].split(";")[0]
    s3, _, h3 = fetch(h2["Location"], jar)
    assert s3 == 302 and h3["Location"].startswith(APP + "/auth/sso/callback")
    # logout no IntentFlow devolve a URL de logout do Keycloak
    st, body, hd = fetch(APP + "/api/logout", None, "POST", {}, {"Cookie": f"if_session={sid}", "X-IntentFlow": "1"})
    out = json.loads(body)
    assert st == 200 and "/protocol/openid-connect/logout" in out["logout_url"] and "id_token_hint=" in out["logout_url"]
    assert fetch(APP + "/api/me", None, headers={"Cookie": f"if_session={sid}"})[1].count('"user": null') == 1     # local session ended
    # the browser follows the URL: Keycloak ends its session and returns to the app
    s4, _, h4 = fetch(out["logout_url"], jar)
    assert s4 == 302 and h4["Location"].startswith(APP + "/")
    # now login asks for the password again (the provider session ended)
    _, _, h5 = fetch(APP + "/auth/sso/login")
    s6, page, _ = fetch(h5["Location"], jar)
    assert s6 == 200 and "<form" in page and 'name="password"' in page
