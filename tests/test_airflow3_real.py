"""Real Airflow 3.x (apache/airflow:3.x standalone container, API v2 with JWT). Skipped without INTENTFLOW_AIRFLOW3_URL.
DAGs in tests/airflow3_dags (new SDK). Credentials: AIRFLOW_USER/AIRFLOW_PASSWORD (simple auth manager)."""
import json, os, time, urllib.request

import pytest

from intentflow import monitoring
from intentflow.checks import Context, run_checks
from intentflow.orchestrators.airflow import AirflowClient

URL = os.environ.get("INTENTFLOW_AIRFLOW3_URL")
pytestmark = pytest.mark.skipif(not URL, reason="local Airflow 3 not configured")


def _trigger(client: AirflowClient, dag: str):
    req = urllib.request.Request(f"{URL}/api/v2/dags/{dag}/dagRuns", method="POST", data=json.dumps({"logical_date": None}).encode(),
                                 headers={**client._h(), "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def test_jwt_login_and_runs_against_real_airflow3():
    c = AirflowClient(URL, "v2")
    for d in ("it_ok", "it_fail"):
        _trigger(c, d)
    deadline, got = time.time() + 120, set()
    while time.time() < deadline:
        got = {(r.pipeline, r.state) for r in c.runs()}
        if ("it_ok", "success") in got and ("it_fail", "failed") in got:
            break
        time.sleep(3)
    assert ("it_ok", "success") in got and ("it_fail", "failed") in got, got
    assert all(r.duration is not None for r in c.runs() if r.state in ("success", "failed"))
    assert any(s.scope == "default_pool" and s.metric == "queued" for s in monitoring.airflow_pools(c))
    assert c.health()["scheduler"]["status"] == "healthy"


def test_wrong_password_gives_clear_error(monkeypatch):
    monkeypatch.setenv("AIRFLOW_PASSWORD", "errada")
    with pytest.raises(RuntimeError, match="login no Airflow 3 falhou"):
        AirflowClient(URL, "v2").runs()


def test_real_airflow3_history_feeds_checks():
    c = AirflowClient(URL, "v2")
    for _ in range(3):
        _trigger(c, "it_fail")
    deadline, runs = time.time() + 150, []
    while time.time() < deadline:
        runs = c.runs()
        mine = [r for r in runs if r.pipeline == "it_fail"]
        if sum(r.state == "failed" for r in mine) >= 3 and not any(r.state in ("running", "queued", "other") for r in mine):
            break
        time.sleep(3)
    assert any("airflow:it_fail" in x.title for x in run_checks(Context(runs=runs), only=["RUN001"]))
