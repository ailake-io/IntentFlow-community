"""AWS: Glue (job runs), Step Functions (executions) and Athena (queries), read-only, through `boto3` (optional).

Credentials: the default boto3 chain (`AWS_*` variables, profile, instance role). Region: `aws.region` / `AWS_REGION`.
Minimum permissions: glue:ListJobs, glue:GetJobRuns · states:ListStateMachines, states:ListExecutions ·
athena:ListQueryExecutions, athena:BatchGetQueryExecution (and, for the Athena warehouse, StartQueryExecution/GetQueryResults/GetTableMetadata + output S3).
"""
from __future__ import annotations

from ..i18n import tr

import os

from . import Run

GLUE = {"SUCCEEDED": "success", "FAILED": "failed", "ERROR": "failed", "TIMEOUT": "failed", "STOPPED": "failed", "EXPIRED": "failed",
        "RUNNING": "running", "STOPPING": "running", "STARTING": "queued", "WAITING": "queued"}
SFN = {"SUCCEEDED": "success", "FAILED": "failed", "TIMED_OUT": "failed", "ABORTED": "failed", "RUNNING": "running", "PENDING_REDRIVE": "queued"}
ATHENA = {"SUCCEEDED": "success", "FAILED": "failed", "CANCELLED": "failed", "RUNNING": "running", "QUEUED": "queued"}


def client(service: str, region: str | None = None, endpoint_url: str | None = None):
    try:
        import boto3  # type: ignore
    except ImportError as e:
        raise RuntimeError(tr("install `boto3` (pip install intentflow[aws])")) from e
    kw = {"region_name": region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")}
    if endpoint_url:
        kw["endpoint_url"] = endpoint_url
    return boto3.client(service, **kw)


def _ts(d) -> float | None:
    return d.timestamp() if d else None


class GlueClient:
    def __init__(self, region=None, endpoint_url=None, boto=None):
        self.c = boto or client("glue", region, endpoint_url)

    def runs(self, jobs: int = 50, per_job: int = 10) -> list[Run]:
        out = []
        names = []
        for page in self.c.get_paginator("list_jobs").paginate():
            names += page.get("JobNames", [])
            if len(names) >= jobs:
                break
        for n in names[:jobs]:
            for r in self.c.get_job_runs(JobName=n, MaxResults=per_job).get("JobRuns", []):
                state = GLUE.get(r.get("JobRunState", ""), "other")
                s, e = _ts(r.get("StartedOn")), _ts(r.get("CompletedOn"))
                done = state in ("success", "failed")
                out.append(Run("glue", n, r["Id"], state, s, e if done else None, (r.get("ExecutionTime") or (e - s if s and e else None)) if done else None,
                               f"https://console.aws.amazon.com/gluestudio/home#/job/{n}/run/{r['Id']}"))
        return out


class StepFunctionsClient:
    def __init__(self, region=None, endpoint_url=None, boto=None):
        self.c = boto or client("stepfunctions", region, endpoint_url)

    def runs(self, machines: int = 50, per_machine: int = 20) -> list[Run]:
        out = []
        for m in self.c.list_state_machines(maxResults=min(machines, 1000)).get("stateMachines", []):
            for x in self.c.list_executions(stateMachineArn=m["stateMachineArn"], maxResults=per_machine).get("executions", []):
                state = SFN.get(x.get("status", ""), "other")
                s, e = _ts(x.get("startDate")), _ts(x.get("stopDate"))
                done = state in ("success", "failed")
                out.append(Run("stepfunctions", m["name"], x["name"], state, s, e if done else None, (e - s) if done and s and e else None, x.get("executionArn", "")))
        return out


class AthenaClient:
    """Athena query executions as runs (per workgroup); `DataScannedInBytes` goes into `scanned_bytes` (cost)."""

    def __init__(self, region=None, endpoint_url=None, boto=None):
        self.c = boto or client("athena", region, endpoint_url)

    def runs(self, limit: int = 50) -> list[Run]:
        ids = self.c.list_query_executions(MaxResults=min(limit, 50)).get("QueryExecutionIds", [])
        out = []
        try:
            qes = self.c.batch_get_query_execution(QueryExecutionIds=ids).get("QueryExecutions", []) if ids else []
        except Exception:      # without athena:BatchGetQueryExecution (or an emulator without support): query one by one
            qes = [self.c.get_query_execution(QueryExecutionId=i)["QueryExecution"] for i in ids]
        for qe in qes:
            st = qe.get("Status", {})
            state = ATHENA.get(st.get("State", ""), "other")
            s, e = _ts(st.get("SubmissionDateTime")), _ts(st.get("CompletionDateTime"))
            ms = (qe.get("Statistics") or {}).get("EngineExecutionTimeInMillis")
            done = state in ("success", "failed")
            out.append(Run("athena", qe.get("WorkGroup", "primary"), qe["QueryExecutionId"], state, s, e if done else None,
                           (ms / 1000 if ms is not None else (e - s if s and e else None)) if done else None, ""))
        return out
