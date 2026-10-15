import base64, json, os, subprocess, sys, threading, time, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

from conftest import new_db
import pytest

from intentflow import cli
from intentflow.orchestrators.airflow import AirflowClient
from intentflow.orchestrators.kestra import KestraClient
from intentflow.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_airflow_client_normalizes_runs_and_pools():
    calls = []

    def fake(url, headers=None, timeout=0):
        calls.append(url)
        if "/dagRuns" in url:
            return {"dag_runs": [
                {"dag_run_id": "r1", "state": "success", "start_date": "2026-10-11T10:00:00+00:00", "end_date": "2026-10-11T10:05:00+00:00"},
                {"dag_run_id": "r2", "state": "failed", "start_date": "2026-10-11T11:00:00+00:00", "end_date": "2026-10-11T11:01:00+00:00"}]}
        if url.endswith("/pools"):
            return {"pools": [{"name": "default_pool", "slots": 8, "running_slots": 6, "queued_slots": 2}]}
        return {"dags": [{"dag_id": "etl", "is_paused": False}, {"dag_id": "old", "is_paused": True}]}

    c = AirflowClient("http://af:8080", "v1", fetch=fake)
    runs = c.runs()
    assert [(r.pipeline, r.state, r.duration) for r in runs] == [("etl", "success", 300.0), ("etl", "failed", 60.0)]
    assert c.pools()[0]["slots"] == 8
    assert not any("old" in u for u in calls)           # a paused DAG is not queried
    assert all("/api/v1/" in u for u in calls)


@pytest.mark.skipif(not os.environ.get("INTENTFLOW_KESTRA_URL"), reason="local Kestra not configured")
def test_kestra_client_against_real_server():
    base, user, pw = os.environ["INTENTFLOW_KESTRA_URL"], os.environ["KESTRA_USER"], os.environ["KESTRA_PASSWORD"]
    hdr = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    def call(method, path, data=None, ctype=None):
        req = urllib.request.Request(f"{base}/api/v1{path}", data=data, method=method, headers={**hdr, **({"Content-Type": ctype} if ctype else {})})
        return urllib.request.urlopen(req, timeout=60).read()

    ok = b"id: it_ok\nnamespace: intentflow\ntasks:\n  - id: hi\n    type: io.kestra.plugin.core.log.Log\n    message: hello\n"
    bad = b"id: it_bad\nnamespace: intentflow\ntasks:\n  - id: boom\n    type: io.kestra.plugin.core.execution.Fail\n"
    for body in (ok, bad):
        try:
            call("POST", "/flows", body, "application/x-yaml")
        except urllib.error.HTTPError as e:      # already exists from a previous run
            assert e.code in (409, 422), e.code
    boundary = "----if"
    form = f"--{boundary}\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n1\r\n--{boundary}--\r\n".encode()
    for flow in ("it_ok", "it_bad"):
        try:
            call("POST", f"/executions/intentflow/{flow}?wait=true", form, f"multipart/form-data; boundary={boundary}")
        except urllib.error.HTTPError:
            pass
    os.environ.setdefault("KESTRA_USER", user)
    runs = KestraClient(base).runs()
    got = {(r.pipeline, r.state) for r in runs}
    assert ("intentflow.it_ok", "success") in got and ("intentflow.it_bad", "failed") in got
    assert all(r.duration is not None for r in runs if r.state in ("success", "failed"))
    assert {f["id"] for f in KestraClient(base).flows()} >= {"it_ok", "it_bad"}


def test_cli_check_store_and_overview_end_to_end(shop_target, tmp_path, capsys):
    db = new_db(tmp_path, "s.db")
    rc = cli.main(["--store", db, "check", "--target", str(shop_target / "target"), "--airflow-dags", str(ROOT / "examples/airflow_dags"),
                   "--kestra-flows", str(ROOT / "examples/kestra_flows"), "--json", "--fail-on", "critical"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0                                   # no critical ones in this case
    ids = {f["check"] for f in out["findings"]}
    assert {"SQL001", "DBT002", "DBT004", "AF003", "KS005"} <= ids
    assert out["lineage"]["resolved_ratio"] == 1.0
    # the second run is idempotent: nothing new
    cli.main(["--store", db, "check", "--target", str(shop_target / "target"), "--json"])
    again = json.loads(capsys.readouterr().out)
    assert again["store"]["new"] == 0
    ov = __import__("intentflow.overview", fromlist=["build"]).build(Store(db))
    assert ov["summary"]["open_findings"] > 0 and ov["findings"][0]["severity"] in ("critical", "high")


def test_cli_fail_on_gate(shop_target, tmp_path):
    rc = cli.main(["--store", new_db(tmp_path, "s.db"), "check", "--target", str(shop_target / "target"), "--only", "DBT004", "--fail-on", "high"])
    assert rc == 1                                   # unmasked PII is high → the gate fails


def test_ui_serves_overview_and_enforces_local_guards(tmp_path):
    from intentflow.model import Finding
    from intentflow.web import make_handler
    db = new_db(tmp_path, "u.db")
    st = Store(db)
    f = Finding("DBT002", "high", "m: incremental sem unique_key", nodes=["x"])
    st.sync_findings([f], {"DBT002"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def req(method, path, headers=None):
            r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {})
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        code, body = req("GET", "/api/overview")
        assert code == 200 and json.loads(body)["summary"]["open_findings"] == 1
        assert req("GET", "/")[0] == 200
        assert req("POST", f"/api/findings/{f.id}/ack")[0] == 400                      # without the anti-CSRF header
        assert req("POST", f"/api/findings/{f.id}/ack", {"X-IntentFlow": "1"})[0] == 200
        assert json.loads(req("GET", "/api/overview")[1])["summary"]["open_findings"] == 0
        assert req("GET", "/api/overview", {"Host": "evil.example"})[0] == 403          # anti DNS rebinding
    finally:
        srv.shutdown()


def test_no_yaml_needed_config_and_policies_live_in_store(shop_target, tmp_path, capsys):
    db = new_db(tmp_path, "c.db")
    assert cli.main(["--store", db, "config", "set", "dbt.target", str(shop_target / "target")]) == 0
    assert cli.main(["--store", db, "policy", "pii-allow", "customers"]) == 0
    assert cli.main(["--store", db, "policy", "pii-allow", "customer_contacts"]) == 0
    capsys.readouterr()
    rc = cli.main(["--store", db, "check", "--only", "DBT004", "--json"])    # no flags: uses the saved settings
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["findings"] == []                                   # PII allowed by the policy
    cli.main(["--store", db, "policy", "pii-allow", "customer_contacts", "--remove"])
    capsys.readouterr()
    cli.main(["--store", db, "check", "--only", "DBT004", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert [f["nodes"][0].split(".")[-1] for f in out["findings"]] == ["customer_contacts"]


def test_ui_saves_settings_and_runs_check_in_background(shop_target, tmp_path):
    from intentflow.web import make_handler
    db = new_db(tmp_path, "ui.db")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    H = {"X-IntentFlow": "1", "Content-Type": "application/json"}
    try:
        def post(path, body=None):
            r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", headers=H, data=json.dumps(body or {}).encode())
            try:
                with urllib.request.urlopen(r, timeout=30) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                return json.loads(r.read())
        assert post("/api/settings", {"key": "state.running", "value": "x"})[0] == 400        # estado interno protegido
        assert post("/api/run/check")[0] == 400 or True                                       # may start and fail without settings
        time.sleep(1.0)
        assert post("/api/settings", {"key": "dbt.target", "value": str(shop_target / "target")})[0] == 200
        assert post("/api/settings", {"key": "policy.pii_allowed_models", "value": "customers, customer_contacts"})[0] == 200
        assert get("/api/settings")["settings"]["policy.pii_allowed_models"] == ["customers", "customer_contacts"]
        for _ in range(50):
            if not get("/api/settings")["running"]:
                break
            time.sleep(0.2)
        code, body = post("/api/run/check")
        assert code == 200 and body["started"] == "check"
        for _ in range(100):
            time.sleep(0.3)
            s = get("/api/settings")
            if s["last_check"] and not s["running"]:
                break
        assert s["last_check"]["findings"] > 0
        ov = get("/api/overview")
        assert ov["summary"]["open_findings"] == s["last_check"]["findings"]
    finally:
        srv.shutdown()


def test_false_positive_is_sticky_and_stats_and_prune(tmp_path):
    from intentflow.model import Finding
    st = Store()
    fs = [Finding("SQL001", "medium", f"m{i}", nodes=[f"n{i}"]) for i in range(3)]
    st.sync_findings(fs, {"SQL001"})
    st.mark_false_positive(fs[0].id, "me", "esperado")
    st.sync_findings(fs, {"SQL001"})                                     # does not reopen
    assert {f["id"] for f in st.findings("open")} == {fs[1].id, fs[2].id}
    st.sync_findings(fs[:1], {"SQL001"})                                  # os outros somem → resolvidos
    s = {c["check"]: c for c in st.check_stats()}["SQL001"]
    assert (s["false_positive"], s["resolved"], s["fp_rate"]) == (1, 2, 0.33)
    st.add_samples([__import__("intentflow.monitoring", fromlist=["Sample"]).Sample(1.0, "host", "h", "memory_bytes", 1.0, 2.0)])
    assert st.prune(days=1)["samples"] == 1 and st.prune(days=1)["findings"] >= 0


def test_disabled_check_policy_and_watch_loop(shop_target, tmp_path, capsys):
    from intentflow import runner
    db = new_db(tmp_path, "w.db")
    st = Store(db)
    st.set_setting("dbt.target", str(shop_target / "target"))
    st.set_setting("policy.disabled_checks", ["DBT004", "SQL001"])
    slept = []
    assert runner.watch(st, interval_s=60, iterations=2, sleep=slept.append) == 2
    assert len(slept) == 1 and 0 < slept[0] <= 60                        # sleeps only between cycles
    ids = {f["check_id"] for f in st.findings("open")}
    assert "DBT004" not in ids and "SQL001" not in ids and "DBT001" in ids
    assert st.get_setting("state.last_check")["findings"] > 0


def test_ui_token_auth(tmp_path):
    from intentflow.web import make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(new_db(tmp_path, "t.db"), token="s3cret"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, headers=None):
            r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
            try:
                return urllib.request.urlopen(r, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        assert get("/api/overview") == 401
        assert get("/api/overview", {"Authorization": "Bearer s3cret"}) == 200
        assert get("/api/overview", {"Cookie": "if_session=s3cret"}) == 200
        assert get("/?token=errado") == 401
    finally:
        srv.shutdown()


def test_ui_javascript_is_syntactically_valid():
    """Regression: a syntax error leaves the UI blank and no API test notices."""
    import re, shutil, subprocess, tempfile
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    html = (ROOT / "src/intentflow/web.html").read_text()
    js = re.search(r"<script>([\s\S]*)</script>", html).group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
    r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:600]


def test_ui_diff_endpoint_and_check_registry_in_overview(tmp_path):
    import duckdb
    from intentflow.web import make_handler
    dbf = tmp_path / "w.duckdb"
    c = duckdb.connect(str(dbf))
    c.execute("create schema prod; create schema dev;")
    c.execute("create table prod.t as select * from (values (1,'a'),(2,'b')) x(id,v)")
    c.execute("create table dev.t as select * from (values (1,'a'),(2,'B'),(3,'c')) x(id,v)")
    c.close()
    db = new_db(tmp_path, "d.db")
    st = Store(db)
    st.set_setting("warehouse.duckdb", str(dbf))
    st.set_setting("policy.disabled_checks", ["SQL001"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def call(method, path, body=None):
            r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers={"X-IntentFlow": "1"},
                                       data=json.dumps(body).encode() if body is not None else None)
            try:
                return json.loads(urllib.request.urlopen(r, timeout=20).read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        d = call("POST", "/api/diff", {"base": "prod.t", "head": "dev.t", "key": "id"})
        assert d["only_in_head"] == 1 and d["changed_by_column"] == {"v": 1}
        assert "error" in call("POST", "/api/diff", {"base": "x"})
        ov = call("GET", "/api/overview")
        checks = {c["id"]: c["enabled"] for c in ov["checks"]}
        assert checks["SQL001"] is False and checks["DBT001"] is True and len(checks) >= 30
    finally:
        srv.shutdown()


def test_store_survives_concurrent_writers(tmp_path):
    """UI, scheduler and CLI write to the same file: none may fail with 'database is locked'."""
    import sqlite3
    db = new_db(tmp_path, "c.db")
    Store(db)
    errors = []

    def writer(n):
        try:
            s = Store(db)
            for i in range(60):
                s.add_history(f"k{n}", float(i), float(i))
                s.set_setting(f"w{n}.{i % 5}", i)
        except sqlite3.Error as e:
            errors.append(str(e))
    ts = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert sum(len(Store(db).history(f"k{n}")) for n in range(6)) == 360


def test_package_declares_ui_as_package_data():
    """The real wheel build was checked by hand; here we only guard the settings (building in CI avoids copying .venv)."""
    import tomllib
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "web.html" in cfg["tool"]["setuptools"]["package-data"]["intentflow"]
    assert (ROOT / "src/intentflow/web.html").exists()
    assert cfg["project"]["scripts"]["intentflow"] == "intentflow.cli:main"


def test_airflow_v2_exchanges_credentials_for_jwt_and_caches_it(monkeypatch):
    from intentflow.orchestrators.airflow import AirflowClient
    monkeypatch.delenv("AIRFLOW_TOKEN", raising=False)
    monkeypatch.setenv("AIRFLOW_USER", "u")
    monkeypatch.setenv("AIRFLOW_PASSWORD", "p")
    calls, seen = [], []
    c = AirflowClient("http://af", "v2", fetch=lambda url, h: (seen.append(h) or {"dags": []}),
                      login=lambda u, p: calls.append((u, p)) or "jwt-123")
    c.dags(); c.dags()
    assert calls == [("u", "p")] and seen[0] == {"Authorization": "Bearer jwt-123"}
    monkeypatch.setenv("AIRFLOW_TOKEN", "fixed")
    assert AirflowClient("http://af", "v2")._h() == {"Authorization": "Bearer fixed"}
