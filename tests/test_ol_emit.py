import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from intentflow import lineage, ol_emit, openlineage, sqlmesh_loader
from intentflow.store import Store

FX = Path(__file__).parent / "fixtures/sqlmesh_shop_export.json"


def _events():
    p = sqlmesh_loader.from_export(json.loads(FX.read_text()))
    return p, ol_emit.build_events(p, lineage.build(p), now=1_800_000_000)


def test_events_have_spec_shape_and_column_lineage():
    p, evs = _events()
    assert {e["job"]["name"] for e in evs} == {"stg_customers", "fct_orders", "revenue_by_country"}     # seeds are not jobs
    rev = next(e for e in evs if e["job"]["name"] == "revenue_by_country")
    assert rev["eventType"] == "COMPLETE" and rev["run"]["runId"] and rev["inputs"][0]["name"] == "shop.shop.fct_orders"
    cl = rev["outputs"][0]["facets"]["columnLineage"]["fields"]
    assert cl["revenue"]["inputFields"][0]["field"] == "amount" and cl["revenue"]["transformationType"] == "TRANSFORMATION"
    assert cl["country"]["transformationType"] == "IDENTITY"
    assert rev["outputs"][0]["facets"]["schema"]["_schemaURL"].endswith("SchemaDatasetFacet.json") or "Schema" in rev["outputs"][0]["facets"]["schema"]["_schemaURL"]
    assert [e["run"]["runId"] for e in evs] == [e["run"]["runId"] for e in _events()[1]]             # idempotente


def test_round_trip_through_our_own_receiver_and_http_emit(tmp_path):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, evs = _events()
        r = ol_emit.emit(f"http://127.0.0.1:{srv.server_address[1]}", evs, {"Authorization": "Bearer x"})
    finally:
        srv.shutdown()
    assert r == {"sent": 3, "errors": []} and all(path == "/api/v1/lineage" for path, _ in got)
    st = Store(str(tmp_path / "s.db"))
    dropped = openlineage.ingest_events(st, [ev for _, ev in got])          # default: the project already has this lineage
    assert dropped["runs"] == 3 and dropped["column_edges"] == 0
    st.set_setting("openlineage.keep_project_lineage", "true")
    res = openlineage.ingest_events(st, [ev for _, ev in got])
    assert res["runs"] == 3 and res["column_edges"] >= 8
    assert any("revenue" in e["dst_col"] for e in st.ol_edges())


def test_unreachable_catalog_reports_error_without_raising():
    _, evs = _events()
    r = ol_emit.emit("http://127.0.0.1:9", evs, timeout=1)
    assert r["sent"] == 0 and r["errors"]


import os

import pytest

MARQUEZ = os.environ.get("INTENTFLOW_MARQUEZ_URL")


@pytest.mark.skipif(not MARQUEZ, reason="real Marquez not configured")
def test_marquez_accepts_events_and_exposes_column_lineage():
    import urllib.request
    p, evs = _events()
    r = ol_emit.emit(MARQUEZ, evs)
    assert r == {"sent": 3, "errors": []}

    def get(path):
        return json.loads(urllib.request.urlopen(f"{MARQUEZ}{path}", timeout=15).read())
    jobs = {j["name"]: j for j in get("/api/v1/namespaces/intentflow/jobs")["jobs"]}
    assert set(jobs) >= {"stg_customers", "fct_orders", "revenue_by_country"} and jobs["fct_orders"]["latestRun"]["state"] == "COMPLETED"
    g = get("/api/v1/lineage?nodeId=dataset:duckdb:shop.shop.revenue_by_country&depth=3")["graph"]
    ids = {n["id"] for n in g}
    assert "dataset:duckdb:shop.shop.raw_orders" in ids and "dataset:duckdb:shop.shop.revenue_by_country" in ids      # atravessa 2 saltos
    cl = get("/api/v1/column-lineage?nodeId=datasetField:duckdb:shop.shop.revenue_by_country:revenue&depth=3")["graph"]
    fields = {n["id"] for n in cl}
    assert "datasetField:duckdb:shop.shop.raw_orders:amount" in fields                     # revenue ← fct_orders.amount ← raw_orders.amount
