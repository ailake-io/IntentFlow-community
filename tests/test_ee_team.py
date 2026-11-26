import time
"""Team mode: users, roles, login, separation of duties, assignment/comments, TLS and safe exposure."""
from conftest import new_db
import json, shutil, threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

import pytest

from intentflow import runner
from intentflow.store import Store
from intentflow.web import make_handler


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


@pytest.mark.usefixtures("sandbox_image")
def test_creator_cannot_approve_own_fix_but_a_peer_can(team):
    api, tok, st, work = team
    fid = next(f["id"] for f in st.findings("open") if f["fix"])
    code, body, _ = api.call("POST", f"/api/findings/{fid}/fix", token=tok["bob"])
    assert code == 200
    pid = body["proposal"]
    code, err, _ = api.call("POST", f"/api/proposals/{pid}/approve", token=tok["bob"])
    assert code == 400 and "cannot approve it" in err["error"]
    assert api.call("POST", f"/api/proposals/{pid}/approve", token=tok["alice"])[1]["status"] == "approved"
    assert api.call("POST", f"/api/proposals/{pid}/validate?mode=build", token=tok["bob"])[1]["validation"]["ok"]
    assert api.call("POST", f"/api/proposals/{pid}/apply", token=tok["carol"])[0] == 403   # a viewer cannot apply
    assert api.call("POST", f"/api/proposals/{pid}/apply", token=tok["bob"])[1]["status"] == "applied"
    approval = st.proposal(pid)["approval"]
    assert approval["user"] == "alice" and st.proposal(pid)["created_by"] == "bob"        # trail of who did what


def test_llm_endpoints_admin_only_key_encrypted_never_returned_and_fix_ai(team, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _S
    api, tokens, st, work = team
    from conftest import BACKEND
    if BACKEND == "postgres":        # no `.key` file on PostgreSQL: the key must come from the variable
        monkeypatch.setenv("INTENTFLOW_SECRET_KEY", "teste-chave-longa-0123456789abcdef")
    else:
        monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({"data": [{"id": "meu-modelo"}]}).encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps({"explanation": "x", "confidence": 0.5, "edits": []})}}],
                                         "usage": {}}).encode())

        def log_message(self, *a):
            pass
    fake = _S(("127.0.0.1", 0), H)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{fake.server_address[1]}"
    # only an admin changes it; an engineer cannot
    c, body, _ = api.call("POST", "/api/llm", {"provider": "local", "model": "m", "base_url": base}, tokens["bob"])
    assert c == 403
    c, body, _ = api.call("POST", "/api/llm", {"provider": "local", "model": "meu-modelo", "base_url": base, "api_key": "sk-NUNCA-DEVOLVER",
                                                "jev": {"provider": "openrouter", "enabled": True, "api_key": "or-NUNCA"}}, tokens["alice"])
    assert c == 200 and body["llm"]["configured"] and body["llm"]["key_source"] == "stored" and body["jev"]["key_source"] == "stored"
    c, got, _ = api.call("GET", "/api/llm", None, tokens["carol"])                 # a viewer sees the state, without the secret
    assert c == 200 and "NUNCA" not in json.dumps(got) and got["llm"]["provider"] == "local"
    raw = [r[0] for r in st.con.execute("select value from settings where key like 'secret.%'").fetchall()]
    assert raw and all("NUNCA" not in r and "enc:v1:" in r for r in raw)
    assert all("NUNCA" not in json.dumps(a) for a in st.audit_log())
    c, body, _ = api.call("POST", "/api/llm/models", {}, tokens["alice"])
    assert c == 200 and body["models"] == ["meu-modelo"]
    c, body, _ = api.call("POST", "/api/llm/test", {}, tokens["alice"])
    assert c == 200 and body["ok"] and body["model"] == "meu-modelo"
    # asking AI for a fix: the model returns zero edits → clear error, nothing created
    fid = next(f["id"] for f in st.findings("open") if f.get("nodes"))
    c, body, _ = api.call("POST", f"/api/findings/{fid}/fix-ai", {}, tokens["bob"])
    assert c == 200 and body["state"] == "running"                                    # asynchronous: the UI follows through /ai-status
    for _ in range(100):
        c, body, _ = api.call("POST", f"/api/findings/{fid}/ai-status", {}, tokens["bob"])
        if body.get("state") != "running":
            break
        time.sleep(0.1)
    assert c == 200 and body["state"] == "error" and "edit" in body["error"] and st.proposals() == []
    c, body, _ = api.call("POST", f"/api/findings/{fid}/fix-ai", {}, tokens["carol"])
    assert c == 403                                                                  # a viewer cannot ask for a fix
    # remover a chave guardada
    c, body, _ = api.call("POST", "/api/llm", {"clear_key": True}, tokens["alice"])
    assert c == 200 and body["llm"]["key_source"] == "none"
    fake.shutdown()
