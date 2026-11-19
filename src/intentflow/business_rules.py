"""Business rules declared as native dbt tests tagged `business_rule`.

SQL stays in dbt, executed with its normal permissions. IntentFlow reads results,
tracks a bounded run history and uses model dependencies to explain impact.
"""
from __future__ import annotations

import time
from datetime import datetime

from .i18n import tr

TAG = "business_rule"
HISTORY_LIMIT = 20


def _key(uid):
    return f"state.business_rule.{uid}"


def rule_owner(project, definition):
    explicit = definition["meta"].get("owner")
    if explicit:
        return explicit.strip() if isinstance(explicit, str) and explicit.strip() else None
    values = [project.nodes[u].meta.get("owner") or project.nodes[u].meta.get("team") for u in definition["models"]]
    owners = {value.strip() for value in values if isinstance(value, str) and value.strip()}
    return next(iter(owners)) if len(owners) == 1 else None


def record(store, project, results):
    if not results or not results.get("invocation_id"):
        return
    by_id = {r["unique_id"]: r for r in results["results"]}
    for uid, definition in project.test_definitions.items():
        if TAG not in definition["tags"] or uid not in by_id:
            continue
        history = store.get_setting(_key(uid), [])
        if any(r["invocation_id"] == results["invocation_id"] for r in history):
            continue
        r = by_id[uid]
        history.append({"invocation_id": results["invocation_id"], "generated_at": results.get("generated_at"),
                        "status": r["status"], "failures": r.get("failures"), "seconds": r.get("time"),
                        "recorded_at": time.time()})
        store.set_setting(_key(uid), history[-HISTORY_LIMIT:])


def build(project, results, store=None, *, tag=TAG, max_age_hours=24, now=None):
    now = time.time() if now is None else now
    by_id = {r["unique_id"]: r for r in results["results"]} if results else {}
    fresh = False
    if results and results.get("generated_at"):
        try:
            stamp = datetime.fromisoformat(results["generated_at"].replace("Z", "+00:00"))
            age = now - stamp.timestamp()
            fresh = stamp.tzinfo is not None and -300 <= age <= max_age_hours * 3600
        except (TypeError, ValueError, OverflowError):
            pass
    out = []
    for uid, definition in project.test_definitions.items():
        if tag and tag not in definition["tags"]:
            continue
        dependencies = definition["models"]
        owner = rule_owner(project, definition)
        downstream = sorted({v for u in dependencies for v in project.downstream(u)} - set(dependencies))
        r = by_id.get(uid)
        observed = r["status"] if r else None
        status = (observed if fresh else "stale") if r else "not_run"
        history = store.get_setting(_key(uid), []) if store else []
        public = {key: value for key, value in definition.items() if key != "meta"}
        out.append({**public, "owner": owner, "status": status, "observed_status": observed,
                    "fresh": bool(r and fresh), "failures": r.get("failures") if r else None,
                    "generated_at": results.get("generated_at") if r else None,
                    "models": [project.nodes[u].name for u in dependencies], "model_ids": dependencies,
                    "downstream": [project.nodes[u].name for u in downstream], "downstream_ids": downstream,
                    "history": history})
    return {"rules": sorted(out, key=lambda r: r["id"]), "tag": tag,
            "note": str(tr("Business rules are native dbt tests; missing or stale results are not a pass."))}
