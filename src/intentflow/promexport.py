"""IntentFlow metrics in the Prometheus exposition format (`GET /metrics`, with a Bearer token like the rest of the API)."""
from __future__ import annotations

import re


def _esc(v) -> str:
    return re.sub(r'(["\\])', r"\\\1", str(v)).replace("\n", " ")


def render(store) -> str:
    lines: list[str] = []

    def metric(name: str, help_: str, kind: str, rows: list[tuple[dict, float]]):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        for lab, v in rows:
            body = ",".join(f'{k}="{_esc(x)}"' for k, x in lab.items())
            lines.append(f"{name}{{{body}}} {v:g}" if body else f"{name} {v:g}")

    sev: dict[str, int] = {}
    by_check: dict[str, int] = {}
    for f in store.findings("open"):
        sev[f["severity"]] = sev.get(f["severity"], 0) + 1
        c = f.get("check_id") or f.get("check") or "?"
        by_check[c] = by_check.get(c, 0) + 1
    metric("intentflow_findings_open", "Open findings by severity", "gauge",
           [({"severity": s}, n) for s, n in sorted(sev.items())] or [({"severity": "none"}, 0)])
    metric("intentflow_findings_open_by_check", "Open findings by check", "gauge", [({"check": c}, n) for c, n in sorted(by_check.items())])
    runs: dict[tuple, int] = {}
    for r in store.runs(1000):
        k = (r["system"], r["state"])
        runs[k] = runs.get(k, 0) + 1
    metric("intentflow_runs", "Recent collected runs by system and state", "gauge",
           [({"system": s, "state": st}, n) for (s, st), n in sorted(runs.items())])
    last = store.get_setting("state.last_check") or {}
    metric("intentflow_check_last_run_timestamp_seconds", "When the last check finished", "gauge", [({}, float(last.get("ts", 0)))])
    metric("intentflow_check_last_findings", "Findings of the last check", "gauge", [({}, float(last.get("findings", 0)))])
    return "\n".join(lines) + "\n"
