import json, os, shutil, subprocess, sys, threading, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from conftest import new_db
import pytest

from intentflow import dbt_loader, lineage, openlineage
from intentflow.checks import Context, run_checks
from intentflow.store import Store

ROOT = Path(__file__).resolve().parent.parent

OL = [
    {"eventType": "START", "eventTime": "2026-10-11T10:00:00.000Z", "run": {"runId": "r1"}, "job": {"namespace": "airflow", "name": "etl.load"},
     "inputs": [{"namespace": "pg", "name": "public.orders"}], "outputs": [], "producer": "p", "schemaURL": "u"},
    {"eventType": "COMPLETE", "eventTime": "2026-10-11T10:02:30.000Z", "run": {"runId": "r1"}, "job": {"namespace": "airflow", "name": "etl.load"},
     "inputs": [{"namespace": "pg", "name": "public.orders"}],
     "outputs": [{"namespace": "wh", "name": "mart.sales", "facets": {"columnLineage": {"fields": {
         "total": {"inputFields": [{"namespace": "pg", "name": "public.orders", "field": "amount"}]}}}}}],
     "producer": "p", "schemaURL": "u"},
    {"eventType": "START", "eventTime": "2026-10-11T11:00:00.000Z", "run": {"runId": "r2"}, "job": {"namespace": "spark", "name": "agg"}, "inputs": [], "outputs": []},
    {"eventType": "FAIL", "eventTime": "2026-10-11T11:00:10.000Z", "run": {"runId": "r2"}, "job": {"namespace": "spark", "name": "agg"}, "inputs": [], "outputs": []},
]


def test_openlineage_events_become_runs_jobs_and_column_edges(tmp_path):
    st = Store()
    r = openlineage.ingest_events(st, OL)
    assert r == {"runs": 2, "column_edges": 1}
    runs = {x["pipeline"]: x for x in st.runs()}
    assert runs["airflow.etl.load"]["state"] == "success" and runs["airflow.etl.load"]["duration"] == 150.0
    assert runs["spark.agg"]["state"] == "failed"
    assert st.ol_jobs()[0]["outputs"] == ["wh:mart.sales"]
    e = st.ol_edges()[0]
    assert (e["src_ds"], e["src_col"], e["dst_ds"], e["dst_col"]) == ("pg:public.orders", "amount", "wh:mart.sales", "total")
    f = tmp_path / "ev.ndjson"
    f.write_text("\n".join(json.dumps(e) for e in OL))
    assert openlineage.ingest_file(Store(), str(f))["runs"] == 2


def test_openlineage_http_receiver(tmp_path):
    db = new_db(tmp_path, "o.db")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), openlineage.make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for ev in OL:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/v1/lineage", data=json.dumps(ev).encode(), method="POST")
            assert urllib.request.urlopen(req, timeout=5).status == 200
    finally:
        srv.shutdown()
    assert {r["pipeline"] for r in Store(db).runs()} == {"airflow.etl.load", "spark.agg"}


PG = os.environ.get("INTENTFLOW_PG_URL")


@pytest.mark.skipif(not PG, reason="local PostgreSQL not configured")
def test_dbt_on_postgres_lineage_probes_plan_and_diff(tmp_path):
    pytest.importorskip("dbt.adapters.postgres")
    import psycopg2
    from urllib.parse import urlparse
    from intentflow import datadiff
    from intentflow.warehouses import PostgresWarehouse
    u = urlparse(PG)
    try:
        con = psycopg2.connect(PG, connect_timeout=3)
    except psycopg2.OperationalError:
        pytest.skip("local PostgreSQL is not reachable (container stopped)")
    con.autocommit = True
    con.cursor().execute("drop schema if exists it_shop cascade; create schema it_shop;")
    proj = tmp_path / "shop_pg"
    shutil.copytree(ROOT / "examples/dbt_shop", proj, ignore=shutil.ignore_patterns("target", "logs"))
    (proj / "profiles.yml").write_text(
        f"shop:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n      host: {u.hostname}\n      port: {u.port}\n"
        f"      user: {u.username}\n      password: \"{{{{ env_var('PGPASSWORD') }}}}\"\n      dbname: {u.path.lstrip('/')}\n      schema: it_shop\n      threads: 1\n")
    env = {**os.environ, "PGPASSWORD": u.password or ""}
    dbt = str(Path(sys.executable).parent / "dbt")
    for cmd in (["build"], ["docs", "generate"]):
        r = subprocess.run([dbt, *cmd, "--profiles-dir", "."], cwd=proj, capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout[-1500:]
    p = dbt_loader.load(proj / "target", proj)
    assert p.adapter == "postgres"
    lin = lineage.build(p)
    assert lin.precision_report["resolved_ratio"] == 1.0
    cust = next(u_ for u_, n in p.nodes.items() if n.name == "customers")
    assert any(p.nodes[e.src].name == "stg_payments" for e in lin.upstream_of(cust, "lifetime_value"))
    wh = PostgresWarehouse("INTENTFLOW_PG_URL")
    f = run_checks(Context(project=p, lineage=lin, warehouse=wh, store=Store(), policies={}), only=["PRB001", "SQL013", "SQL012"])
    assert not [x for x in f if x.check == "PRB001"]
    assert wh.plan("select * from it_shop.stg_orders")["max_rows"] is not None
    con.cursor().execute("create table it_shop.orders_v2 as select * from it_shop.orders; update it_shop.orders_v2 set status='x' where order_id=1")
    d = datadiff.diff_relations(wh, "it_shop.orders", "it_shop.orders_v2", ["order_id"])
    assert d["changed_rows"] == 1 and d["changed_by_column"] == {"status": 1}
