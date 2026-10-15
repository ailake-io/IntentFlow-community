"""Prefect (2.x/3.x) via REST: `POST /api/flow_runs/filter` e `POST /api/flows/filter`. Token opcional: PREFECT_API_KEY."""
from __future__ import annotations

from ..i18n import tr

import json
import os
import urllib.error
import urllib.request

from . import Run, ts

STATE = {"COMPLETED": "success", "FAILED": "failed", "CRASHED": "failed", "CANCELLED": "failed", "CANCELLING": "failed",
         "RUNNING": "running", "PENDING": "queued", "SCHEDULED": "queued", "PAUSED": "running"}


def _post(url: str, body: dict, headers: dict, timeout: float = 15.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(tr("HTTP {code} at {url}", code=e.code, url=url)) from None
    except urllib.error.URLError as e:
        raise RuntimeError(tr("no connection to {url}: {reason}", url=url, reason=e.reason)) from None


class PrefectClient:
    def __init__(self, base_url: str, post=_post):
        # aceita http://host:4200 ou http://host:4200/api
        self.base = base_url.rstrip("/").removesuffix("/api")
        self.post = post

    def _h(self) -> dict:
        k = os.environ.get("PREFECT_API_KEY")
        return {"Authorization": f"Bearer {k}"} if k else {}

    def runs(self, limit: int = 100) -> list[Run]:
        rs = self.post(f"{self.base}/api/flow_runs/filter", {"limit": limit, "sort": "START_TIME_DESC"}, self._h())
        ids = sorted({r["flow_id"] for r in rs if r.get("flow_id")})
        names = {}
        if ids:
            fl = self.post(f"{self.base}/api/flows/filter", {"flows": {"id": {"any_": ids}}, "limit": len(ids)}, self._h())
            names = {f["id"]: f["name"] for f in fl}
        out = []
        for r in rs:
            st = (r.get("state") or {}).get("type", "")
            s, e = ts(r.get("start_time")), ts(r.get("end_time"))
            dur = r.get("total_run_time") or ((e - s) if s and e else None)
            out.append(Run("prefect", names.get(r.get("flow_id"), r.get("name", "?")), r["id"], STATE.get(st, "other"), s, e, dur,
                           f"{self.base}/runs/flow-run/{r['id']}"))
        return out
