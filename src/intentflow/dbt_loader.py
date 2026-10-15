"""Reads dbt artifacts (manifest.json, catalog.json) into a simple graph."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from .i18n import tr


@dataclass
class Col:
    name: str
    type: str = ""
    description: str = ""
    meta: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    actual: bool = True      # came from the catalog (exists in the warehouse), not only declared in YAML


@dataclass
class DbtNode:
    unique_id: str
    name: str
    resource_type: str            # model | seed | source | snapshot
    relation: str = ""            # "db"."schema"."table"
    path: str = ""
    raw_code: str = ""
    compiled_code: str = ""
    materialized: str = ""
    unique_key: object = None
    incremental_strategy: str | None = None
    contract_enforced: bool = False
    meta: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    columns: dict[str, Col] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    tests: list[dict] = field(default_factory=list)   # {name, column, kwargs}


@dataclass
class Project:
    nodes: dict[str, DbtNode]
    tests_by_id: dict[str, tuple] = field(default_factory=dict)   # test unique_id → (attached node, name, column)
    adapter: str = "duckdb"
    root: Path | None = None
    root_package: str | None = None      # name of the root dbt project; models of installed packages (dbt_utils, evaluator...) are not the team's

    def in_root_package(self, uid: str) -> bool:
        """True for nodes of the root project (or when there is no notion of package, e.g. SQLMesh)."""
        if not self.root_package or uid not in self.nodes:
            return True
        parts = uid.split(".")
        return len(parts) < 3 or parts[1] == self.root_package

    def actual_columns(self, uid: str) -> dict[str, Col]:
        """Columns confirmed by the catalog; empty if the node was not cataloged."""
        return {k: c for k, c in self.nodes[uid].columns.items() if c.actual}

    def children(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {u: [] for u in self.nodes}
        for u, n in self.nodes.items():
            for d in n.depends_on:
                out.setdefault(d, []).append(u)
        return out

    def downstream(self, uid: str) -> list[str]:
        ch, seen, out, stack = self.children(), {uid}, [], [uid]
        while stack:
            for c in ch.get(stack.pop(), []):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
                    stack.append(c)
        return out

    def by_relation(self) -> dict[tuple[str, str, str], str]:
        out = {}
        for u, n in self.nodes.items():
            if n.relation:
                parts = [p.strip('"`[]').lower() for p in n.relation.split(".")]
                if len(parts) == 3:
                    out[tuple(parts)] = u
        return out

    def sqlglot_schema(self) -> dict:
        s: dict = {}
        for n in self.nodes.values():
            parts = [p.strip('"`[]').lower() for p in n.relation.split(".")] if n.relation else []
            if len(parts) == 3 and n.columns:
                s.setdefault(parts[0], {}).setdefault(parts[1], {})[parts[2]] = {
                    c.name.lower(): (c.type or "VARCHAR") for c in n.columns.values()
                }
        return s


def load(target_dir: str | Path, root: str | Path | None = None) -> Project:
    t = Path(target_dir)
    from . import sqlmesh_loader          # a SQLMesh project (or exported snapshot) becomes the same Project
    if not (t / "manifest.json").exists() and (sqlmesh_loader.is_project(t) or sqlmesh_loader.is_snapshot(t)):
        if sqlmesh_loader.is_project(t) and not sqlmesh_loader.is_snapshot(t) and os.environ.get("INTENTFLOW_SQLMESH_ALLOW_EXEC") != "1":
            raise ValueError(tr(
                "{dir} is a SQLMesh project: reading it runs its config.py and Python models. Run `intentflow sqlmesh-export {dir}` "
                "in a trusted environment and use the JSON as --target, or set INTENTFLOW_SQLMESH_ALLOW_EXEC=1 if you trust the code.", dir=t))
        return sqlmesh_loader.load(t, root)
    man = json.loads((t / "manifest.json").read_text())
    cat = {}
    if (t / "catalog.json").exists():
        c = json.loads((t / "catalog.json").read_text())
        cat = {**c.get("nodes", {}), **c.get("sources", {})}
    adapter = man.get("metadata", {}).get("adapter_type", "duckdb")
    nodes: dict[str, DbtNode] = {}
    tests: list[dict] = []
    tests_by_id: dict[str, tuple] = {}
    semantic = {**(man.get("semantic_models") or {}), **(man.get("metrics") or {}), **(man.get("saved_queries") or {})}
    for uid, n in {**man["nodes"], **man.get("sources", {}), **man.get("exposures", {}), **semantic}.items():
        rt = n["resource_type"]
        if rt == "test":
            tests.append(n)
            continue
        if rt not in ("model", "seed", "source", "snapshot", "exposure", "semantic_model", "metric", "saved_query"):
            continue
        cfg = n.get("config", {}) or {}
        if rt in ("semantic_model", "metric", "saved_query"):   # semantic layer: metrics depend on models through semantic models
            nodes[uid] = DbtNode(unique_id=uid, name=n["name"], resource_type=rt, path=n.get("original_file_path", ""),
                                 meta={**(n.get("meta") or {}), **(cfg.get("meta") or {}), **({"label": n["label"]} if n.get("label") else {}),
                                       **({"metric_type": n["type"]} if n.get("type") and rt == "metric" else {})},
                                 description=n.get("description", "") or "", tags=list(n.get("tags") or []),
                                 depends_on=[d for d in (n.get("depends_on") or {}).get("nodes", [])])
            continue
        if rt == "exposure":           # dashboards/applications that consume models: the impact must reach them
            ow = n.get("owner") or {}
            nodes[uid] = DbtNode(unique_id=uid, name=n["name"], resource_type="exposure", path=n.get("original_file_path", ""),
                                 meta={k: v for k, v in {"owner": ow.get("name") or ow.get("email"), "exposure_type": n.get("type"),
                                                         "url": n.get("url"), "maturity": n.get("maturity")}.items() if v},
                                 description=n.get("description", "") or "", tags=list(n.get("tags") or []),
                                 depends_on=[d for d in (n.get("depends_on") or {}).get("nodes", [])])
            continue
        node = DbtNode(
            unique_id=uid, name=n["name"], resource_type=rt,
            relation=n.get("relation_name") or "", path=n.get("original_file_path", ""),
            raw_code=n.get("raw_code", "") or "", compiled_code=n.get("compiled_code", "") or "",
            materialized=cfg.get("materialized", ""), unique_key=cfg.get("unique_key"),
            incremental_strategy=cfg.get("incremental_strategy"),
            contract_enforced=bool(((cfg.get("contract") or {}).get("enforced"))),
            meta={**(n.get("meta") or {}), **(cfg.get("meta") or {})}, tags=list(n.get("tags") or []),
            description=n.get("description", "") or "",
            depends_on=[d for d in (n.get("depends_on") or {}).get("nodes", [])],
        )
        declared = n.get("columns", {}) or {}
        actual = (cat.get(uid) or {}).get("columns", {}) or {}
        for cn, ci in actual.items():
            d = declared.get(cn.lower(), declared.get(cn, {})) or {}
            node.columns[cn.lower()] = Col(cn.lower(), ci.get("type", ""), d.get("description", ""),
                                           {**(d.get("meta") or {}), **((d.get("config") or {}).get("meta") or {})},
                                           list(d.get("tags") or []), True)
        for cn, d in declared.items():
            if cn.lower() not in node.columns:
                node.columns[cn.lower()] = Col(cn.lower(), d.get("data_type") or "", d.get("description", ""),
                                               {**(d.get("meta") or {}), **((d.get("config") or {}).get("meta") or {})},
                                               list(d.get("tags") or []), False)
        nodes[uid] = node
    for t_ in tests:
        att = t_.get("attached_node")
        md = t_.get("test_metadata") or {}
        if att in nodes:
            kw = md.get("kwargs") or {}
            nodes[att].tests.append({"name": md.get("name", t_["name"]), "column": kw.get("column_name") or t_.get("column_name"),
                                     "kwargs": kw})
            tests_by_id[t_["unique_id"]] = (att, md.get("name", t_["name"]), kw.get("column_name") or t_.get("column_name"))
    return Project(nodes=nodes, tests_by_id=tests_by_id, adapter=adapter, root=Path(root) if root else None,
                   root_package=(man.get("metadata") or {}).get("project_name"))
