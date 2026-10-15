"""Read-only collectors for orchestrators. They normalize runs into `Run`."""
from __future__ import annotations

from ..i18n import tr

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Run:
    system: str            # airflow | kestra
    pipeline: str          # dag_id | namespace.flow
    run_id: str
    state: str             # success | failed | running | queued | other
    start: float | None
    end: float | None
    duration: float | None
    url: str = ""


def ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:                 # no time zone (e.g. Mage) = UTC; `timestamp()` of a naive datetime would use the local zone
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def http_json(url: str, headers: dict | None = None, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(tr("HTTP {code} at {url}", code=e.code, url=url.split("?")[0])) from None
    except urllib.error.URLError as e:
        raise RuntimeError(tr("no connection to {url}: {reason}", url=url.split("?")[0], reason=e.reason)) from None


def basic(user_env: str, pass_env: str) -> dict:
    u, p = os.environ.get(user_env), os.environ.get(pass_env)
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()} if u and p else {}
