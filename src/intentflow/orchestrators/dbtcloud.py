"""dbt Cloud (API v2): job runs and the artifacts of the last successful run.

Variables: DBT_CLOUD_TOKEN (service token, read-only). Config: `dbt_cloud.host` (default cloud.getdbt.com; use your
tenant's host), `dbt_cloud.account_id`, `dbt_cloud.job_id` (jobs that produce manifest/catalog/run_results).
Verification: a local HTTP server with the documented format; never against the real dbt Cloud.
"""
from __future__ import annotations

from ..i18n import tr

import json
import os
import re
from pathlib import Path

from . import Run, http_json, ts

STATUS = {1: "queued", 2: "queued", 3: "running", 10: "success", 20: "failed", 30: "failed"}
ARTIFACTS = ("manifest.json", "catalog.json", "run_results.json")


def _hms(s) -> float | None:
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:\.\d+)?", str(s or ""))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


class DbtCloudClient:
    def __init__(self, host: str | None, account_id: str | int, fetch=http_json):
        h = (host or "cloud.getdbt.com").rstrip("/")
        self.base = h if "://" in h else f"https://{h}"
        self.account, self.fetch = str(account_id), fetch

    def _h(self) -> dict:
        t = os.environ.get("DBT_CLOUD_TOKEN")
        return {"Authorization": f"Token {t}"} if t else {}

    def _get(self, path: str):
        return self.fetch(f"{self.base}/api/v2/accounts/{self.account}{path}", self._h())

    def runs(self, limit: int = 100) -> list[Run]:
        data = self._get(f"/runs/?order_by=-id&limit={limit}&include_related=%5B%22job%22%5D") or {}
        out = []
        for r in data.get("data") or []:
            state = STATUS.get(r.get("status"), "other")
            s, e = ts(r.get("started_at")), ts(r.get("finished_at"))
            dur = r.get("run_duration") if isinstance(r.get("run_duration"), (int, float)) else _hms(r.get("duration"))
            name = (r.get("job") or {}).get("name") or f"job-{r.get('job_definition_id')}"
            out.append(Run("dbt_cloud", name, str(r.get("id")), state, s, e if state in ("success", "failed") else None,
                           dur if state in ("success", "failed") else None, r.get("href", "")))
        return out

    def pull_artifacts(self, job_id: str | int, dest: str | Path, run_id: str | int | None = None) -> dict:
        """Download manifest/catalog/run_results of the job's last successful run (or of `run_id`). `catalog.json` is optional."""
        d = Path(dest)
        d.mkdir(parents=True, exist_ok=True)
        got, missing = [], []
        for name in ARTIFACTS:
            path = f"/runs/{run_id}/artifacts/{name}" if run_id else f"/jobs/{job_id}/artifacts/{name}"
            try:
                doc = self._get(path)
            except RuntimeError as e:
                if name == "manifest.json":
                    raise RuntimeError(tr("manifest.json unavailable for job {job}: {error} (the job must produce artifacts)", job=job_id, error=e)) from None
                missing.append(name)
                continue
            (d / name).write_text(json.dumps(doc))
            got.append(name)
        return {"dir": str(d), "files": got, "missing": missing}
