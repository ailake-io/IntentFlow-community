"""Real Prefect and Dagster (local servers). Skipped without INTENTFLOW_PREFECT_URL / INTENTFLOW_DAGSTER_URL.
Also cover normalization with known-format responses (always run)."""
import os

import pytest

from intentflow import runner
from intentflow.orchestrators.dagster import DagsterClient
from intentflow.orchestrators.prefect import PrefectClient
from intentflow.store import Store


def test_prefect_normalization_with_known_shapes():
    def post(url, body, headers):
        if url.endswith("/flow_runs/filter"):
            return [{"id": "r1", "flow_id": "f1", "name": "x", "state": {"type": "COMPLETED"}, "start_time": "2026-10-11T10:00:00Z",
                     "end_time": "2026-10-11T10:01:00Z", "total_run_time": 60.0},
                    {"id": "r2", "flow_id": "f1", "name": "y", "state": {"type": "CRASHED"}, "start_time": None, "end_time": None}]
        return [{"id": "f1", "name": "etl"}]
    rs = PrefectClient("http://p:4200/api", post=post).runs()
    assert [(r.pipeline, r.state, r.duration) for r in rs] == [("etl", "success", 60.0), ("etl", "failed", None)]


def test_dagster_normalization_and_error_surface():
    ok = {"data": {"runsOrError": {"__typename": "Runs", "results": [
        {"runId": "a", "jobName": "j", "status": "SUCCESS", "startTime": 10.0, "endTime": 14.5},
        {"runId": "b", "jobName": "j", "status": "STARTED", "startTime": 20.0, "endTime": None}]}}}
    rs = DagsterClient("http://d:3000", gql=lambda *a: ok).runs()
    assert [(r.state, r.duration) for r in rs] == [("success", 4.5), ("running", None)]
    bad = {"data": {"runsOrError": {"__typename": "PythonError", "message": "instância indisponível"}}}
    with pytest.raises(RuntimeError, match="indisponível"):
        DagsterClient("http://d:3000", gql=lambda *a: bad).runs()


@pytest.mark.skipif(not os.environ.get("INTENTFLOW_PREFECT_URL"), reason="local Prefect not configured")
def test_prefect_against_real_server():
    rs = PrefectClient(os.environ["INTENTFLOW_PREFECT_URL"]).runs()
    got = {(r.pipeline, r.state) for r in rs}
    assert ("it_prefect_ok", "success") in got and ("it_prefect_fail", "failed") in got


@pytest.mark.skipif(not os.environ.get("INTENTFLOW_DAGSTER_URL"), reason="local Dagster not configured")
def test_dagster_against_real_server():
    rs = DagsterClient(os.environ["INTENTFLOW_DAGSTER_URL"]).runs()
    got = {(r.pipeline, r.state) for r in rs}
    assert ("it_dagster_ok", "success") in got and ("it_dagster_fail", "failed") in got
    assert all(r.duration is not None for r in rs if r.state in ("success", "failed"))


@pytest.mark.skipif(not (os.environ.get("INTENTFLOW_PREFECT_URL") and os.environ.get("INTENTFLOW_DAGSTER_URL")), reason="local servers not configured")
def test_collect_runs_unifies_all_orchestrators():
    st = Store()
    st.set_setting("prefect.url", os.environ["INTENTFLOW_PREFECT_URL"])
    st.set_setting("dagster.url", os.environ["INTENTFLOW_DAGSTER_URL"])
    r = runner.collect_runs(st)
    assert r["errors"] == [] and {x["system"] for x in st.runs()} == {"prefect", "dagster"}
