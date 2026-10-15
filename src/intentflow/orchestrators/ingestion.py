"""Ingestion tools (Airbyte, Fivetran) through REST, normalized into `Run`.

Airbyte: public API v1 (`/api/public/v1/jobs`, `/connections`). Auth: AIRBYTE_TOKEN, or AIRBYTE_CLIENT_ID/AIRBYTE_CLIENT_SECRET, or AIRBYTE_USER/AIRBYTE_PASSWORD.
Fivetran: `GET /v1/connectors` (Basic FIVETRAN_API_KEY:FIVETRAN_API_SECRET). Only the last sync per connector is available.
Verification: a real Airbyte OSS 1.15 (abctl); Fivetran only with a local HTTP server.
"""
from __future__ import annotations

from ..i18n import tr

import base64
import os
import re
import time

from . import Run, basic, http_json, ts

AIRBYTE_STATE = {"succeeded": "success", "failed": "failed", "incomplete": "failed", "cancelled": "failed", "running": "running", "pending": "queued"}


def _iso_seconds(d: str | None) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", d or "")
    if not m or not any(m.groups()):
        return None
    dd, h, mi, s = (float(x or 0) for x in m.groups())
    return dd * 86400 + h * 3600 + mi * 60 + s


class AirbyteClient:
    """Auth (from the environment): AIRBYTE_TOKEN (ready Bearer) | AIRBYTE_CLIENT_ID + AIRBYTE_CLIENT_SECRET (exchanged for a token at
    `/api/v1/applications/token`; it is what `abctl local credentials` shows) | AIRBYTE_USER/AIRBYTE_PASSWORD (Basic, older installs)."""

    def __init__(self, base_url: str, fetch=http_json, login=None):
        self.base, self.fetch, self._login, self._tok = base_url.rstrip("/"), fetch, login, None

    def _post_token(self, cid: str, secret: str) -> str:
        import json
        import urllib.request
        req = urllib.request.Request(f"{self.base}/api/v1/applications/token", method="POST",
                                     data=json.dumps({"client_id": cid, "client_secret": secret, "grant-type": "client_credentials"}).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())["access_token"]
        except Exception as e:
            raise RuntimeError(tr("Airbyte login failed at {url}: {error}", url=f"{self.base}/api/v1/applications/token", error=getattr(e, "code", type(e).__name__))) from None

    def _h(self) -> dict:
        tok = os.environ.get("AIRBYTE_TOKEN")
        if tok:
            return {"Authorization": f"Bearer {tok}"}
        cid, sec = os.environ.get("AIRBYTE_CLIENT_ID"), os.environ.get("AIRBYTE_CLIENT_SECRET")
        if cid and sec:
            if self._tok is None or time.time() - self._tok[1] > 120:       # tokens do Airbyte duram poucos minutos
                self._tok = ((self._login or self._post_token)(cid, sec), time.time())
            return {"Authorization": f"Bearer {self._tok[0]}"}
        return basic("AIRBYTE_USER", "AIRBYTE_PASSWORD")

    def _get(self, path: str):
        return self.fetch(f"{self.base}/api/public/v1{path}", self._h())

    def connections(self, limit: int = 100) -> dict[str, dict]:
        return {c["connectionId"]: c for c in self._get(f"/connections?limit={limit}").get("data", [])}

    def runs(self, limit: int = 100) -> list[Run]:
        names = self.connections()
        out = []
        for j in self._get(f"/jobs?limit={limit}&orderBy=createdAt%7CDESC").get("data", []):
            cid = j.get("connectionId", "")
            c = names.get(cid, {})
            s = ts(j.get("startTime"))
            state = AIRBYTE_STATE.get(str(j.get("status", "")).lower(), "other")
            e = ts(j.get("lastUpdatedAt")) if state in ("success", "failed") else None
            dur = _iso_seconds(j.get("duration")) or ((e - s) if s and e else None)
            label = c.get("name") or cid
            out.append(Run("airbyte", f"{label}:{j.get('jobType', 'sync')}", str(j.get("jobId")), state, s, e, dur,
                           f"{self.base}/connections/{cid}/job-history" if cid else self.base))
        return out


class FivetranClient:
    def __init__(self, base_url: str = "https://api.fivetran.com", fetch=http_json):
        self.base, self.fetch = base_url.rstrip("/"), fetch

    def _h(self) -> dict:
        k, s = os.environ.get("FIVETRAN_API_KEY"), os.environ.get("FIVETRAN_API_SECRET")
        return {"Authorization": "Basic " + base64.b64encode(f"{k}:{s}".encode()).decode()} if k and s else {}

    def runs(self, limit: int = 1000) -> list[Run]:
        out, cursor = [], None
        while len(out) < limit:
            r = self.fetch(f"{self.base}/v1/connectors?limit=1000" + (f"&cursor={cursor}" if cursor else ""), self._h()).get("data", {})
            for c in r.get("items", []):
                st = c.get("status") or {}
                ok, bad = ts(c.get("succeeded_at")), ts(c.get("failed_at"))
                if st.get("sync_state") == "syncing":
                    state, when = "running", ts(c.get("sync_started_at")) or ok
                elif bad and (not ok or bad > ok):
                    state, when = "failed", bad
                elif ok:
                    state, when = "success", ok
                else:
                    state, when = "other", None
                if c.get("paused"):
                    continue
                out.append(Run("fivetran", f"{c.get('schema') or c.get('id')}", f"{c.get('id')}@{int(when or 0)}", state, when, when if state != "running" else None,
                               None, f"https://fivetran.com/dashboard/connectors/{c.get('id')}"))
            cursor = r.get("next_cursor")
            if not cursor:
                break
        return out
