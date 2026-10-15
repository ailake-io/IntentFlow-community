"""Dagster through GraphQL (`/graphql`): recent runs with state and timings. No credentials by default (OSS)."""
from __future__ import annotations

from ..i18n import tr

import json
import urllib.error
import urllib.request

from . import Run

STATE = {"SUCCESS": "success", "FAILURE": "failed", "CANCELED": "failed", "CANCELING": "failed", "STARTED": "running",
         "STARTING": "running", "QUEUED": "queued", "NOT_STARTED": "queued", "MANAGED": "queued"}
QUERY = """query Runs($limit: Int!) { runsOrError(limit: $limit) { __typename
  ... on Runs { results { runId jobName status startTime endTime } }
  ... on PythonError { message } } }"""


def _gql(url: str, query: str, variables: dict, timeout: float = 15.0):
    req = urllib.request.Request(url, data=json.dumps({"query": query, "variables": variables}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(tr("HTTP {code} at {url}", code=e.code, url=url)) from None
    except urllib.error.URLError as e:
        raise RuntimeError(tr("no connection to {url}: {reason}", url=url, reason=e.reason)) from None


class DagsterClient:
    def __init__(self, base_url: str, gql=_gql):
        self.url = base_url.rstrip("/") + "/graphql"
        self.base = base_url.rstrip("/")
        self.gql = gql

    def runs(self, limit: int = 100) -> list[Run]:
        d = self.gql(self.url, QUERY, {"limit": limit})
        r = (d.get("data") or {}).get("runsOrError") or {}
        if r.get("__typename") != "Runs":
            raise RuntimeError(f"Dagster: {r.get('message', d.get('errors', tr('unexpected response')))}")
        out = []
        for x in r["results"]:
            s, e = x.get("startTime"), x.get("endTime")
            out.append(Run("dagster", x["jobName"], x["runId"], STATE.get(x["status"], "other"), s, e,
                           (e - s) if s and e else None, f"{self.base}/runs/{x['runId']}"))
        return out
