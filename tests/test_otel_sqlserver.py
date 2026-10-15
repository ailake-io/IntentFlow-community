import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from intentflow import cli, otel
from intentflow.model import Finding
from intentflow.orchestrators import Run
from intentflow.store import Store


def _store(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.sync_findings([Finding("DBT001", "high", "a", nodes=["x"]), Finding("SQL001", "medium", "b", nodes=["y"]), Finding("SQL001", "medium", "c", nodes=["z"])],
                     {"DBT001", "SQL001"})
    st.upsert_runs([Run("airflow", "etl", "r1", "failed", 1.0, 2.0, 1.0, "u"), Run("airflow", "etl", "r2", "success", 3.0, 4.0, 1.0, "u")])
    return st


def test_payload_follows_otlp_json_shape(tmp_path):
    p = otel.build_payload(_store(tmp_path), now=1_800_000_000)
    m = {x["name"]: x for x in p["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    assert p["resourceMetrics"][0]["resource"]["attributes"][0] == {"key": "service.name", "value": {"stringValue": "intentflow"}}
    pts = {tuple((a["key"], a["value"]["stringValue"]) for a in d["attributes"]): d["asDouble"] for d in m["intentflow.findings.open"]["gauge"]["dataPoints"]}
    assert pts == {(("severity", "high"),): 1.0, (("severity", "medium"),): 2.0}
    assert m["intentflow.findings.open"]["gauge"]["dataPoints"][0]["timeUnixNano"] == "1800000000000000000"
    runs = {tuple(a["value"]["stringValue"] for a in d["attributes"]): d["asDouble"] for d in m["intentflow.runs"]["gauge"]["dataPoints"]}
    assert runs == {("airflow", "failed"): 1.0, ("airflow", "success"): 1.0}


def test_push_posts_to_v1_metrics_with_headers_and_cli(tmp_path, monkeypatch, capsys):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers.get("x-api-key"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=abc, outro=1")
    db = tmp_path / "s.db"
    _store(tmp_path)
    assert cli.main(["--store", str(db), "otel-push", "--endpoint", f"http://127.0.0.1:{srv.server_address[1]}"]) == 0
    srv.shutdown()
    assert got[0][0] == "/v1/metrics" and got[0][1] == "abc" and "resourceMetrics" in got[0][2]
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert cli.main(["--store", str(db), "otel-push"]) == 1 and "OTEL_EXPORTER_OTLP_ENDPOINT" in capsys.readouterr().err


def test_sqlserver_quote_describe_and_null_safe_compare_with_fake_connection():
    from intentflow.warehouses.more import SQLServerWarehouse

    class Cur:
        def __init__(self):
            self.description, self.sql = None, ""

        def execute(self, sql):
            self.sql = sql
            self.description = [("c",)]

        def fetchall(self):
            return [("id", "int"), ("amount", "decimal")] if "information_schema" in self.sql else []

        def close(self):
            pass

    class Con:
        def cursor(self):
            return Cur()
    wh = SQLServerWarehouse(connection=Con())
    assert wh.quote("a]b") == "[a]]b]" and wh.describe("shop.dbo.orders") == [("id", "int"), ("amount", "decimal")]
    assert wh.differs("b.x", "h.x") == "not exists (select b.x intersect select h.x)"


@pytest.mark.skipif(not os.environ.get("INTENTFLOW_OTEL_URL"), reason="real OpenTelemetry Collector not configured")
def test_real_collector_accepts_the_payload(tmp_path):
    assert otel.push(_store(tmp_path), os.environ["INTENTFLOW_OTEL_URL"]) == 200     # the Collector validates OTLP; an invalid format gives 400
