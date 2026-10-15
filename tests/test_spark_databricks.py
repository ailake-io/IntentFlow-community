"""Real Spark (apache/spark:3.5.3 in a container, INTENTFLOW_SPARK_URL) and Databricks Jobs with API-format responses."""
import os

import pytest

from intentflow.checks import Context, run_checks
from intentflow.orchestrators.spark import DatabricksJobsClient, SparkClient

URL = os.environ.get("INTENTFLOW_SPARK_URL")


@pytest.mark.skipif(not URL, reason="local Spark not configured")
def test_spark_ui_real_runs_and_samples():
    c = SparkClient(URL)
    runs = c.runs()
    assert runs and runs[0].system == "spark" and runs[0].pipeline == "it_spark_app" and runs[0].state == "running"
    ss = c.samples()
    mem = [s for s in ss if s.metric == "memory_bytes"]
    assert mem and mem[0].value > 0 and mem[0].limit > mem[0].value          # peak heap vs spark.driver.memory (1g in the container)
    assert mem[0].limit == 1024**3


def _fake(table):
    return lambda url, headers=None: next(v for k, v in table.items() if k in url)


def test_size_parser_and_heap_vs_configured_limit():
    from intentflow.orchestrators.spark import _size
    assert _size("1g") == 1024**3 and _size("512m") == 512 * 1024**2 and _size("2048") == 2048 * 1024**2 and _size("x") is None
    t = {"/environment": {"sparkProperties": [["spark.executor.memory", "2g"]]},
         "/allexecutors": [{"id": "1", "memoryUsed": 10, "maxMemory": 1000, "peakMemoryMetrics": {"JVMHeapMemory": 1.5 * 1024**3}}],
         "/stages": [], "/applications": [{"id": "a", "name": "n", "attempts": [{}]}]}
    m = [s for s in SparkClient("http://s", fetch=_fake(t)).samples() if s.metric == "memory_bytes"][0]
    assert m.value == 1.5 * 1024**3 and m.limit == 2 * 1024**3


def test_spark_completed_app_with_failed_job_is_failed_and_spill_and_oom_become_samples():
    t = {
        "/allexecutors": [{"id": "1", "memoryUsed": 900, "maxMemory": 1000, "removeReason": "Container killed by YARN for exceeding memory limits"}],
        "/stages": [{"memoryBytesSpilled": 5e9, "diskBytesSpilled": 1e9}, {"memoryBytesSpilled": 0, "diskBytesSpilled": 0}],
        "/jobs": [{"status": "SUCCEEDED"}, {"status": "FAILED"}],
        "/applications": [{"id": "app-1", "name": "etl", "attempts": [{"completed": True, "startTimeEpoch": 1000000, "endTimeEpoch": 1060000}]}],
    }
    c = SparkClient("http://spark", fetch=_fake(t))
    r = c.runs()[0]
    assert (r.state, r.duration, r.pipeline) == ("failed", 60.0, "etl")
    m = {(s.metric, s.scope): s for s in c.samples()}
    assert m[("spilled_bytes", "etl")].value == 6e9 and ("oom_killed", "etl:1") in m
    assert m[("memory_bytes", "etl:1")].limit == 1000


def test_spill_and_oom_samples_reach_the_existing_cluster_checks():
    import time
    from intentflow.monitoring import Sample
    now = time.time()
    s = [Sample(now - 60, "spark", "etl:1", "oom_killed", 1.0)]
    f = run_checks(Context(samples=s), only=["CLU003", "CLU004"])
    assert any("etl:1" in x.title for x in f)


def test_databricks_jobs_states(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    seen = []
    payload = {"runs": [
        {"run_id": 1, "run_name": "nightly", "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}, "start_time": 1000000, "end_time": 1030000,
         "run_duration": 30000, "run_page_url": "https://x/1"},
        {"run_id": 2, "run_name": "nightly", "state": {"life_cycle_state": "TERMINATED", "result_state": "TIMEDOUT"}, "start_time": 2000000, "end_time": 2900000},
        {"run_id": 3, "run_name": "etl", "state": {"life_cycle_state": "INTERNAL_ERROR"}, "start_time": 3000000, "end_time": 3001000},
        {"run_id": 4, "run_name": "etl", "state": {"life_cycle_state": "RUNNING"}, "start_time": 4000000, "end_time": 0},
        {"run_id": 5, "run_name": "etl", "state": {"life_cycle_state": "PENDING"}},
    ]}
    c = DatabricksJobsClient("adb-1.azuredatabricks.net", fetch=lambda url, h: (seen.append((url, h)) or payload))
    rs = {r.run_id: r for r in c.runs()}
    assert [rs[i].state for i in "12345"] == ["success", "failed", "failed", "running", "queued"]
    assert rs["1"].duration == 30 and rs["4"].end is None
    assert seen[0][0].startswith("https://adb-1.azuredatabricks.net/api/2.1/jobs/runs/list") and seen[0][1] == {"Authorization": "Bearer t"}
