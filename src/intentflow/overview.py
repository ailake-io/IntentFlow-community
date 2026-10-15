"""Single view: what is running, what failed, what is being fixed and what was prevented."""
from __future__ import annotations

from .i18n import tr

import time
from collections import Counter, defaultdict, OrderedDict
import threading

from . import incidents as inc_mod
from .checks import REGISTRY, load_all
from .store import Store


def build(store: Store, window_h: int = 24) -> dict:
    now = time.time()
    load_all()
    disabled = set(store.get_setting("policy.disabled_checks", []) or [])
    findings = store.findings("open")
    sev = Counter(f["severity"] for f in findings)
    runs = store.runs(500)
    by = defaultdict(list)
    for r in runs:
        by[(r["system"], r["pipeline"])].append(r)
    pipelines = []
    for (sys_, pipe), rs in by.items():
        rs.sort(key=lambda r: r["start"] or 0, reverse=True)
        pipelines.append({"system": sys_, "pipeline": pipe, "last_state": rs[0]["state"], "runs": len(rs),
                          "failed": sum(r["state"] == "failed" for r in rs), "last_start": rs[0]["start"], "url": rs[0]["url"],
                          "last_duration": rs[0]["duration"]})
    pipelines.sort(key=lambda p: (p["last_state"] != "failed", p["last_state"] != "running", p["pipeline"]))
    latest: dict = {}
    for s in store.recent_samples(now - window_h * 3600):
        latest[(s.source, s.scope, s.metric)] = s
    cluster = [{"source": s.source, "scope": s.scope, "metric": s.metric, "value": s.value, "limit": s.limit,
                "ratio": (s.value / s.limit) if s.limit else None} for s in latest.values()]
    cluster.sort(key=lambda c: -(c["ratio"] or 0))
    proj = None
    target = store.get_setting("dbt.target")
    if target:
        try:
            from . import dbt_loader
            proj = dbt_loader.load(target)
        except Exception:
            proj = None
    incidents = [{"id": i.id, "title": i.title, "severity": i.severity, "findings": len(i.findings), "finding_ids": [f["id"] for f in i.findings], "impacted": len(i.impacted),
                  "owners": i.owners} for i in inc_mod.group(findings, proj)]
    props = store.proposals()
    return {
        "generated_at": now,
        "summary": {"open_findings": len(findings), "by_severity": dict(sev),
                    "running": sum(p["last_state"] == "running" for p in pipelines),
                    "failing": sum(p["last_state"] == "failed" for p in pipelines),
                    "fixes_pending": sum(p["status"] in ("pending", "approved", "invalid") for p in props),
                    "fixes_applied": sum(p["status"] == "applied" for p in props)},
        "check_stats": [dict(c, suggest_disable=bool(c["fp_rate"] is not None and c["fp_rate"] >= 0.5 and (c["false_positive"] + c["resolved"]) >= 5))
                        for c in store.check_stats()],
        "openlineage": store.ol_jobs()[:50],
        "dbt_run": store.get_setting("state.dbt_run"),
        "checks": [{"id": c.id, "title": c.title, "stage": c.stage, "cost": c.cost, "enabled": c.id not in disabled}
                   for c in sorted(REGISTRY.values(), key=lambda c: c.id)],
        "incidents": incidents[:50], "findings": findings[:200], "pipelines": pipelines, "cluster": cluster[:50],
        "proposals": [{k: v for k, v in p.items() if k not in _HIDDEN_PROPOSAL_KEYS} | {"changes": [{"file": c["file"], "diff": c["diff"]} for c in p["changes"]]}
                      for p in props[:50]],
        "audit": store.audit_log(limit=30),
    }


# raw backups hold whole original files (outside a viewer's write scope too); the diff is what the UI needs
_HIDDEN_PROPOSAL_KEYS = {"backup", "backup_bytes", "project_root", "validation_request"}

_CACHE: OrderedDict = OrderedDict()
_CACHE_GUARD = threading.Lock()
_CACHE_LOCKS: dict[str, list] = {}    # lock + active/waiting references; the entry is removed when idle
_CACHE_LIMIT = 128


def _signature(store: Store):
    import os
    key = store.path
    if store.backend == "sqlite" and os.path.exists(key):
        files = (key, key + "-wal")
        return tuple((os.stat(f).st_mtime_ns, os.stat(f).st_size) if os.path.exists(f) else (0, 0) for f in files)
    c = store.con
    return (tuple(c.execute("select count(*), coalesce(max(last_seen), 0) from findings").fetchone()),
            tuple(c.execute("select count(*), coalesce(max(ts), 0) from audit").fetchone()),
            tuple(c.execute("select count(*), coalesce(max(updated), 0) from proposals").fetchone()),
            tuple(c.execute("select count(*), coalesce(max(\"end\"), 0) from runs").fetchone()),
            tuple(c.execute("select count(*), coalesce(max(ts), 0) from samples").fetchone()),
            tuple(c.execute("select count(*), coalesce(max(updated), 0) from settings").fetchone()))


def build_cached(store: Store, ttl: float = 2.0) -> dict:
    """Short cache with one build per store: keeps dozens of requests from rebuilding the view at the same time."""
    key = str(store.path)
    with _CACHE_GUARD:
        slot = _CACHE_LOCKS.get(key)
        if slot is None:
            slot = [threading.Lock(), 0]
            _CACHE_LOCKS[key] = slot
        slot[1] += 1
    lock = slot[0]
    try:
        with lock:
            signature = _signature(store)
            now = time.time()
            with _CACHE_GUARD:
                hit = _CACHE.get(key)
                if hit and hit[0] == signature and now - hit[1] < ttl:
                    _CACHE.move_to_end(key)
                    return hit[2]
            out = build(store)
            signature = _signature(store)      # persiste a assinatura mais recente se houve escrita concorrente
            with _CACHE_GUARD:
                _CACHE[key] = (signature, time.time(), out)
                _CACHE.move_to_end(key)
                while len(_CACHE) > _CACHE_LIMIT:
                    _CACHE.popitem(last=False)
            return out
    finally:
        with _CACHE_GUARD:
            slot[1] -= 1
            if slot[1] == 0 and _CACHE_LOCKS.get(key) is slot:
                del _CACHE_LOCKS[key]
