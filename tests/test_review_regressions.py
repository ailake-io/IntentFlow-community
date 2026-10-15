"""Regressions found during the project review; all resources are temporary/local."""
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from intentflow import dbt_loader, lineage_api, openlineage, runner, totp, web
from intentflow.checks import Check, Context, run_checks
from intentflow.model import Finding
from intentflow.store import Store


@pytest.fixture
def local_api(tmp_path):
    path = str(tmp_path / "review.db")
    with Store(path):
        pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(method, route, body=None, token=None, raw=None, headers=None):
        hs = {"X-IntentFlow": "1", **(headers or {})}
        if token:
            hs["Authorization"] = "Bearer " + token
        data = raw if raw is not None else json.dumps(body or {}).encode() if method == "POST" else None
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{route}", data=data, method=method, headers=hs)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read() or b"{}")

    yield path, call
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b'"text"'])
def test_api_rejects_non_object_or_malformed_json(local_api, raw):
    _, call = local_api
    assert call("POST", "/api/login", raw=raw)[0] == 400
    assert call("GET", "/api/me")[0] == 200


@pytest.mark.parametrize("headers, expected", [
    ({"Content-Length": "-1"}, 400),
    ({"Content-Length": "invalid"}, 400),
    ({"Content-Length": str(1024 * 1024 + 1)}, 413),
    ({"Transfer-Encoding": "chunked"}, 400),
])
def test_api_rejects_unsafe_framing_before_read(local_api, headers, expected):
    _, call = local_api
    assert call("POST", "/api/login", raw=b"{}", headers=headers)[0] == expected


def test_settings_do_not_expose_secrets_or_global_configuration(local_api):
    path, call = local_api
    with Store(path) as store:
        token = store.add_user("viewer", "viewer")
        store.set_setting("secret.llm_api_key", "must-not-leak")
        store.set_setting("sso.client_id", "global-client")
        store.set_setting("projects", {"other": "private-location"})
        store.set_setting("dbt.target", "target")
    code, body = call("GET", "/api/settings", token=token)
    assert code == 200
    assert body["settings"] == {"dbt.target": "target"}




def test_request_closes_store_connection(local_api, monkeypatch):
    _, call = local_api
    closed = []
    original = Store.close

    def close(store):
        closed.append(store.path)
        original(store)

    monkeypatch.setattr(Store, "close", close)
    assert call("GET", "/api/me")[0] == 200
    # Handler cleanup happens immediately after the body is written.
    for _ in range(100):
        if closed:
            break
        time.sleep(0.001)
    assert len(closed) == 1


def test_findings_use_one_query_and_count_notes():
    with Store() as store:
        findings = [Finding("TEST", "low", str(i), nodes=[str(i)]) for i in range(100)]
        store.sync_findings(findings, {"TEST"})
        store.add_note(findings[0].id, "user", "one")
        store.add_note(findings[0].id, "user", "two")
        queries = []
        store.con.set_trace_callback(queries.append)
        rows = store.findings()
        assert len(queries) == 1
        assert next(row["notes"] for row in rows if row["id"] == findings[0].id) == 2
        assert sum(row["notes"] for row in rows) == 2


def test_recent_audit_is_limited_and_chronological():
    with Store() as store:
        for i in range(20):
            store.audit("user", str(i), {})
        assert [row["event"] for row in store.audit_log(limit=3)] == ["17", "18", "19"]
        assert store.audit_log(limit=0) == []
        assert len(store.audit_log()) == 20


def test_partial_or_failed_checks_preserve_existing_findings(tmp_path, monkeypatch):
    import intentflow.checks as checks
    project = dbt_loader.Project({})
    old = [Finding(code, "high", code) for code in ("REV_A", "REV_B", "REV_C", "REV_D")]
    registry = {
        "REV_A": Check("REV_A", "ok", "pre-merge", "static", lambda ctx: []),
        "REV_B": Check("REV_B", "other-stage", "continuous", "static", lambda ctx: []),
        "REV_C": Check("REV_C", "disabled", "pre-merge", "static", lambda ctx: []),
        "REV_D": Check("REV_D", "failed", "pre-merge", "static", lambda ctx: (_ for _ in ()).throw(ValueError("broken"))),
    }
    monkeypatch.setattr(checks, "REGISTRY", registry)
    monkeypatch.setattr(checks, "load_all", lambda: None)
    monkeypatch.setattr(runner.dbt_loader, "load", lambda *a: project)
    monkeypatch.setattr(runner.lin, "build", lambda *a, **k: SimpleNamespace(precision_report={}))
    monkeypatch.setattr(runner.dbt_results, "load", lambda *a: None)
    with Store() as store:
        store.set_setting("dbt.target", str(tmp_path))
        store.set_setting("policy.disabled_checks", ["REV_C"])
        store.sync_findings(old, set(registry))
        runner.run_check(store, stages={"pre-merge"})
        statuses = {f["id"]: f["status"] for f in store.findings(None)}
        assert statuses[old[0].id] == "resolved"
        assert all(statuses[f.id] == "open" for f in old[1:])


def test_check_ids_are_not_resolved_by_prefix():
    with Store() as store:
        finding = Finding("SQL010", "high", "keep")
        store.sync_findings([finding], {"SQL010"})
        store.sync_findings([], {"SQL01"})
        assert store.findings()[0]["id"] == finding.id


def test_sql_is_parsed_once_per_model_per_run(monkeypatch):
    from intentflow.checks import sql_quality
    project = dbt_loader.Project({"n": dbt_loader.DbtNode("n", "n", "model", compiled_code="select id from items")})
    ctx = Context(project=project)
    with patch.object(sql_quality, "_parse", wraps=sql_quality._parse) as parse:
        run_checks(ctx, only=["SQL001", "SQL002", "SQL003"])
        assert parse.call_count == 1
        project.nodes["n"].compiled_code = "select * from items"
        out = run_checks(ctx, only=["SQL001", "SQL002", "SQL003"])
        assert parse.call_count == 2
        assert ctx.parsed_sql


def test_project_downstream_handles_cycles_without_including_itself():
    project = dbt_loader.Project({
        "a": dbt_loader.DbtNode("a", "a", "model", depends_on=["b"]),
        "b": dbt_loader.DbtNode("b", "b", "model", depends_on=["a"]),
    })
    assert project.downstream("a") == ["b"]
    assert len(lineage_api.model_graph(project, "a", depth=100)["nodes"]) == 2


def test_lineage_cache_preserves_project_root(tmp_path, monkeypatch):
    target = str(tmp_path / "target")
    monkeypatch.setattr(lineage_api.dbt_loader, "load", lambda target, root: SimpleNamespace(root=root))
    monkeypatch.setattr(lineage_api.lin, "build", lambda *a, **k: object())
    a, _ = lineage_api.load(target, str(tmp_path / "a"))
    b, _ = lineage_api.load(target, str(tmp_path / "b"))
    assert a is not b and a.root != b.root




def test_mfa_cannot_be_reset_while_enabled(tmp_path):
    with Store(str(tmp_path / "mfa.db")) as store:
        store.add_user("user", "admin")
        secret = store.mfa_begin("user")
        code = totp.code_at(secret, int(time.time() // 30))
        assert store.mfa_confirm("user", code)
        with pytest.raises(ValueError, match="already on"):
            store.mfa_begin("user")
        assert store.user_record("user")["totp_enabled"] == 1
        assert not store.mfa_confirm("user", code)
        assert not store.mfa_check("missing", code)


def test_totp_is_consumed_once_under_concurrency(tmp_path, monkeypatch):
    path = str(tmp_path / "race.db")
    with Store(path) as store:
        store.add_user("user", "admin")
        secret = store.mfa_begin("user")
        step = int(time.time() // 30)
        assert store.mfa_confirm("user", totp.code_at(secret, step))
    barrier = threading.Barrier(2)

    def verify(*a, **k):
        barrier.wait(timeout=5)
        return step + 1

    monkeypatch.setattr(totp, "verify", verify)

    def attempt(_):
        with Store(path) as store:
            return store.mfa_check("user", "123456")

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]


def test_recovery_code_is_consumed_once_under_concurrency(tmp_path, monkeypatch):
    path = str(tmp_path / "recovery.db")
    with Store(path) as store:
        store.add_user("user", "admin")
        secret = store.mfa_begin("user")
        assert store.mfa_confirm("user", totp.code_at(secret, int(time.time() // 30)))
        code = store.mfa_recovery_codes("user")[0]
    barrier = threading.Barrier(2)
    original = Store.user_record

    def record(store, name):
        row = original(store, name)
        barrier.wait(timeout=5)
        return row

    monkeypatch.setattr(Store, "user_record", record)

    def attempt(_):
        with Store(path) as store:
            return store.mfa_use_recovery("user", code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]


def test_openlineage_preserves_start_across_requests_and_job_identity():
    start = {"eventType": "START", "eventTime": "2026-10-13T10:00:00Z", "run": {"runId": "same"},
             "job": {"namespace": "local", "name": "one"}}
    end = {**start, "eventType": "COMPLETE", "eventTime": "2026-10-13T10:02:30Z"}
    with Store() as store:
        openlineage.ingest_events(store, [start])
        openlineage.ingest_events(store, [end])
        openlineage.ingest_events(store, [start])
        row = store.runs()[0]
        assert row["state"] == "success" and row["duration"] == 150
        other = {**start, "job": {"namespace": "local", "name": "two"}}
        assert openlineage.ingest_events(store, [start, other])["runs"] == 2
        assert len(store.runs()) == 2


@pytest.mark.parametrize("relation", ["a; select 1", "a where true", "a join b on true", "read_csv('secret')", "(select * from a)", "a as other"])
def test_diff_rejects_sql_in_relation_before_query(relation):
    from intentflow import datadiff
    from intentflow.warehouses import Warehouse
    warehouse = Warehouse()
    with pytest.raises(ValueError, match="relation"):
        datadiff.diff_relations(warehouse, relation, "safe", ["id"])


@pytest.mark.parametrize("kwargs", [{"sample": -1}, {"sample": 1001}, {"sample_pct": 0}, {"sample_pct": float("nan")}])
def test_diff_rejects_unbounded_or_invalid_samples(kwargs):
    from intentflow import datadiff
    from intentflow.warehouses import Warehouse
    with pytest.raises(ValueError, match="sample"):
        datadiff.diff_relations(Warehouse(), "a", "b", ["id"], **kwargs)


def test_diff_detects_type_changes_with_equal_values():
    from intentflow import datadiff
    from intentflow.warehouses import DuckDBWarehouse
    warehouse = DuckDBWarehouse(":memory:", read_only=False)
    try:
        warehouse.con.execute("create table a as select 1 as id, 2::integer as value")
        warehouse.con.execute("create table b as select 1 as id, 2::bigint as value")
        diff = datadiff.diff_relations(warehouse, "a", "b", ["id"])
        assert diff["schema"]["type_changed"]
        assert diff["identical"] is False
    finally:
        warehouse.close()


def test_lineage_parallel_matches_serial_with_native_threads(tmp_path):
    import duckdb
    import synth_project
    from intentflow import lineage

    # DuckDB can hold native threads invisible to threading.active_count().
    with duckdb.connect() as warehouse:
        warehouse.execute("SELECT sum(i) FROM range(100000) t(i)").fetchone()
        synth_project.make(6, tmp_path)
        project = dbt_loader.load(tmp_path / "target")
        serial = lineage.build(project, workers=1)
        parallel = lineage.build(project, workers=2)
    assert set(parallel.edges) == set(serial.edges)
    assert parallel.unresolved == serial.unresolved
    assert parallel.columns_total == serial.columns_total


def test_lineage_pool_uses_spawn_and_one_initializer(monkeypatch):
    import multiprocessing
    from intentflow import lineage

    seen = {}

    class Pool:
        def __init__(self, workers, *, initializer, initargs):
            seen.update(workers=workers, initializer=initializer, initargs=initargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def imap_unordered(self, worker, todo, chunksize):
            return [(uid, uid) for uid in todo]

    def get_context(method):
        seen["method"] = method
        return SimpleNamespace(Pool=Pool)

    monkeypatch.setattr(multiprocessing, "get_context", get_context)
    monkeypatch.setattr(lineage.threading, "active_count", lambda: 1)
    assert lineage._compute("p", ["a", "b", "c", "d"], "s", "r", "d", 2) == {k: k for k in "abcd"}
    assert seen == {"method": "spawn", "workers": 2, "initializer": lineage._init_worker, "initargs": ("p", "s", "r", "d")}


@pytest.mark.parametrize("dialect, value, expected", [
    ("postgres", "Mixed.Name", '"mixed"."name"'),
    ("snowflake", "Mixed.Name", '"MIXED"."NAME"'),
    ("snowflake", '"Mixed"."Name"', '"Mixed"."Name"'),
    ("bigquery", "Mixed.Name", "`Mixed`.`Name`"),
])
def test_relation_preserves_dialect_identifier_semantics(dialect, value, expected):
    from intentflow.datadiff import _relation
    assert _relation(value, dialect) == expected


def test_persistent_secrets_fail_closed_without_key_or_crypto(tmp_path, monkeypatch):
    from intentflow import secrets_box

    monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)
    pg = secrets_box.Box("postgresql://localhost/local-test", backend="postgres")
    with pytest.raises(RuntimeError, match="encryption unavailable"):
        pg.enc("new-secret")
    assert pg.dec("legacy-secret") == "legacy-secret"
    assert pg.enc(None) is None

    def unavailable(_):
        raise ImportError("simulated missing optional dependency")

    monkeypatch.setattr(secrets_box, "_fernet", unavailable)
    sqlite = secrets_box.Box(str(tmp_path / "local.db"))
    assert sqlite.source == "unavailable"
    with pytest.raises(RuntimeError, match="encryption unavailable"):
        sqlite.enc("new-secret")


def test_mfa_does_not_persist_plaintext_when_crypto_unavailable(tmp_path, monkeypatch):
    from intentflow import secrets_box

    monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)
    def unavailable(_):
        raise ImportError("simulated missing optional dependency")
    monkeypatch.setattr(secrets_box, "_fernet", unavailable)
    with Store(str(tmp_path / "local.db")) as store:
        store.add_user("admin", "admin")
        with pytest.raises(RuntimeError, match="encryption unavailable"):
            store.mfa_begin("admin")
        assert store.con.execute("select totp_secret from users where name=?", ("admin",)).fetchone()[0] in (None, "")
