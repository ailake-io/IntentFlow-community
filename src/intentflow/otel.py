"""Exports IntentFlow metrics over OTLP/HTTP (JSON) to an OpenTelemetry Collector: `POST {endpoint}/v1/metrics`.
Same series as the Prometheus `/metrics`. Env: OTEL_EXPORTER_OTLP_ENDPOINT (default), OTEL_EXPORTER_OTLP_HEADERS (`k=v,k2=v2`).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from .i18n import tr


def _attrs(d: dict) -> list[dict]:
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items()]


def build_payload(store, now: float | None = None, service: str = "intentflow") -> dict:
    t = str(int((now or time.time()) * 1e9))
    metrics = []

    def gauge(name: str, desc: str, points: list[tuple[dict, float]]):
        metrics.append({"name": name, "description": desc, "gauge": {"dataPoints": [
            {"attributes": _attrs(a), "timeUnixNano": t, "asDouble": float(v)} for a, v in points]}})
    sev: dict[str, int] = {}
    chk: dict[str, int] = {}
    for f in store.findings("open"):
        sev[f["severity"]] = sev.get(f["severity"], 0) + 1
        c = f.get("check_id") or f.get("check") or "?"
        chk[c] = chk.get(c, 0) + 1
    gauge("intentflow.findings.open", "Open findings by severity", [({"severity": s}, n) for s, n in sorted(sev.items())] or [({"severity": "none"}, 0)])
    gauge("intentflow.findings.open.by_check", "Open findings by check", [({"check": c}, n) for c, n in sorted(chk.items())])
    runs: dict[tuple, int] = {}
    for r in store.runs(1000):
        runs[(r["system"], r["state"])] = runs.get((r["system"], r["state"]), 0) + 1
    gauge("intentflow.runs", "Recent runs by system and state", [({"system": s, "state": st}, n) for (s, st), n in sorted(runs.items())])
    last = store.get_setting("state.last_check") or {}
    gauge("intentflow.check.last_findings", "Findings of the last check", [({}, float(last.get("findings", 0)))])
    return {"resourceMetrics": [{"resource": {"attributes": _attrs({"service.name": service})},
                                 "scopeMetrics": [{"scope": {"name": "intentflow"}, "metrics": metrics}]}]}


def push(store, endpoint: str | None = None, timeout: float = 10.0) -> int:
    ep = (endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").rstrip("/")
    if not ep:
        raise RuntimeError(tr("pass --endpoint or set OTEL_EXPORTER_OTLP_ENDPOINT"))
    hdr = {"Content-Type": "application/json"}
    for kv in (os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") or "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            hdr[k.strip()] = v.strip()
    req = urllib.request.Request(f"{ep}/v1/metrics", data=json.dumps(build_payload(store)).encode(), method="POST", headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status
