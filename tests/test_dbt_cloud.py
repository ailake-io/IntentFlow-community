"""dbt Cloud: local HTTP server with the documented API v2 format (never against the real dbt Cloud)."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from intentflow import cli, dbt_loader, runner
from intentflow.orchestrators.dbtcloud import DbtCloudClient, _hms
from intentflow.store import Store

RUNS = {"data": [
    {"id": 300, "job_definition_id": 7, "status": 10, "started_at": "2026-10-10 10:00:00.1+00:00", "finished_at": "2026-10-10 10:02:00+00:00",
     "duration": "00:02:00", "run_duration": 118, "href": "https://cloud.getdbt.com/deploy/1/projects/2/runs/300", "job": {"name": "nightly build"}},
    {"id": 301, "job_definition_id": 7, "status": 20, "started_at": "2026-10-10 11:00:00+00:00", "finished_at": "2026-10-10 11:00:30+00:00",
     "duration": "00:00:30", "job": {"name": "nightly build"}},
    {"id": 302, "job_definition_id": 8, "status": 3, "started_at": "2026-10-10 12:00:00+00:00", "finished_at": None, "job": None},
    {"id": 303, "job_definition_id": 8, "status": 30, "started_at": "2026-10-10 12:30:00+00:00", "finished_at": "2026-10-10 12:30:05+00:00"},
]}


@pytest.fixture
def api(shop_target):
    t = Path(shop_target) if (Path(shop_target) / "manifest.json").exists() else Path(shop_target) / "target"
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if "/runs/" in self.path and self.path.split("?")[0].endswith("/runs/"):
                body = RUNS
            elif "/jobs/7/artifacts/" in self.path or "/runs/300/artifacts/" in self.path:
                name = self.path.rsplit("/", 1)[1]
                if name == "catalog.json" and "/runs/300" in self.path:      # optional catalog missing in this run
                    self.send_response(404); self.end_headers(); return
                body = json.loads((t / name).read_text())
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def test_runs_are_normalized(api, monkeypatch):
    url, seen = api
    monkeypatch.setenv("DBT_CLOUD_TOKEN", "svc")
    rs = {r.run_id: r for r in DbtCloudClient(url, 1).runs()}
    assert [rs[i].state for i in ("300", "301", "302", "303")] == ["success", "failed", "running", "failed"]
    assert rs["300"].pipeline == "nightly build" and rs["300"].duration == 118 and rs["301"].duration == 30   # run_duration, otherwise HH:MM:SS
    assert rs["302"].pipeline == "job-8" and rs["302"].end is None and rs["300"].start < rs["300"].end
    assert seen[0][1] == "Token svc" and "/api/v2/accounts/1/runs/" in seen[0][0]
    assert _hms("01:02:03") == 3723 and _hms(None) is None


def test_artifacts_pull_feeds_the_normal_loader_and_checks(api, tmp_path, monkeypatch):
    url, _ = api
    monkeypatch.setenv("DBT_CLOUD_TOKEN", "svc")
    r = DbtCloudClient(url, 1).pull_artifacts(7, tmp_path / "t")
    assert set(r["files"]) == {"manifest.json", "catalog.json", "run_results.json"} or set(r["files"]) >= {"manifest.json"}
    p = dbt_loader.load(tmp_path / "t")
    assert p.nodes and p.adapter
    only = DbtCloudClient(url, 1).pull_artifacts(7, tmp_path / "t2", run_id=300)     # specific run, without a catalog
    assert "catalog.json" in only["missing"] and "manifest.json" in only["files"]
    with pytest.raises(RuntimeError, match="manifest.json unavailable"):
        DbtCloudClient(url, 1).pull_artifacts(99, tmp_path / "t3")


def test_run_check_uses_cloud_artifacts_when_no_local_target(api, tmp_path, monkeypatch):
    url, _ = api
    monkeypatch.setenv("DBT_CLOUD_TOKEN", "svc")
    st = Store(str(tmp_path / "s.db"))
    for k, v in {"dbt_cloud.host": url, "dbt_cloud.account_id": 1, "dbt_cloud.job_id": 7}.items():
        st.set_setting(k, v)
    res = runner.run_check(st)
    assert res["project"].nodes and (tmp_path / ".intentflow_dbt_cloud" / "job-7" / "manifest.json").exists()
    assert runner.collect_runs(st)["runs"] == 4 and {r["system"] for r in st.runs()} == {"dbt_cloud"}


def test_cli_runs_and_pull(api, tmp_path, capsys, monkeypatch):
    url, _ = api
    monkeypatch.setenv("DBT_CLOUD_TOKEN", "svc")
    assert cli.main(["--store", str(tmp_path / "s.db"), "dbt-cloud", "runs", "--account", "1", "--host", url]) == 0
    assert "nightly build" in capsys.readouterr().out
    assert cli.main(["--store", str(tmp_path / "s.db"), "dbt-cloud", "pull", "--account", "1", "--host", url, "--job", "7", "--dest", str(tmp_path / "d")]) == 0
    assert "dbt.target" in capsys.readouterr().out
