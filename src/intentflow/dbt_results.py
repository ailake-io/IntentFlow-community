"""Result of the last dbt run (`target/run_results.json`): tests, failures, warnings, timings and skipped nodes."""
from __future__ import annotations

import json
import statistics
from pathlib import Path


def load(target: str | Path) -> dict | None:
    f = Path(target) / "run_results.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
    except ValueError:
        return None
    meta = d.get("metadata", {})
    return {
        "invocation_id": meta.get("invocation_id"), "generated_at": meta.get("generated_at"),
        "command": (d.get("args") or {}).get("which"), "elapsed": d.get("elapsed_time"),
        "results": [{"unique_id": r["unique_id"], "status": r.get("status"), "time": r.get("execution_time") or 0.0,
                     "message": r.get("message"), "failures": r.get("failures")} for r in d.get("results", [])],
    }


def summary(res: dict, project=None) -> dict:
    out = {"invocation_id": res["invocation_id"], "command": res["command"], "elapsed": res["elapsed"],
           "counts": {}, "slowest": [], "failing": []}
    for r in res["results"]:
        out["counts"][r["status"]] = out["counts"].get(r["status"], 0) + 1
    name = lambda u: (project.nodes[u].name if project and u in project.nodes else u.split(".")[-1])   # noqa: E731
    out["slowest"] = [{"name": name(r["unique_id"]), "time": round(r["time"], 2)}
                      for r in sorted(res["results"], key=lambda r: -r["time"]) if not r["unique_id"].startswith("test.")][:5]
    for r in res["results"]:
        if r["status"] in ("fail", "error", "warn"):
            label = r["unique_id"].split(".")[-2] if r["unique_id"].startswith("test.") else r["unique_id"].split(".")[-1]
            out["failing"].append({"name": label, "status": r["status"], "failures": r["failures"], "message": (r["message"] or "")[:160]})
    return out


def record(store, res: dict) -> bool:
    """Record per-node timings in the history (once per invocation). Returns False if already recorded."""
    if store.get_setting("state.dbt_invocation") == res["invocation_id"]:
        return False
    for r in res["results"]:
        if not r["unique_id"].startswith("test.") and r["status"] in ("success", "pass"):
            store.add_history(f"dbt_time:{r['unique_id']}", __import__("time").time(), r["time"])
    store.set_setting("state.dbt_invocation", res["invocation_id"])
    return True


def median_history(store, uid: str, last: int = 20) -> tuple[float | None, int]:
    h = [v for _, v in store.history(f"dbt_time:{uid}")][-last:]
    return (statistics.median(h) if h else None), len(h)
