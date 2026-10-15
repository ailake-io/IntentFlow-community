"""Kestra through REST (`/api/v1`). Basic auth: KESTRA_USER / KESTRA_PASSWORD."""
from __future__ import annotations

from . import Run, basic, http_json, ts

STATE = {"SUCCESS": "success", "WARNING": "success", "FAILED": "failed", "KILLED": "failed", "RUNNING": "running",
         "CREATED": "queued", "QUEUED": "queued", "RETRYING": "running", "RESTARTED": "running", "PAUSED": "running"}


class KestraClient:
    def __init__(self, base_url: str, fetch=http_json):
        self.base, self.fetch = base_url.rstrip("/"), fetch

    def _get(self, path: str):
        return self.fetch(f"{self.base}/api/v1{path}", basic("KESTRA_USER", "KESTRA_PASSWORD"))

    def flows(self, size: int = 200) -> list[dict]:
        return self._get(f"/flows/search?size={size}").get("results", [])

    def runs(self, size: int = 100) -> list[Run]:
        out = []
        for e in self._get(f"/executions/search?size={size}&sort=state.startDate:desc").get("results", []):
            st = e.get("state", {})
            s, en = ts(st.get("startDate")), ts(st.get("endDate"))
            out.append(Run("kestra", f"{e.get('namespace')}.{e.get('flowId')}", e["id"], STATE.get(st.get("current", ""), "other"),
                           s, en, (en - s) if s and en else None, f"{self.base}/ui/executions/{e.get('namespace')}/{e.get('flowId')}/{e['id']}"))
        return out
