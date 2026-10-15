"""Argo Workflows and Mage through REST, normalized into `Run`. Verification: local HTTP servers with the documented format
(no Kubernetes cluster or Mage runs here).

Argo : `GET /api/v1/workflows/{ns}` (Bearer ARGO_TOKEN; `ARGO_NAMESPACE` or argo.namespace; default `argo`)
Mage : `GET /api/pipeline_runs` (optional MAGE_API_KEY, sent as `api_key`)
"""
from __future__ import annotations

from ..i18n import tr

import os
import urllib.parse

from . import Run, http_json, ts
from .extra import _post_json

ARGO_PHASE = {"Succeeded": "success", "Failed": "failed", "Error": "failed", "Running": "running", "Pending": "queued"}
MAGE_STATUS = {"completed": "success", "failed": "failed", "cancelled": "failed", "running": "running", "initial": "queued"}


class ArgoClient:
    def __init__(self, base_url: str, namespace: str | None = None, fetch=http_json):
        self.base, self.ns, self.fetch = base_url.rstrip("/"), namespace or os.environ.get("ARGO_NAMESPACE", "argo"), fetch

    def _h(self) -> dict:
        t = os.environ.get("ARGO_TOKEN")
        return {"Authorization": f"Bearer {t}" if not t.startswith("Bearer ") else t} if t else {}

    def runs(self, limit: int = 100) -> list[Run]:
        data = self.fetch(f"{self.base}/api/v1/workflows/{urllib.parse.quote(self.ns)}?listOptions.limit={limit}", self._h()) or {}
        out = []
        for w in data.get("items") or []:
            md, st = w.get("metadata", {}), w.get("status", {})
            labels = md.get("labels") or {}
            name = labels.get("workflows.argoproj.io/workflow-template") or labels.get("workflows.argoproj.io/cron-workflow") \
                or (md.get("generateName") or md.get("name", "?")).rstrip("-")
            s, e = ts(st.get("startedAt")), ts(st.get("finishedAt"))
            state = ARGO_PHASE.get(st.get("phase", ""), "other")
            out.append(Run("argo", name, md.get("name", ""), state, s, e if state in ("success", "failed") else None,
                           (e - s) if s and e and state in ("success", "failed") else None, f"{self.base}/workflows/{self.ns}/{md.get('name', '')}"))
        return out


class MageClient:
    """Mage OSS requires `api_key` + a session token. Credentials (from the environment): MAGE_TOKEN, or MAGE_USER/MAGE_PASSWORD (login at
    `/api/sessions`); MAGE_API_KEY overrides Mage's default development key (a public value from its docs)."""
    DEFAULT_KEY = "zkWlN0PkIKSN0C11CfUHUj84OT5XOJ6tDZ6bDRO2"

    def __init__(self, base_url: str, fetch=http_json, login=None):
        self.base, self.fetch, self._login, self._tok = base_url.rstrip("/"), fetch, login, None

    def _key(self) -> str:
        return os.environ.get("MAGE_API_KEY") or self.DEFAULT_KEY

    def _token(self) -> str | None:
        t = os.environ.get("MAGE_TOKEN")
        if t:
            return t
        user, pw = os.environ.get("MAGE_USER"), os.environ.get("MAGE_PASSWORD")
        if not (user and pw):
            return None
        if self._tok is None:
            self._tok = (self._login or self._session)(user, pw)
        return self._tok

    def _session(self, user: str, pw: str) -> str:
        import base64
        import json
        import urllib.error
        import urllib.request
        body = json.dumps({"session": {"email": user, "password": pw}, "api_key": self._key()}).encode()
        req = urllib.request.Request(f"{self.base}/api/sessions", data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                jwt = json.loads(r.read().decode())["session"]["token"]
            seg = jwt.split(".")[1]
            return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))["token"]     # internal session token
        except Exception as e:
            raise RuntimeError(tr("Mage login failed at {url}: {error}", url=f"{self.base}/api/sessions", error=getattr(e, "code", type(e).__name__))) from None

    def runs(self, limit: int = 100) -> list[Run]:
        tok = self._token()
        data = self.fetch(f"{self.base}/api/pipeline_runs?_limit={limit}&api_key={urllib.parse.quote(self._key())}",
                          {"Authorization": f"Bearer {tok}"} if tok else {}) or {}
        if isinstance(data.get("error"), dict):
            raise RuntimeError(tr("Mage refused the query: {error} (set MAGE_USER/MAGE_PASSWORD or MAGE_TOKEN)", error=data["error"].get("message", "")[:120]))
        out = []
        for r in data.get("pipeline_runs") or []:
            s = ts(r.get("started_at") or r.get("execution_date"))
            state = MAGE_STATUS.get(str(r.get("status", "")).lower(), "other")
            e = ts(r.get("completed_at") or (r.get("updated_at") if state in ("success", "failed") else None))   # failed runs have no completed_at
            # `updated_at` comes truncated to seconds: the end may fall before the fractional start
            out.append(Run("mage", r.get("pipeline_uuid", "?"), str(r.get("id")), state, s, e if state in ("success", "failed") else None,
                           max(0.0, e - s) if s and e and state in ("success", "failed") else None, f"{self.base}/pipelines/{r.get('pipeline_uuid', '')}/runs"))
        return out


class AzureDataFactoryClient:
    """Azure Data Factory: `POST .../factories/{f}/queryPipelineRuns` (last 24 h). Token: AZURE_TOKEN
    (`az account get-access-token --resource https://management.azure.com --query accessToken -o tsv`).
    Permission: the *Reader* role (or `Microsoft.DataFactory/factories/pipelineruns/read`) on the factory."""
    STATE = {"Succeeded": "success", "Failed": "failed", "Cancelled": "failed", "Canceling": "failed", "InProgress": "running", "Queued": "queued"}

    def __init__(self, resource: str, base: str = "https://management.azure.com", post=_post_json):
        """`resource`: `subscriptionId/resourceGroup/factoryName`."""
        sub, rg, fac = resource.strip("/").split("/")[-3:]
        self.url = (f"{base.rstrip('/')}/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.DataFactory/factories/{fac}"
                    f"/queryPipelineRuns?api-version=2018-06-01")
        self.post = post
        self.portal = f"https://adf.azure.com/en/monitoring/pipelineruns/"

    def runs(self, hours: int = 24) -> list[Run]:
        import time
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        tok = os.environ.get("AZURE_TOKEN")
        if not tok:
            raise RuntimeError(tr("AZURE_TOKEN is not set"))
        out, cont = [], None
        for _ in range(10):
            body = {"lastUpdatedAfter": (now - timedelta(hours=hours)).isoformat(), "lastUpdatedBefore": now.isoformat(),
                    **({"continuationToken": cont} if cont else {})}
            d = self.post(self.url, body, {"Authorization": f"Bearer {tok}"}) or {}
            for r in d.get("value", []):
                state = self.STATE.get(r.get("status", ""), "other")
                s, e = ts(r.get("runStart")), ts(r.get("runEnd"))
                done = state in ("success", "failed")
                dur = (r.get("durationInMs") / 1000) if r.get("durationInMs") is not None else ((e - s) if s and e else None)
                out.append(Run("adf", r.get("pipelineName", "?"), r.get("runId", ""), state, s, e if done else None, dur if done else None,
                               self.portal + str(r.get("runId", ""))))
            cont = d.get("continuationToken")
            if not cont:
                break
        return out
