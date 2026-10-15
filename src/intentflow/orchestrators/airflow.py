"""Airflow through REST. v1 (Airflow 2.x, `/api/v1`, basic auth) or v2 (Airflow 3.x, `/api/v2`, Bearer token).

Credentials through environment variables: AIRFLOW_USER/AIRFLOW_PASSWORD or AIRFLOW_TOKEN.
"""
from __future__ import annotations

from ..i18n import tr

import json
import os
import time
import urllib.request

from . import Run, basic, http_json, ts

STATE = {"success": "success", "failed": "failed", "running": "running", "queued": "queued", "up_for_retry": "running",
         "upstream_failed": "failed"}


class AirflowClient:
    def __init__(self, base_url: str, api: str = "v1", fetch=http_json, login=None):
        self.base, self.api, self.fetch = base_url.rstrip("/"), api, fetch
        self._login, self._jwt = login or self._post_token, None

    def _post_token(self, user: str, password: str) -> str:
        """Airflow 3 (simple auth manager / FAB): POST /auth/token exchanges user and password for a JWT."""
        req = urllib.request.Request(f"{self.base}/auth/token", method="POST", data=json.dumps({"username": user, "password": password}).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())["access_token"]
        except Exception as e:
            raise RuntimeError(tr("Airflow 3 login failed at {url}: {error}", url=f"{self.base}/auth/token", error=getattr(e, "code", type(e).__name__))) from None

    def _h(self) -> dict:
        tok = os.environ.get("AIRFLOW_TOKEN")
        if tok:
            return {"Authorization": f"Bearer {tok}"}
        user, pw = os.environ.get("AIRFLOW_USER"), os.environ.get("AIRFLOW_PASSWORD")
        if self.api == "v2" and user and pw:
            if not self._jwt or time.time() - self._jwt[1] > 1800:      # the default token lasts 24 h; renew every 30 min
                self._jwt = (self._login(user, pw), time.time())
            return {"Authorization": f"Bearer {self._jwt[0]}"}
        return basic("AIRFLOW_USER", "AIRFLOW_PASSWORD")

    def _get(self, path: str):
        return self.fetch(f"{self.base}/api/{self.api}{path}", self._h())

    def dags(self, limit: int = 100) -> list[dict]:
        return self._get(f"/dags?limit={limit}").get("dags", [])

    def runs(self, per_dag: int = 10, limit_dags: int = 100) -> list[Run]:
        out: list[Run] = []
        for d in self.dags(limit_dags):
            if d.get("is_paused"):
                continue
            did = d["dag_id"]
            for r in self._get(f"/dags/{did}/dagRuns?order_by=-start_date&limit={per_dag}").get("dag_runs", []):
                s, e = ts(r.get("start_date")), ts(r.get("end_date"))
                out.append(Run("airflow", did, r["dag_run_id"], STATE.get(r.get("state", ""), "other"), s, e,
                               (e - s) if s and e else None, f"{self.base}/dags/{did}/grid"))
        return out

    def pools(self) -> list[dict]:
        return self._get("/pools").get("pools", [])

    def health(self) -> dict:
        return self.fetch(f"{self.base}/api/{self.api}/health" if self.api == "v1" else f"{self.base}/api/v2/monitor/health", self._h())
