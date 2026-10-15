"""Spark (UI/History Server REST `/api/v1`) and Databricks Jobs (`/api/2.1/jobs/runs/list`).

Spark: applications become `Run`s; executors and stages become memory/spill samples (for the existing cluster checks).
Databricks Jobs: Bearer DATABRICKS_TOKEN. Verification: a real Spark 3.5 in a container; Databricks only with a local HTTP server.
"""
from __future__ import annotations

import os
import time

from . import Run, http_json, ts

DB_RESULT = {"SUCCESS": "success", "FAILED": "failed", "TIMEDOUT": "failed", "CANCELED": "failed", "MAXIMUM_CONCURRENT_RUNS_REACHED": "failed",
             "UPSTREAM_FAILED": "failed", "EXCLUDED": "other", "SUCCESS_WITH_FAILURES": "failed"}
OOM_HINTS = ("outofmemory", "out of memory", "exceeding memory limits", "killed by yarn", "oomkilled", "exit code 137")


_SIZE = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def _size(v: str | None) -> float | None:
    """'1g' / '512m' / '2048' (MiB in Spark when there is no unit) → bytes."""
    import re
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgGtT])?[bB]?\s*", str(v or ""))
    if not m:
        return None
    return float(m.group(1)) * (_SIZE[m.group(2).lower()] if m.group(2) else 1024**2)


class SparkClient:
    def __init__(self, base_url: str, fetch=http_json):
        self.base, self.fetch = base_url.rstrip("/"), fetch

    def _get(self, path: str):
        return self.fetch(f"{self.base}/api/v1{path}", {})

    def apps(self) -> list[dict]:
        return self._get("/applications") or []

    def runs(self) -> list[Run]:
        out = []
        for a in self.apps():
            for att in a.get("attempts", [{}]):
                s = (att.get("startTimeEpoch") or 0) / 1000 or ts(att.get("startTime"))
                e = (att.get("endTimeEpoch") or 0) / 1000 or ts(att.get("endTime"))
                done = bool(att.get("completed"))
                state = "running"
                if done:
                    try:
                        jobs = self._get(f"/applications/{a['id']}/jobs")
                    except RuntimeError:
                        jobs = []
                    state = "failed" if any(str(j.get("status")).upper() == "FAILED" for j in jobs) else "success"
                rid = a["id"] + (f"/{att['attemptId']}" if att.get("attemptId") else "")
                out.append(Run("spark", a.get("name", a["id"]), rid, state, s or None, (e or None) if done else None,
                               (e - s) if done and s and e else None, f"{self.base}/history/{a['id']}"))
        return out

    def samples(self, now: float | None = None):
        from ..monitoring import Sample
        now = now or time.time()
        out = []
        for a in self.apps():
            name = a.get("name", a["id"])
            try:
                props = {k: v for k, v in (self._get(f"/applications/{a['id']}/environment").get("sparkProperties") or [])}
            except (RuntimeError, AttributeError, ValueError):
                props = {}
            for ex in self._get(f"/applications/{a['id']}/allexecutors"):
                eid = ex.get("id", "?")
                maxm = float(ex.get("maxMemory") or 0)
                heap = float((ex.get("peakMemoryMetrics") or {}).get("JVMHeapMemory") or 0)
                limit = _size(props.get("spark.driver.memory" if eid == "driver" else "spark.executor.memory", None))
                if heap and limit:            # heap real do JVM (pico) contra o limite configurado
                    out.append(Sample(now, "spark", f"{name}:{eid}", "memory_bytes", heap, limit))
                else:                          # storage memory only (not the heap): better than nothing
                    out.append(Sample(now, "spark", f"{name}:{eid}", "memory_bytes", float(ex.get("memoryUsed") or 0), maxm or None))
                reason = str(ex.get("removeReason") or "").lower()
                if reason and any(h in reason for h in OOM_HINTS):
                    out.append(Sample(now, "spark", f"{name}:{eid}", "oom_killed", 1.0))
            spilled = 0.0
            for st in self._get(f"/applications/{a['id']}/stages"):
                spilled += float(st.get("memoryBytesSpilled") or 0) + float(st.get("diskBytesSpilled") or 0)
            if spilled:
                out.append(Sample(now, "spark", name, "spilled_bytes", spilled))
        return out


class DatabricksJobsClient:
    def __init__(self, host: str | None = None, fetch=http_json):
        self.host = (host or os.environ.get("DATABRICKS_HOST", "")).rstrip("/")
        if self.host and "://" not in self.host:
            self.host = "https://" + self.host
        self.fetch = fetch

    def _h(self) -> dict:
        t = os.environ.get("DATABRICKS_TOKEN")
        return {"Authorization": f"Bearer {t}"} if t else {}

    def runs(self, limit: int = 100) -> list[Run]:
        out = []
        for r in self.fetch(f"{self.host}/api/2.1/jobs/runs/list?limit={min(limit, 25)}&expand_tasks=false", self._h()).get("runs", []):
            st = r.get("state") or {}
            life = st.get("life_cycle_state", "")
            if life in ("TERMINATED", "SKIPPED"):
                state = DB_RESULT.get(st.get("result_state", ""), "other") if life == "TERMINATED" else "other"
            elif life == "INTERNAL_ERROR":
                state = "failed"
            elif life in ("PENDING", "QUEUED", "BLOCKED", "WAITING_FOR_RETRY"):
                state = "queued"
            else:
                state = "running"
            s = (r.get("start_time") or 0) / 1000 or None
            e = (r.get("end_time") or 0) / 1000 or None
            dur = (r.get("run_duration") or 0) / 1000 or ((e - s) if s and e else None)
            out.append(Run("databricks", r.get("run_name") or str(r.get("job_id")), str(r.get("run_id")), state, s,
                           e if state in ("success", "failed") else None, dur if state in ("success", "failed") else None, r.get("run_page_url", "")))
        return out
