"""Kafka Connect, Flink, Luigi, Rundeck and MLflow with responses in each API's documented format (local server)."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from intentflow import runner
from intentflow.orchestrators.extra import FlinkClient, KafkaConnectClient, LuigiClient, MLflowClient, RundeckClient
from intentflow.store import Store

CONNECT = {"orders-cdc": {"status": {"name": "orders-cdc", "connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "RUNNING"}]}},
           "users-cdc": {"status": {"name": "users-cdc", "connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "FAILED", "trace": "boom"}]}},
           "paused": {"status": {"name": "paused", "connector": {"state": "PAUSED"}, "tasks": []}}}
FLINK = {"jobs": [{"jid": "a1", "name": "enrich", "state": "RUNNING", "start-time": 1000000, "end-time": -1, "duration": 5000},
                  {"jid": "a2", "name": "batch", "state": "FINISHED", "start-time": 2000000, "end-time": 2060000, "duration": 60000},
                  {"jid": "a3", "name": "agg", "state": "FAILED", "start-time": 3000000, "end-time": 3010000, "duration": 10000}]}
LUIGI = {"response": {"Load(d=1)": {"name": "Load", "status": "DONE", "start_time": 1000.0, "last_updated": 1030.0},
                      "Fail(d=1)": {"name": "Fail", "status": "FAILED", "start_time": 2000.0, "last_updated": 2005.0},
                      "Wait(d=1)": {"name": "Wait", "status": "PENDING", "start_time": 0, "last_updated": 3000}}}
RUNDECK = {"executions": [{"id": 5, "status": "succeeded", "job": {"name": "nightly"}, "date-started": {"unixtime": 1000000}, "date-ended": {"unixtime": 1090000}, "permalink": "http://r/5"},
                          {"id": 6, "status": "failed", "job": {"name": "nightly"}, "date-started": {"unixtime": 2000000}, "date-ended": {"unixtime": 2010000}},
                          {"id": 7, "status": "running", "description": "adhoc", "date-started": {"unixtime": 3000000}}]}
MLFLOW_EXP = {"experiments": [{"experiment_id": "1", "name": "churn"}]}
MLFLOW_RUNS = {"runs": [{"info": {"run_id": "r1", "experiment_id": "1", "status": "FINISHED", "start_time": 1000000, "end_time": 1120000}},
                        {"info": {"run_id": "r2", "experiment_id": "1", "status": "FAILED", "start_time": 2000000, "end_time": 2005000}},
                        {"info": {"run_id": "r3", "experiment_id": "1", "status": "RUNNING", "start_time": 3000000}}]}


def test_kafka_connect_failed_task_is_failure():
    rs = {r.pipeline: r.state for r in KafkaConnectClient("http://c", fetch=lambda u, h: CONNECT).runs()}
    assert rs == {"orders-cdc": "running", "users-cdc": "failed", "paused": "other"}


def test_flink_jobs():
    rs = {r.run_id: r for r in FlinkClient("http://f", fetch=lambda u, h: FLINK).runs()}
    assert [rs[i].state for i in ("a1", "a2", "a3")] == ["running", "success", "failed"] and rs["a2"].duration == 60 and rs["a1"].end is None


def test_luigi_tasks():
    rs = {r.pipeline: r for r in LuigiClient("http://l", fetch=lambda u, h: LUIGI).runs()}
    assert (rs["Load"].state, rs["Load"].duration, rs["Fail"].state, rs["Wait"].state) == ("success", 30.0, "failed", "queued")


def test_rundeck_executions_and_token(monkeypatch):
    monkeypatch.setenv("RUNDECK_TOKEN", "tok")
    seen = []
    rs = RundeckClient("http://r", "dados", fetch=lambda u, h: (seen.append((u, h)) or RUNDECK)).runs()
    assert [(r.pipeline, r.state) for r in rs] == [("nightly", "success"), ("nightly", "failed"), ("adhoc", "running")] and rs[0].duration == 90
    assert seen[0][1]["X-Rundeck-Auth-Token"] == "tok" and "/project/dados/executions" in seen[0][0]


def test_mlflow_runs_use_experiment_names(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", "t")
    seen = []

    def post(url, body, headers):
        seen.append((url, body, headers))
        return MLFLOW_EXP if "experiments/search" in url else MLFLOW_RUNS
    rs = MLflowClient("http://m", post=post).runs()
    assert [(r.pipeline, r.state) for r in rs] == [("churn", "success"), ("churn", "failed"), ("churn", "running")] and rs[0].duration == 120
    assert seen[1][1]["experiment_ids"] == ["1"] and seen[0][2] == {"Authorization": "Bearer t"}


def test_collect_runs_wires_all_extras(tmp_path):
    routes = {"/connectors": CONNECT, "/jobs/overview": FLINK, "/api/task_list": LUIGI, "/api/41/project/dados/executions": RUNDECK,
              "/api/2.0/mlflow/experiments/search": MLFLOW_EXP, "/api/2.0/mlflow/runs/search": MLFLOW_RUNS}

    class H(BaseHTTPRequestHandler):
        def _go(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(n)
            body = routes.get(self.path.split("?")[0])
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())
        do_GET = do_POST = _go

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    u = f"http://127.0.0.1:{srv.server_address[1]}"
    st = Store(str(tmp_path / "s.db"))
    for k in ("kafka_connect.url", "flink.url", "luigi.url", "mlflow.url", "rundeck.url"):
        st.set_setting(k, u)
    st.set_setting("rundeck.project", "dados")
    r = runner.collect_runs(st)
    srv.shutdown()
    assert r["errors"] == [] and {x["system"] for x in st.runs()} == {"kafka_connect", "flink", "luigi", "mlflow", "rundeck"}


import os


@pytest.mark.skipif(not os.environ.get("INTENTFLOW_FLINK_URL"), reason="real Flink not configured")
def test_flink_real_cluster_finished_and_failed_jobs():
    rs = FlinkClient(os.environ["INTENTFLOW_FLINK_URL"]).runs()
    by = {r.state for r in rs}
    assert {"success", "failed"} <= by
    ok = next(r for r in rs if r.state == "success")
    assert ok.duration is not None and ok.duration > 0 and ok.start and ok.end and ok.end >= ok.start
    assert all(r.run_id and len(r.run_id) == 32 for r in rs)                    # jid real do Flink
