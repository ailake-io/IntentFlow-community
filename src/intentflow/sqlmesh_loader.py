"""SQLMesh → the same `Project` the dbt loader produces, so lineage, impact, drift and checks work unchanged.

SQLMesh runs in its own interpreter (INTENTFLOW_SQLMESH_PYTHON, or the current one if importable); only JSON crosses the boundary.
For CI without SQLMesh installed: `intentflow sqlmesh-export PROJECT -o sqlmesh_export.json` and use the JSON as the target.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .dbt_loader import Col, DbtNode, Project
from .i18n import tr

SNAPSHOT = "sqlmesh_export.json"

_EXPORT = r'''
import json, sys
from sqlmesh import Context
ctx = Context(paths=sys.argv[1], gateway=(sys.argv[2] or None))
out = {"dialect": None, "models": []}
try:
    out["dialect"] = ctx.config.model_defaults.dialect
except Exception:
    pass
for fqn, m in ctx.models.items():
    seed = m.kind.name == "SEED"
    q = None
    if not seed and getattr(m, "is_sql", False):
        try:
            q = m.render_query().sql(m.dialect)
        except Exception as e:
            q = None
    try:
        cols = {k: str(v) for k, v in (m.columns_to_types or {}).items()}
    except Exception:
        cols = {}
    out["models"].append({
        "fqn": fqn, "name": m.name, "kind": m.kind.name, "dialect": m.dialect or out["dialect"], "sql": q,
        "columns": cols, "depends_on": sorted(m.depends_on), "owner": m.owner, "description": m.description,
        "column_descriptions": dict(m.column_descriptions or {}), "grain": [str(g) for g in (m.grains or [])],
        "audits": [a[0] for a in (m.audits or [])], "tags": sorted(m.tags or []), "cron": getattr(m, "cron", None),
        "path": str(getattr(m, "_path", "") or ""), "python": not getattr(m, "is_sql", True) and m.kind.name != "SEED",
    })
print(json.dumps(out))
'''


def _python() -> str:
    return os.environ.get("INTENTFLOW_SQLMESH_PYTHON") or sys.executable


def is_project(path: str | Path) -> bool:
    p = Path(path)
    return p.is_dir() and (p / "models").is_dir() and any((p / n).exists() for n in ("config.yaml", "config.yml", "config.py"))


def is_snapshot(path: str | Path) -> bool:
    p = Path(path)
    return (p.is_file() and p.suffix == ".json" and "sqlmesh" in p.name) or (p.is_dir() and (p / SNAPSHOT).exists())


def export(project_dir: str | Path, gateway: str | None = None, python: str | None = None) -> dict:
    r = subprocess.run([python or _python(), "-c", _EXPORT, str(project_dir), gateway or ""], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-6:])
        hint = " (" + tr("set INTENTFLOW_SQLMESH_PYTHON to the Python where sqlmesh is installed") + ")" if "No module named 'sqlmesh'" in tail else ""
        raise RuntimeError(tr("failed to read the SQLMesh project") + f"{hint}:\n{tail}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def _relation(fqn: str) -> str:
    return fqn if fqn.startswith('"') else ".".join(f'"{p}"' for p in fqn.split("."))


def _parts(fqn: str) -> list[str]:
    return [p.strip('"') for p in fqn.replace('"."', '\x00').strip('"').split("\x00")]


_MATERIALIZED = {"FULL": "table", "VIEW": "view", "SEED": "seed", "EMBEDDED": "ephemeral", "EXTERNAL": "source", "SCD_TYPE_2": "incremental",
                 "INCREMENTAL_BY_TIME_RANGE": "incremental", "INCREMENTAL_BY_UNIQUE_KEY": "incremental",
                 "INCREMENTAL_BY_PARTITION": "incremental", "MANAGED": "table"}


def from_export(data: dict, root: str | Path | None = None) -> Project:
    models = data.get("models", [])
    dialect = (data.get("dialect") or "duckdb").lower()
    ids: dict[str, str] = {}
    for m in models:
        ids[m["fqn"]] = f"model.sqlmesh.{_parts(m['fqn'])[-1]}"
    seen: dict[str, int] = {}
    for fqn, uid in list(ids.items()):         # mesmo nome em schemas diferentes
        seen[uid] = seen.get(uid, 0) + 1
    for fqn, uid in list(ids.items()):
        if seen[uid] > 1:
            ids[fqn] = "model.sqlmesh." + ".".join(_parts(fqn)[-2:])
    nodes: dict[str, DbtNode] = {}
    for m in models:
        uid = ids[m["fqn"]]
        seed = m["kind"] == "SEED"
        node = DbtNode(
            unique_id=uid, name=_parts(m["fqn"])[-1], resource_type="seed" if seed else "model", relation=_relation(m["fqn"]),
            path=m.get("path", ""), raw_code="" if seed else (m.get("sql") or ""), compiled_code="" if seed else (m.get("sql") or ""),
            materialized=_MATERIALIZED.get(m["kind"], m["kind"].lower()),
            unique_key=(m["grain"][0] if len(m.get("grain") or []) == 1 else (m.get("grain") or None)),
            contract_enforced=bool(m.get("columns")) and m["kind"] != "VIEW",
            meta={k: v for k, v in {"owner": m.get("owner"), "sqlmesh_kind": m["kind"], "cron": m.get("cron")}.items() if v},
            tags=list(m.get("tags") or []), description=m.get("description") or "",
            depends_on=[ids[d] for d in m.get("depends_on", []) if d in ids],
        )
        descs = {k.lower(): v for k, v in (m.get("column_descriptions") or {}).items()}
        for cn, ct in (m.get("columns") or {}).items():
            node.columns[cn.lower()] = Col(cn.lower(), ct, descs.get(cn.lower(), ""), {}, [], True)
        for a in m.get("audits", []):
            node.tests.append({"name": a, "column": None, "kwargs": {}})
        nodes[uid] = node
    for m in models:                       # tabelas externas viram "source"
        for d in m.get("depends_on", []):
            if d not in ids:
                uid = f"source.sqlmesh.{_parts(d)[-1]}"
                nodes.setdefault(uid, DbtNode(unique_id=uid, name=_parts(d)[-1], resource_type="source", relation=_relation(d)))
                if uid not in nodes[ids[m["fqn"]]].depends_on:
                    nodes[ids[m["fqn"]]].depends_on.append(uid)
    return Project(nodes=nodes, adapter=dialect, root=Path(root) if root else None)


def load(target: str | Path, root: str | Path | None = None) -> Project:
    t = Path(target)
    if is_snapshot(t):
        f = t / SNAPSHOT if t.is_dir() else t
        return from_export(json.loads(f.read_text()), root or f.parent)
    return from_export(export(t), root or t)
