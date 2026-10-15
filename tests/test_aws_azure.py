"""AWS (Glue, Step Functions, Athena) contra o emulador `moto` (API real do boto3, servidor emulado) e Azure Data Factory com servidor local."""
import json, os, time

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from intentflow import runner, warehouses  # noqa: E402
from intentflow.orchestrators.aws import AthenaClient, GlueClient, StepFunctionsClient  # noqa: E402
from intentflow.orchestrators.more import AzureDataFactoryClient  # noqa: E402
from intentflow.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def aws():
    from moto import mock_aws
    with mock_aws():
        yield


def test_glue_job_runs(aws):
    g = boto3.client("glue")
    g.create_job(Name="etl-orders", Role="arn:aws:iam::123456789012:role/r", Command={"Name": "glueetl", "ScriptLocation": "s3://b/s.py"})
    g.start_job_run(JobName="etl-orders")
    rs = GlueClient(boto=g).runs()
    assert rs and rs[0].system == "glue" and rs[0].pipeline == "etl-orders" and rs[0].state in ("success", "running", "queued", "failed")


def test_step_functions_executions(aws):
    sf = boto3.client("stepfunctions")
    arn = sf.create_state_machine(name="pipeline", definition=json.dumps({"StartAt": "P", "States": {"P": {"Type": "Pass", "End": True}}}),
                                  roleArn="arn:aws:iam::123456789012:role/r")["stateMachineArn"]
    sf.start_execution(stateMachineArn=arn, name="run-1", input="{}")
    rs = StepFunctionsClient(boto=sf).runs()
    assert [r.pipeline for r in rs] == ["pipeline"] and rs[0].run_id == "run-1" and rs[0].state in ("success", "running", "failed")


def test_athena_executions_and_warehouse(aws):
    a = boto3.client("athena")
    for q in ("select 1", "select 2"):
        a.start_query_execution(QueryString=q, ResultConfiguration={"OutputLocation": "s3://bucket/out/"})
    rs = AthenaClient(boto=a).runs()
    assert len(rs) == 2 and all(r.system == "athena" and r.pipeline == "primary" for r in rs)
    from intentflow.warehouses.more import AthenaWarehouse
    wh = AthenaWarehouse(client=a, output="s3://bucket/out/", poll=0.01)
    assert wh.dialect == "athena" and wh.differs("a", "b") == "a is distinct from b"
    assert isinstance(wh.rows("select 1"), list)


def test_collect_runs_wires_aws_and_adf(aws, tmp_path, monkeypatch):
    g = boto3.client("glue")
    g.create_job(Name="j", Role="arn:aws:iam::123456789012:role/r", Command={"Name": "glueetl", "ScriptLocation": "s3://b/s.py"})
    g.start_job_run(JobName="j")
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("aws.region", "us-east-1")
    st.set_setting("aws.glue", True)
    r = runner.collect_runs(st)
    assert r["errors"] == [] and {x["system"] for x in st.runs()} == {"glue"}


def test_missing_boto3_is_a_clear_error(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "boto3":
            raise ImportError("no boto3")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    from intentflow.orchestrators import aws as _aws
    with pytest.raises(RuntimeError, match="boto3"):
        _aws.client("glue")


def test_adf_runs_paginate_and_map_states(monkeypatch):
    monkeypatch.setenv("AZURE_TOKEN", "tok")
    pages = [{"value": [{"runId": "a", "pipelineName": "copy_orders", "status": "Succeeded", "runStart": "2026-10-12T10:00:00Z",
                         "runEnd": "2026-10-12T10:02:00Z", "durationInMs": 120000}], "continuationToken": "next"},
             {"value": [{"runId": "b", "pipelineName": "copy_orders", "status": "Failed", "runStart": "2026-10-12T11:00:00Z", "runEnd": "2026-10-12T11:00:10Z"},
                        {"runId": "c", "pipelineName": "load", "status": "InProgress", "runStart": "2026-10-12T12:00:00Z"}]}]
    seen = []

    def post(url, body, headers):
        seen.append((url, body, headers))
        return pages[len(seen) - 1]
    rs = AzureDataFactoryClient("sub1/rg1/fac1", post=post).runs()
    assert [(r.run_id, r.state) for r in rs] == [("a", "success"), ("b", "failed"), ("c", "running")] and rs[0].duration == 120 and rs[1].duration == 10
    assert "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.DataFactory/factories/fac1/queryPipelineRuns" in seen[0][0]
    assert seen[1][1]["continuationToken"] == "next" and seen[0][2] == {"Authorization": "Bearer tok"}
    monkeypatch.delenv("AZURE_TOKEN")
    with pytest.raises(RuntimeError, match="AZURE_TOKEN"):
        AzureDataFactoryClient("s/r/f", post=post).runs()
