"""Lineage/impact queries for the UI. Loads the project once per artifact version (mtime)."""
from __future__ import annotations

import os
import threading
from collections import deque
from pathlib import Path

from . import dbt_loader, lineage as lin
from .i18n import tr

_cache: dict = {}
_lock = threading.Lock()


def _sig(target: str):
    return tuple(os.path.getmtime(Path(target) / f) if (Path(target) / f).exists() else 0 for f in ("manifest.json", "catalog.json"))


def load(target: str, root: str | None = None, cache=None):
    """(project, lineage_result); recomputed only when manifest/catalog change. `cache` (store): only changed models are re-read."""
    target = str(Path(target).resolve())
    root = str(Path(root).resolve()) if root else str(Path(target).parent)
    key = (target, root, _sig(target))
    with _lock:
        hit = _cache.get((target, root))
        if hit and hit[0] == key:
            return hit[1], hit[2]
    proj = dbt_loader.load(target, root or Path(target).parent)
    res = lin.build(proj, cache=cache)
    if cache is not None and hasattr(cache, "flush"):
        cache.flush()
    with _lock:
        _cache[(target, root)] = (key, proj, res)
        while len(_cache) > 16:
            del _cache[next(iter(_cache))]
    return proj, res


def _resolve(proj, model: str) -> str | None:
    return next((u for u, n in proj.nodes.items() if n.name == model or u == model), None)


def models(proj, findings: list[dict] | None = None, include_packages: bool = False) -> list[dict]:
    by_node: dict[str, list[str]] = {}
    for f in findings or []:
        for n in f.get("nodes", []):
            by_node.setdefault(n, []).append(f["severity"])
    order = ["critical", "high", "medium", "low", "info"]
    out = []
    for uid, n in proj.nodes.items():
        if not include_packages and not proj.in_root_package(uid):
            continue                  # installed packages (dbt_utils, project-evaluator...) are not the team's; `scan.include_packages` shows them
        sv = by_node.get(uid, [])
        out.append({"id": uid, "name": n.name, "type": n.resource_type, "materialized": n.materialized,
                    "columns": len(proj.actual_columns(uid)) or len(n.columns), "upstream": len(n.depends_on),
                    "downstream": len(proj.downstream(uid)), "owner": n.meta.get("owner") or n.meta.get("team"),
                    "tests": len(n.tests), "findings": len(sv), "worst": min(sv, key=order.index) if sv else None,
                    "pii": any(c.meta.get("pii") or "pii" in c.tags for c in n.columns.values())})
    out.sort(key=lambda m: (m["type"] != "model", -m["downstream"], m["name"]))
    return out


def model_detail(proj, model: str) -> dict | None:
    uid = _resolve(proj, model)
    if not uid:
        return None
    n = proj.nodes[uid]
    cols = proj.actual_columns(uid) or n.columns
    return {"id": uid, "name": n.name, "type": n.resource_type, "materialized": n.materialized, "path": n.path,
            "owner": n.meta.get("owner"), "description": n.description,
            "columns": [{"name": c.name, "type": c.type, "pii": bool(c.meta.get("pii") or "pii" in c.tags),
                         "tests": sorted({t["name"] for t in n.tests if t.get("column") == c.name})} for c in cols.values()],
            "tests": [t["name"] for t in n.tests if not t.get("column")]}


def model_graph(proj, model: str, depth: int = 2) -> dict:
    uid = _resolve(proj, model)
    if not uid:
        raise ValueError(tr("model '{model}' not found", model=model))
    nodes, edges = {uid: 0}, set()
    q = deque([(uid, 0)])
    visited = {(uid, "up"), (uid, "down")}
    ch = proj.children()
    while q:
        cur, d = q.popleft()
        if abs(d) >= depth:
            continue
        for p in proj.nodes[cur].depends_on:
            if p in proj.nodes and d <= 0:
                nodes.setdefault(p, d - 1)
                edges.add((p, cur))
                if (p, "up") not in visited:
                    visited.add((p, "up"))
                    q.append((p, d - 1))
        for c in ch.get(cur, []):
            if d >= 0:
                nodes.setdefault(c, d + 1)
                edges.add((cur, c))
                if (c, "down") not in visited:
                    visited.add((c, "down"))
                    q.append((c, d + 1))
    return {"center": uid, "nodes": [{"id": u, "name": proj.nodes[u].name, "type": proj.nodes[u].resource_type, "level": lv,
                                      "owner": proj.nodes[u].meta.get("owner")} for u, lv in nodes.items()],
            "edges": [{"src": a, "dst": b} for a, b in sorted(edges)]}


def column_graph(proj, res, model: str, column: str, depth: int = 3) -> dict:
    uid = _resolve(proj, model)
    if not uid:
        raise ValueError(tr("model '{model}' not found", model=model))
    column = column.lower()
    if column not in proj.nodes[uid].columns:
        raise ValueError(tr("column '{column}' does not exist in {model}", column=column, model=model))
    nodes = {(uid, column): 0}
    edges = []
    for direction in ("up", "down"):
        q = deque([((uid, column), 0)])
        seen = {(uid, column)}
        while q:
            (u, c), d = q.popleft()
            if d >= depth:
                continue
            es = res.upstream_of(u, c) if direction == "up" else res.downstream_of(u, c)
            for e in es:
                nxt = (e.src, e.src_col) if direction == "up" else (e.dst, e.dst_col)
                lvl = -(d + 1) if direction == "up" else d + 1
                nodes.setdefault(nxt, lvl)
                edges.append({"src": f"{e.src}.{e.src_col}", "dst": f"{e.dst}.{e.dst_col}", "kind": e.kind, "masked": e.masked})
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, d + 1))
    seen_e, uniq = set(), []
    for e in edges:
        k = (e["src"], e["dst"])
        if k not in seen_e:
            seen_e.add(k)
            uniq.append(e)
    out_nodes = []
    for (u, c), lv in nodes.items():
        n = proj.nodes[u]
        col = n.columns.get(c)
        out_nodes.append({"id": f"{u}.{c}", "model": n.name, "column": c, "type": col.type if col else "", "level": lv,
                          "pii": bool(col and (col.meta.get("pii") or "pii" in col.tags)), "owner": n.meta.get("owner")})
    return {"center": f"{uid}.{column}", "nodes": out_nodes, "edges": uniq}


def impact(proj, res, model: str, column: str | None = None, external: list | None = None) -> dict:
    uid = _resolve(proj, model)
    if not uid:
        raise ValueError(tr("model '{model}' not found", model=model))
    if column:
        affected: dict[str, dict] = {}
        via_map: dict[str, set[str]] = {}
        frontier = [(uid, column.lower(), 0)]
        seen = set()
        while frontier:
            u, c, d = frontier.pop()
            for dst, via in res.indirect_of(u, c):        # JOIN/filter/grouping...: the whole model is affected
                via_map.setdefault(dst, set()).add(via)
                a = affected.setdefault(dst, {"distance": d + 1, "columns": set()})
                a["distance"] = min(a["distance"], d + 1)
                for col in proj.nodes[dst].columns:
                    if (dst, col) not in seen:
                        seen.add((dst, col))
                        frontier.append((dst, col, d + 1))
            for e in res.downstream_of(u, c):
                if (e.dst, e.dst_col) in seen:
                    continue
                seen.add((e.dst, e.dst_col))
                a = affected.setdefault(e.dst, {"distance": d + 1, "columns": set()})
                a["distance"] = min(a["distance"], d + 1)
                a["columns"].add(e.dst_col)
                frontier.append((e.dst, e.dst_col, d + 1))
        rows = [{"name": proj.nodes[u].name, "id": u, "distance": a["distance"], "columns": sorted(a["columns"]),
                 "owner": proj.nodes[u].meta.get("owner"), "tests": len(proj.nodes[u].tests),
                 **({"indirect_via": sorted(via_map[u])} if u in via_map else {})} for u, a in affected.items()]
        hit = {uid, *affected}
        consumers: dict[str, int] = {}          # dashboards, semantic models, metrics: reached through the graph, without column lineage
        for h in hit:
            base = affected[h]["distance"] if h in affected else 0
            for d in proj.downstream(h):
                if proj.nodes[d].resource_type in ("exposure", "semantic_model", "metric", "saved_query"):
                    consumers[d] = min(consumers.get(d, 99), base + 1 + (proj.downstream(h).index(d) if False else 0))
        if external:        # jobs outside the project (Spark/Python/...) that read the column or something derived from it, and the chain of outputs
            from . import external as ext
            touched = {(uid, column.lower())} | {(u, c) for u, a in affected.items() for c in a["columns"]}
            hits = ext.readers(proj, external, touched, set(affected) | {uid})
            jdist: dict[str, int] = {}
            for job, h in hits.items():        # `hits` comes in discovery order: the previous job already has a distance
                rs = h["reads"]
                base_d = min((affected[u]["distance"] for u, _, r in ext.resolve(proj, rs) if u in affected), default=0)
                jdist[job] = jdist[h["via"]] + 1 if h["via"] in jdist else base_d + h["depth"]
                rows.append({"name": job, "id": "job:" + job, "distance": jdist[job], "columns": sorted({r.column for r in rs if r.column != "*"}),
                             "owner": rs[0].owner, "tests": 0, "kind": "job", "source": rs[0].source,
                             "writes": sorted({w for r in rs for w in r.writes}), "via_job": h["via"]})
                known = {r["id"] for r in rows}
                outs = {f for r in rs for f in (r.feeds or tuple((w, "*") for w in r.writes))}
                for w, wcol in outs:       # a job output that is a project node: per column when known, otherwise the whole node
                    wu = ext.match_dataset(proj, w)
                    if not wu or wu == uid:
                        continue
                    wn = proj.nodes[wu]
                    sub = ([r2 for r2 in impact(proj, res, wn.name, wcol)["affected"] if r2.get("kind") is None]
                           if wcol != "*" and wcol in wn.columns else
                           [{"id": du, "name": proj.nodes[du].name, "distance": 1, "columns": [], "owner": proj.nodes[du].meta.get("owner"),
                             "tests": len(proj.nodes[du].tests)} for du in proj.downstream(wu)])
                    for du, name, dist, cols, own, ntests in [(wu, wn.name, 0, [wcol] if wcol != "*" else [], wn.meta.get("owner"), len(wn.tests))] + \
                            [(x["id"], x["name"], x["distance"] or 1, x["columns"], x["owner"], x["tests"]) for x in sub]:
                        if du in known or du in affected or du == uid:
                            continue
                        known.add(du)
                        rows.append({"name": name, "id": du, "distance": jdist[job] + 1 + dist, "columns": cols, "owner": own, "tests": ntests,
                                     "via_job": job})
        for xu, dist in consumers.items():
            xn = proj.nodes[xu]
            rows.append({"name": xn.name, "id": xu, "distance": dist, "columns": [], "owner": xn.meta.get("owner"), "tests": 0,
                         "kind": xn.resource_type, "exposure_type": xn.meta.get("exposure_type"), "url": xn.meta.get("url")})
    else:
        down = proj.downstream(uid)
        rows = [{"name": proj.nodes[u].name, "id": u, "distance": None, "columns": [], "owner": proj.nodes[u].meta.get("owner"),
                 "tests": len(proj.nodes[u].tests)} for u in down]
    rows.sort(key=lambda r: (r["distance"] or 99, r["name"]))
    return {"model": proj.nodes[uid].name, "column": column, "affected": rows, "owners": sorted({r["owner"] for r in rows if r["owner"]}),
            "count": len(rows)}
