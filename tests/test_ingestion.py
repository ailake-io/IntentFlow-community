"""Airbyte and Fivetran against local HTTP servers with the documented format (never against the real services)."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from intentflow import runner
from intentflow.checks import Context, run_checks
from intentflow.orchestrators.ingestion import AirbyteClient, FivetranClient, _iso_seconds
from intentflow.store import Store

AB_CONN = {"data": [{"connectionId": "c1", "name": "shopify → warehouse"}, {"connectionId": "c2", "name": "crm → warehouse"}]}
AB_JOBS = {"data": [
    {"jobId": 10, "status": "succeeded", "jobType": "sync", "connectionId": "c1", "startTime": "2026-10-10T10:00:00Z",
     "lastUpdatedAt": "2026-10-10T10:05:00Z", "duration": "PT5M", "rowsSynced": 100},
    {"jobId": 11, "status": "failed", "jobType": "sync", "connectionId": "c2", "startTime": "2026-10-10T11:00:00Z",
     "lastUpdatedAt": "2026-10-10T11:00:30Z"},
    {"jobId": 12, "status": "running", "jobType": "sync", "connectionId": "c1", "startTime": "2026-10-10T12:00:00Z", "lastUpdatedAt": "2026-10-10T12:01:00Z"},
]}
FT = {"data": {"items": [
    {"id": "abc", "schema": "shop", "paused": False, "status": {"sync_state": "scheduled"}, "succeeded_at": "2026-10-10T09:00:00Z", "failed_at": "2026-10-09T09:00:00Z"},
    {"id": "def", "schema": "crm", "paused": False, "status": {"sync_state": "scheduled"}, "succeeded_at": "2026-10-09T09:00:00Z", "failed_at": "2026-10-10T09:00:00Z"},
    {"id": "ghi", "schema": "old", "paused": True, "status": {}, "succeeded_at": "2026-01-01T00:00:00Z"},
    {"id": "jkl", "schema": "ads", "paused": False, "status": {"sync_state": "syncing"}, "succeeded_at": "2026-10-09T09:00:00Z", "sync_started_at": "2026-10-10T13:00:00Z"},
], "next_cursor": None}}


@pytest.fixture
def server():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            body = AB_CONN if "/connections" in self.path else AB_JOBS if "/jobs" in self.path else FT if "/v1/connectors" in self.path else None
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def test_airbyte_jobs_are_normalized_with_connection_names(server, monkeypatch):
    url, seen = server
    monkeypatch.setenv("AIRBYTE_TOKEN", "tok")
    rs = {r.run_id: r for r in AirbyteClient(url).runs()}
    assert rs["10"].pipeline == "shopify → warehouse:sync" and rs["10"].state == "success" and rs["10"].duration == 300
    assert rs["11"].state == "failed" and rs["11"].duration == 30 and rs["12"].state == "running" and rs["12"].end is None
    assert all(a == "Bearer tok" for _, a in seen)


def test_fivetran_last_sync_outcome_pause_and_running(server, monkeypatch):
    url, seen = server
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    rs = {r.pipeline: r.state for r in FivetranClient(url).runs()}
    assert rs == {"shop": "success", "crm": "failed", "ads": "running"}          # paused ones do not show up
    assert seen[0][1].startswith("Basic ")


def test_collect_runs_feeds_the_store_and_checks(server, tmp_path, monkeypatch):
    for k in ("AIRBYTE_CLIENT_ID", "AIRBYTE_CLIENT_SECRET", "AIRBYTE_TOKEN", "FIVETRAN_API_KEY", "FIVETRAN_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    url, _ = server
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("airbyte.url", url)
    st.set_setting("fivetran.url", url)
    r = runner.collect_runs(st)
    assert r["errors"] == [] and {x["system"] for x in st.runs()} == {"airbyte", "fivetran"}


def test_iso_duration():
    assert _iso_seconds("PT1H2M3.5S") == 3723.5 and _iso_seconds("P1DT1S") == 86401 and _iso_seconds("") is None and _iso_seconds("lixo") is None


def test_argo_and_mage_normalize_runs(monkeypatch):
    from intentflow.orchestrators.more import ArgoClient, MageClient
    monkeypatch.setenv("ARGO_TOKEN", "t")
    seen = []
    argo = {"items": [
        {"metadata": {"name": "etl-abc", "generateName": "etl-", "labels": {"workflows.argoproj.io/workflow-template": "etl-template"}},
         "status": {"phase": "Succeeded", "startedAt": "2026-10-10T10:00:00Z", "finishedAt": "2026-10-10T10:02:00Z"}},
        {"metadata": {"name": "load-xyz", "generateName": "load-"}, "status": {"phase": "Error", "startedAt": "2026-10-10T11:00:00Z", "finishedAt": "2026-10-10T11:00:10Z"}},
        {"metadata": {"name": "run-1"}, "status": {"phase": "Running", "startedAt": "2026-10-10T12:00:00Z"}},
    ]}
    rs = ArgoClient("http://argo:2746", "data", fetch=lambda u, h: (seen.append((u, h)) or argo)).runs()
    assert [(r.pipeline, r.state) for r in rs] == [("etl-template", "success"), ("load", "failed"), ("run-1", "running")]
    assert rs[0].duration == 120 and rs[2].end is None
    assert seen[0][0].endswith("/api/v1/workflows/data?listOptions.limit=100") and seen[0][1] == {"Authorization": "Bearer t"}
    mage = {"pipeline_runs": [
        {"id": 7, "pipeline_uuid": "orders_etl", "status": "completed", "started_at": "2026-10-10 10:00:00+00:00", "completed_at": "2026-10-10 10:01:00+00:00"},
        {"id": 8, "pipeline_uuid": "orders_etl", "status": "failed", "execution_date": "2026-10-10T11:00:00Z"},
        {"id": 9, "pipeline_uuid": "x", "status": "initial"}]}
    seen_m = []
    monkeypatch.setenv("MAGE_USER", "u")
    monkeypatch.setenv("MAGE_PASSWORD", "p")
    mc = MageClient("http://mage:6789", fetch=lambda u, h: (seen_m.append((u, h)) or mage), login=lambda u, p: "inner-token")
    ms = mc.runs()
    assert [(r.run_id, r.state) for r in ms] == [("7", "success"), ("8", "failed"), ("9", "queued")] and ms[0].duration == 60
    assert seen_m[0][1] == {"Authorization": "Bearer inner-token"} and "api_key=" in seen_m[0][0]
    assert ms[1].end is None or ms[1].end > 0                                    # without completed_at/updated_at: no made-up duration
    monkeypatch.delenv("MAGE_USER")
    monkeypatch.delenv("MAGE_PASSWORD")
    denied = MageClient("http://mage", fetch=lambda u, h: {"error": {"code": 403, "message": "Private access for list only"}})
    with pytest.raises(RuntimeError, match="MAGE_USER"):
        denied.runs()


def test_naive_timestamps_are_utc():
    from intentflow.orchestrators import ts
    assert ts("2026-10-10 10:00:00") == ts("2026-10-10T10:00:00Z") == ts("2026-10-10T10:00:00+00:00")


def test_airbyte_client_credentials_exchange_is_cached(monkeypatch):
    monkeypatch.delenv("AIRBYTE_TOKEN", raising=False)
    monkeypatch.setenv("AIRBYTE_CLIENT_ID", "id")
    monkeypatch.setenv("AIRBYTE_CLIENT_SECRET", "sec")
    calls, seen = [], []
    c = AirbyteClient("http://ab", fetch=lambda u, h: (seen.append(h) or {"data": []}), login=lambda i, s: calls.append((i, s)) or "tok-1")
    c.runs(); c.runs()
    assert calls == [("id", "sec")] and seen[0] == {"Authorization": "Bearer tok-1"}
