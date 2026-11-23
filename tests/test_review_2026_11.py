"""Regressions from the 2026-11 review: CLI exit codes, OpenLineage receiver hardening, prune coverage, watch isolation."""
import json
import threading
import time
import urllib.error
import urllib.request

from intentflow import cli, openlineage, runner
from intentflow.store import Store


def test_cli_missing_target_exits_2(tmp_path, capsys):
    assert cli.main(["--store", str(tmp_path / "s.db"), "check", "--target", str(tmp_path / "nope")]) == 2
    assert "manifest.json" in capsys.readouterr().err


def _post(port, body, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/lineage", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_openlineage_token_and_malformed(tmp_path):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), openlineage.make_handler(str(tmp_path / "s.db"), token="t0k"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        ev = {"eventType": "START", "eventTime": "2026-11-01T00:00:00Z", "run": {"runId": "r1"}, "job": {"name": "j"}, "inputs": ["bad"]}
        assert _post(port, ev) == 401
        assert _post(port, ev, {"Authorization": "Bearer t0k"}) == 400          # malformed input dataset: answered, not dropped
        ev["inputs"] = [{"namespace": "n", "name": "a"}]
        assert _post(port, ev, {"Authorization": "Bearer t0k"}) == 200
    finally:
        srv.shutdown()


def test_prune_covers_growth_tables(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.upsert_ol_job("j", ["a"], ["b"], 1.0)
    st.add_ol_edges("j", [("a", "x", "b", "y")])
    st.set_setting("state.notified.slack.abc", 1.0)
    st.con.execute("update settings set updated=1")
    st.con.commit()
    res = st.prune(1, now=time.time())
    assert res["ol_jobs"] == 1 and res["ol_edges"] == 1 and res["notified"] == 1
    assert not st.ol_jobs() and st.get_setting("state.notified.slack.abc") is None


def test_watch_collection_failure_does_not_skip_checks(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("dbt.target", "x")
    calls = []
    monkeypatch.setattr(runner, "collect_runs", lambda s: (_ for _ in ()).throw(ValueError("bad json")))
    monkeypatch.setattr(runner, "collect_samples", lambda s: 0)
    monkeypatch.setattr(runner, "run_check", lambda s: calls.append(1) or {"project": None})
    monkeypatch.setattr("intentflow.notify.send", lambda *a, **k: [])
    runner.watch(st, 0, iterations=1, sleep=lambda _: None)
    assert calls == [1]
    assert any(e["event"] == "watch_error" for e in st.audit_log())


def _ui(tmp_path, **kw):
    from intentflow import web
    srv = web.BoundedThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(str(tmp_path / "ui.db"), **kw))
    srv.scheme = "http"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_legacy_cookie_is_not_the_token_and_page_has_csp(tmp_path):
    srv = _ui(tmp_path, token="legacy-secret")
    port = srv.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/login", data=b'{"token": "legacy-secret"}', method="POST",
                                     headers={"X-IntentFlow": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            cookie = r.headers["Set-Cookie"].split(";")[0]
        assert "legacy-secret" not in cookie and cookie.startswith("if_session=ifl_")
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", headers={"Cookie": cookie}), timeout=5) as r:
            assert r.status == 200
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            csp, page = r.headers["Content-Security-Policy"], r.read().decode()
        nonce = csp.split("'nonce-")[1].split("'")[0]
        assert f'<script nonce="{nonce}">' in page
    finally:
        srv.shutdown()


def test_claim_setting_is_exclusive(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    assert st.claim_setting("state.running", "check") and not st.claim_setting("state.running", "runs")


def test_notify_rejects_non_http_urls():
    import pytest
    from intentflow import notify
    with pytest.raises(ValueError):
        notify.post_json("file:///etc/passwd", {})


def test_backup_copies_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)
    st = Store(str(tmp_path / "s.db"))
    st.add_user("ana", "admin")
    st.mfa_begin("ana")                      # writes an encrypted secret, creating s.db.key
    st.backup(str(tmp_path / "b.db"))
    assert (tmp_path / "b.db.key").read_bytes() == (tmp_path / "s.db.key").read_bytes()
    assert Store(str(tmp_path / "b.db")).box.dec(Store(str(tmp_path / "b.db")).user_record("ana")["totp_secret"])


def test_openlineage_outside_loopback_needs_tls(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("INTENTFLOW_OL_TOKEN", "x")
    with pytest.raises(SystemExit):
        openlineage.serve(str(tmp_path / "s.db"), 0, host="0.0.0.0")


def test_prune_keeps_live_lineage_cache(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.set("live", {"a": 1})
    st.set("stale", {"a": 2})
    st.flush()
    assert st.prune(30, keep_lineage={"live"})["lineage_cache"] == 1
    assert st.get("live") and st.get("stale") is None
    assert "lineage_cache" not in st.prune(30)


def test_tls_handshake_does_not_block_accept(tmp_path):
    """A client that connects and never sends a ClientHello must not stall other TLS clients."""
    import socket, ssl, subprocess
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    from http.server import ThreadingHTTPServer
    from intentflow.http_utils import tls_context
    srv = ThreadingHTTPServer(("127.0.0.1", 0), openlineage.make_handler(str(tmp_path / "s.db")))
    srv.socket = tls_context(str(cert), str(key)).wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    idle = socket.create_connection(("127.0.0.1", port))          # silent client
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(f"https://127.0.0.1:{port}/api/v1/lineage", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=3, context=ctx)
        except urllib.error.HTTPError as e:
            assert e.code == 400                                     # answered despite the idle connection
    finally:
        idle.close()
        srv.shutdown()
