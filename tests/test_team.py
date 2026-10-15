import time
"""Team mode: users, roles, login, separation of duties, assignment/comments, TLS and safe exposure."""
from conftest import new_db
import json, shutil, subprocess, threading, urllib.request, urllib.error, ssl
from http.server import ThreadingHTTPServer

import pytest

from intentflow import runner
from intentflow.store import Store
from intentflow.web import build_server, make_handler


class Api:
    def __init__(self, port, scheme="http", ctx=None):
        self.base, self.ctx = f"{scheme}://127.0.0.1:{port}", ctx

    def call(self, method, path, body=None, token=None, headers=None):
        h = {"X-IntentFlow": "1", **(headers or {})}
        if token:
            h["Authorization"] = f"Bearer {token}"
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=json.dumps(body if body is not None else {}).encode() if method == "POST" else None)
        try:
            with urllib.request.urlopen(r, timeout=300, context=self.ctx) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


@pytest.fixture()
def team(shop_target, tmp_path):
    work = tmp_path / "proj"
    shutil.copytree(shop_target, work, ignore=shutil.ignore_patterns("logs"))
    db = new_db(tmp_path, "team.db")
    st = Store(db)
    st.set_setting("dbt.target", str(work / "target"))
    runner.run_check(st, only=["SQL001", "DBT"])
    tokens = {"alice": st.add_user("alice", "admin"), "bob": st.add_user("bob", "engineer"), "carol": st.add_user("carol", "viewer")}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Api(srv.server_address[1]), tokens, st, work
    srv.shutdown()


def test_local_mode_has_no_login(tmp_path):
    db = new_db(tmp_path, "l.db")
    Store(db)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, me, _ = Api(srv.server_address[1]).call("GET", "/api/me")
        assert code == 200 and me["auth_required"] is False and me["user"]["role"] == "admin"
    finally:
        srv.shutdown()


def test_roles_are_enforced_and_actions_are_attributed(team):
    api, tok, st, _ = team
    assert api.call("GET", "/api/overview")[0] == 401                                  # sem token
    assert api.call("GET", "/api/overview", token="if_errado")[0] == 401
    code, ov, _ = api.call("GET", "/api/overview", token=tok["carol"])
    assert code == 200 and ov["findings"]
    fid = ov["findings"][0]["id"]
    assert api.call("POST", f"/api/findings/{fid}/ack", token=tok["carol"])[0] == 403   # a viewer cannot act
    assert api.call("POST", f"/api/findings/{fid}/ack", token=tok["bob"])[0] == 200     # engineer age
    assert any(a["actor"] == "bob" and a["event"] == "ack" for a in st.audit_log())     # attributed to whoever did it
    assert api.call("POST", "/api/settings", {"key": "dbt.root", "value": "/x"}, token=tok["bob"])[0] == 403
    assert api.call("POST", "/api/settings", {"key": "policy.max_estimated_rows", "value": 5}, token=tok["alice"])[0] == 200
    assert api.call("GET", "/api/users", token=tok["bob"])[0] == 403
    assert [u["name"] for u in api.call("GET", "/api/users", token=tok["alice"])[1]["users"]] == ["alice", "bob", "carol"]
    assert "token_hash" not in json.dumps(api.call("GET", "/api/users", token=tok["alice"])[1])   # hash nunca sai


def test_login_cookie_throttle_and_disabled_user(team):
    api, tok, st, _ = team
    code, body, hdr = api.call("POST", "/api/login", {"token": tok["bob"]})
    assert code == 200 and body["user"] == {"name": "bob", "role": "engineer"}
    assert "HttpOnly" in hdr["Set-Cookie"] and "SameSite=Strict" in hdr["Set-Cookie"]
    code, _, _ = api.call("GET", "/api/me", headers={"Cookie": hdr["Set-Cookie"].split(";")[0]})
    assert code == 200
    st.set_user("bob", disabled=True)
    assert api.call("GET", "/api/overview", token=tok["bob"])[0] == 401               # desativado perde acesso na hora
    codes = [api.call("POST", "/api/login", {"token": "if_x"})[0] for _ in range(12)]
    assert 429 in codes                                                              # limite de tentativas


def test_admin_manages_team_and_cannot_remove_last_admin(team):
    api, tok, st, _ = team
    code, body, _ = api.call("POST", "/api/users", {"action": "add", "name": "dave", "role": "engineer"}, token=tok["alice"])
    assert code == 200 and body["token"].startswith("if_")
    assert api.call("GET", "/api/me", token=body["token"])[1]["user"]["name"] == "dave"
    assert api.call("POST", "/api/users", {"action": "disable", "name": "alice"}, token=tok["alice"])[0] == 400   # last admin
    assert api.call("POST", "/api/users", {"action": "add", "name": "x", "role": "root"}, token=tok["alice"])[0] == 400


def test_assignment_and_notes(team):
    api, tok, st, _ = team
    fid = st.findings("open")[0]["id"]
    assert api.call("POST", f"/api/findings/{fid}/assign", {"assignee": "alice"}, token=tok["bob"])[0] == 200
    assert api.call("POST", f"/api/findings/{fid}/assign", {"assignee": "fantasma"}, token=tok["bob"])[0] == 400
    assert api.call("POST", f"/api/findings/{fid}/note", {"text": "olhando isto"}, token=tok["bob"])[0] == 200
    assert api.call("POST", f"/api/findings/{fid}/note", {"text": ""}, token=tok["bob"])[0] == 400
    assert api.call("POST", f"/api/findings/{fid}/note", {"text": "x"}, token=tok["carol"])[0] == 403
    f = next(x for x in api.call("GET", "/api/overview", token=tok["carol"])[1]["findings"] if x["id"] == fid)
    assert f["assignee"] == "alice" and f["notes"] == 1
    assert api.call("GET", f"/api/notes/{fid}", token=tok["carol"])[1]["notes"][0]["author"] == "bob"


def test_exposure_rules_and_tls(tmp_path):
    db = new_db(tmp_path, "x.db")
    st = Store(db)
    with pytest.raises(SystemExit, match="add users first"):
        build_server(db, 0, "0.0.0.0")                                                  # no users: refused
    token = st.add_user("alice", "admin")
    with pytest.raises(SystemExit, match="HTTPS"):
        build_server(db, 0, "0.0.0.0")                                                  # neither TLS nor proxy: refused
    build_server(db, 0, "127.0.0.1").server_close()
    if not shutil.which("openssl"):
        pytest.skip("openssl unavailable")
    cert, key = str(tmp_path / "c.pem"), str(tmp_path / "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    srv = build_server(db, 0, "127.0.0.1", cert, key)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context(cafile=cert)
        code, me, _ = Api(srv.server_address[1], "https", ctx).call("GET", "/api/me", token=token)
        assert srv.scheme == "https" and code == 200 and me["user"]["name"] == "alice"
    finally:
        srv.shutdown()


def test_many_people_hitting_the_server_at_once(team):
    """Several people with the UI open (polling) + simultaneous actions: no response may fail."""
    api, tok, st, _ = team
    fid = st.findings("open")[0]["id"]
    errors = []

    def person(name, n):
        try:
            for i in range(25):
                code = api.call("GET", "/api/overview", token=tok[name])[0]
                if code != 200:
                    errors.append((name, "overview", code))
                if name != "carol" and i % 5 == 0:
                    c = api.call("POST", f"/api/findings/{fid}/note", {"text": f"{name} {i}"}, token=tok[name])[0]
                    if c != 200:
                        errors.append((name, "note", c))
        except Exception as e:   # dropped connection = the original bug
            errors.append((name, repr(e)[:80]))
    ts = [threading.Thread(target=person, args=(n, 0)) for n in ("alice", "bob", "carol", "bob", "carol", "alice")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert len(st.notes(fid)) >= 8


def test_backup_is_consistent_and_private(team, tmp_path):
    import os
    api, tok, st, _ = team
    dest = new_db(tmp_path, "bk.db", "sqlite")         # SQLite file (works for both source backends)
    assert st.backup(dest) > 0
    assert oct(os.stat(dest).st_mode & 0o777) == "0o600"
    copy = Store(dest)
    assert {u["name"] for u in copy.users()} == {"alice", "bob", "carol"} and copy.findings("open")
    assert copy.user_by_token(tok["alice"])["role"] == "admin"      # the token stays valid in the copy


def test_prometheus_metrics_endpoint_requires_auth_and_exposes_counts(team):
    api, tokens, st, _ = team
    r = urllib.request.Request(api.base + "/metrics", headers={"X-IntentFlow": "1"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 401                                         # no counts without a token
    r = urllib.request.Request(api.base + "/metrics", headers={"Authorization": f"Bearer {tokens['carol']}"})
    with urllib.request.urlopen(r) as resp:
        text, ctype = resp.read().decode(), resp.headers["Content-Type"]
    assert ctype.startswith("text/plain") and "# TYPE intentflow_findings_open gauge" in text
    n = len(st.findings("open"))
    import re
    total = sum(int(float(v)) for v in re.findall(r'^intentflow_findings_open\{severity="[a-z]+"\} (\S+)$', text, re.M))
    assert n > 0 and total == n
    assert "intentflow_check_last_run_timestamp_seconds" in text
