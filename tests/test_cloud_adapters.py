"""Cloud adapters with fake connections: they check the generated SQL and normalization, NOT real accounts."""
from intentflow import monitoring
from intentflow.warehouses.cloud import BigQueryWarehouse, DatabricksWarehouse, SnowflakeWarehouse


class Cur:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def execute(self, sql):
        self.log.append(sql)

    def fetchall(self):
        return self.rows

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Conn:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def cursor(self):
        return Cur(self.rows, self.log)


def test_snowflake_spill_report():
    c = Conn([("01a", "select * from big", 5_000_000_000, 7_000_000_000)])
    r = SnowflakeWarehouse(c).spill_report(12)
    assert r[0]["spilled_bytes"] == 12_000_000_000 and r[0]["source"] == "snowflake"
    assert "dateadd(hour, -12" in c.log[0] and "bytes_spilled_to_remote_storage" in c.log[0]
    s = monitoring.warehouse_spill(SnowflakeWarehouse(c))
    assert s[0].metric == "spilled_bytes" and s[0].scope == "snowflake:01a"


def test_databricks_spill_report():
    c = Conn([("st-1", "select ...", 2_000_000_000)])
    r = DatabricksWarehouse(c).spill_report()
    assert r[0]["spilled_bytes"] == 2_000_000_000
    assert "system.query.history" in c.log[0]


def test_bigquery_resources_exceeded_report():
    class Row(dict):
        def values(self):
            return list(super().values())

    class Job:
        def result(self):
            return [Row(job_id="j1", q="select 1", b=10)]

    class Client:
        def query(self, sql):
            self.sql = sql
            return Job()

    cl = Client()
    r = BigQueryWarehouse(cl).spill_report()
    assert r[0]["scope"] == "j1" and "resourcesExceeded" in cl.sql


def test_missing_credentials_fail_clearly(monkeypatch):
    import pytest
    for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises((RuntimeError, ImportError)):
        SnowflakeWarehouse()


def test_docker_sampler_parses_stats_and_oom():
    import types
    def run(cmd, **kw):
        if cmd[:2] == ["docker", "stats"]:
            return types.SimpleNamespace(stdout='{"Name":"worker","MemUsage":"512MiB / 1GiB"}\n')
        if cmd[:2] == ["docker", "ps"]:
            return types.SimpleNamespace(stdout="abc\n")
        return types.SimpleNamespace(stdout="/worker true\n/db false\n")
    s = monitoring.docker_containers(now=1.0, run=run)
    assert any(x.metric == "memory_bytes" and abs(x.value / x.limit - 0.5) < 1e-6 for x in s)
    assert any(x.metric == "oom_killed" and x.scope == "worker" for x in s)
