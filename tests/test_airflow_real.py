"""Real Airflow 2.10 (local container, API v1 with basic auth). Skipped without INTENTFLOW_AIRFLOW_URL."""
import base64, json, os, time, urllib.request

import pytest

from intentflow import monitoring
from intentflow.checks import Context, run_checks
from intentflow.orchestrators.airflow import AirflowClient

URL = os.environ.get("INTENTFLOW_AIRFLOW_URL")
pytestmark = pytest.mark.skipif(not URL, reason="local Airflow not configured")


def _api(method, path, body=None):
    tok = base64.b64encode(f"{os.environ['AIRFLOW_USER']}:{os.environ['AIRFLOW_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(f"{URL}/api/v1{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Basic {tok}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def test_airflow_client_against_real_server():
    for d in ("it_ok", "it_fail"):
        _api("POST", f"/dags/{d}/dagRuns", {})
    c = AirflowClient(URL, "v1")
    deadline = time.time() + 90
    while time.time() < deadline:
        got = {(r.pipeline, r.state) for r in c.runs()}
        if ("it_ok", "success") in got and ("it_fail", "failed") in got:
            break
        time.sleep(3)
    assert ("it_ok", "success") in got and ("it_fail", "failed") in got, got
    assert all(r.duration is not None for r in c.runs() if r.state in ("success", "failed"))
    pools = monitoring.airflow_pools(c)
    assert any(s.scope == "default_pool" and s.metric == "queued" for s in pools)
    assert c.health()["scheduler"]["status"] == "healthy"


def test_real_history_feeds_checks():
    c = AirflowClient(URL, "v1")
    runs = c.runs()
    # three failures in a row of it_fail need 3 runs: trigger two more and wait
    for _ in range(2):
        _api("POST", "/dags/it_fail/dagRuns", {})
    deadline = time.time() + 120
    while time.time() < deadline:
        runs = c.runs()
        mine = [r for r in runs if r.pipeline == "it_fail"]
        if sum(r.state == "failed" for r in mine) >= 3 and not any(r.state in ("running", "queued", "other") for r in mine):
            break
        time.sleep(3)
    f = run_checks(Context(runs=runs), only=["RUN001"])
    assert any("airflow:it_fail" in x.title for x in f)
