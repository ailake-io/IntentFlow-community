"""More run sources through REST, normalized into `Run`: Kafka Connect/Debezium, Apache Flink, Luigi, Rundeck, MLflow.
Each client accepts `fetch(url, headers)` (and `post` where the API requires POST) for tests.
"""
from __future__ import annotations

from ..i18n import tr

import json
import os
import urllib.request

from . import Run, http_json, ts


def _post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 15.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(tr("HTTP {code} at {url}", code=e.code, url=url.split("?")[0])) from None
    except urllib.error.URLError as e:
        raise RuntimeError(tr("no connection to {url}: {reason}", url=url.split("?")[0], reason=e.reason)) from None


class KafkaConnectClient:
    """`GET /connectors?expand=status`: a FAILED connector or one with a FAILED task counts as a failure (CDC stopped)."""

    def __init__(self, base_url: str, fetch=http_json):
        self.base, self.fetch = base_url.rstrip("/"), fetch

    def runs(self) -> list[Run]:
        import time
        now = time.time()
        out = []
        for name, info in (self.fetch(f"{self.base}/connectors?expand=status", {}) or {}).items():
            st = info.get("status") or {}
            states = [(st.get("connector") or {}).get("state", "")] + [t.get("state", "") for t in st.get("tasks", [])]
            if any(s == "FAILED" for s in states):
                state = "failed"
            elif all(s == "RUNNING" for s in states if s):
                state = "running"
            else:
                state = "other"              # PAUSED/UNASSIGNED/RESTARTING
            out.append(Run("kafka_connect", name, f"{name}:{state}", state, now, now if state == "failed" else None, None,
                           f"{self.base}/connectors/{name}/status"))
        return out


class FlinkClient:
    STATE = {"FINISHED": "success", "FAILED": "failed", "CANCELED": "failed", "CANCELLING": "failed", "SUSPENDED": "failed", "FAILING": "failed",
             "RUNNING": "running", "RESTARTING": "queued", "CREATED": "queued", "RECONCILING": "queued", "INITIALIZING": "queued"}

    def __init__(self, base_url: str, fetch=http_json):
        self.base, self.fetch = base_url.rstrip("/"), fetch

    def runs(self) -> list[Run]:
        out = []
        for j in (self.fetch(f"{self.base}/jobs/overview", {}) or {}).get("jobs", []):
            state = self.STATE.get(str(j.get("state", "")).upper(), "other")
            s = (j.get("start-time") or 0) / 1000 or None
            e = (j.get("end-time") or 0) / 1000 if (j.get("end-time") or 0) > 0 else None
            done = state in ("success", "failed")
            out.append(Run("flink", j.get("name", "?"), j.get("jid", ""), state, s, e if done else None,
                           ((j.get("duration") or 0) / 1000 or ((e - s) if e and s else None)) if done else None, f"{self.base}/#/job/{j.get('jid', '')}/overview"))
        return out


class LuigiClient:
    STATE = {"DONE": "success", "FAILED": "failed", "RUNNING": "running", "PENDING": "queued", "DISABLED": "failed", "UPSTREAM_FAILED": "failed",
             "UPSTREAM_DISABLED": "failed"}

    def __init__(self, base_url: str, fetch=http_json):
        self.base, self.fetch = base_url.rstrip("/"), fetch

    def runs(self) -> list[Run]:
        import urllib.parse
        q = urllib.parse.quote(json.dumps({"status": "", "upstream_status": "", "search": "", "filter_deps": False, "limit": 500}))
        tasks = (self.fetch(f"{self.base}/api/task_list?data={q}", {}) or {}).get("response", {})
        out = []
        for tid, t in tasks.items():
            state = self.STATE.get(str(t.get("status", "")).upper(), "other")
            e = float(t["last_updated"]) if t.get("last_updated") and state in ("success", "failed") else None
            s = float(t["start_time"]) if t.get("start_time") else None
            out.append(Run("luigi", t.get("name", tid), tid, state, s, e, max(0.0, e - s) if e and s else None, f"{self.base}/#/{tid}"))
        return out


class RundeckClient:
    STATE = {"succeeded": "success", "failed": "failed", "aborted": "failed", "timedout": "failed", "failed-with-retry": "failed", "running": "running", "scheduled": "queued"}

    def __init__(self, base_url: str, project: str, fetch=http_json, api: str = "41"):
        self.base, self.project, self.fetch, self.api = base_url.rstrip("/"), project, fetch, api

    def runs(self, limit: int = 100) -> list[Run]:
        tok = os.environ.get("RUNDECK_TOKEN")
        h = {"X-Rundeck-Auth-Token": tok, "Accept": "application/json"} if tok else {"Accept": "application/json"}
        out = []
        for x in (self.fetch(f"{self.base}/api/{self.api}/project/{self.project}/executions?max={limit}", h) or {}).get("executions", []):
            state = self.STATE.get(str(x.get("status", "")).lower(), "other")
            s = ((x.get("date-started") or {}).get("unixtime") or 0) / 1000 or None
            e = ((x.get("date-ended") or {}).get("unixtime") or 0) / 1000 or None
            done = state in ("success", "failed")
            out.append(Run("rundeck", (x.get("job") or {}).get("name") or x.get("description", "?"), str(x.get("id")), state, s, e if done else None,
                           (e - s) if done and s and e else None, x.get("permalink", "")))
        return out


class MLflowClient:
    STATE = {"FINISHED": "success", "FAILED": "failed", "KILLED": "failed", "RUNNING": "running", "SCHEDULED": "queued"}

    def __init__(self, base_url: str, fetch=http_json, post=_post_json):
        self.base, self.fetch, self.post = base_url.rstrip("/"), fetch, post

    def _h(self) -> dict:
        t = os.environ.get("MLFLOW_TRACKING_TOKEN")
        return {"Authorization": f"Bearer {t}"} if t else {}

    def runs(self, limit: int = 100) -> list[Run]:
        exps = self.post(f"{self.base}/api/2.0/mlflow/experiments/search", {"max_results": 100}, self._h()) or {}
        names = {e["experiment_id"]: e.get("name", e["experiment_id"]) for e in exps.get("experiments", [])}
        if not names:
            return []
        rs = self.post(f"{self.base}/api/2.0/mlflow/runs/search", {"experiment_ids": list(names), "max_results": limit, "order_by": ["attributes.start_time DESC"]}, self._h()) or {}
        out = []
        for r in rs.get("runs", []):
            i = r.get("info", {})
            state = self.STATE.get(str(i.get("status", "")).upper(), "other")
            s = (int(i.get("start_time") or 0)) / 1000 or None
            e = (int(i.get("end_time") or 0)) / 1000 or None
            done = state in ("success", "failed")
            out.append(Run("mlflow", names.get(i.get("experiment_id"), i.get("experiment_id", "?")), i.get("run_id", ""), state, s, e if done else None,
                           (e - s) if done and s and e else None, f"{self.base}/#/experiments/{i.get('experiment_id')}/runs/{i.get('run_id')}"))
        return out
