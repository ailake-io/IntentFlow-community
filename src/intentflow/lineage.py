"""Column lineage from the compiled SQL (SQLGlot).

Resolved: goes through CTEs, joins, aliases, aggregations, `select *` (with the catalog schema).
Unresolved (recorded, never invented): SQL without a catalog, opaque UDFs, unsupported dialect.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import hashlib
import json
import os
import threading
import logging

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer import qualify as _qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope
from sqlglot.schema import MappingSchema

from .dbt_loader import Project
from .model import Edge
from .i18n import N_

logging.getLogger("sqlglot").setLevel(logging.ERROR)     # optimizer noise on real SQL
DIALECTS = {"duckdb": "duckdb", "postgres": "postgres", "snowflake": "snowflake", "bigquery": "bigquery",
            "databricks": "databricks", "spark": "spark", "redshift": "redshift", "trino": "trino", "clickhouse": "clickhouse",
            "athena": "athena", "starrocks": "starrocks", "doris": "doris", "mysql": "mysql", "sqlserver": "tsql", "oracle": "oracle"}
MASK_FUNCS = {"MD5", "SHA", "SHA1", "SHA2", "SHA256", "SHA512", "HASH", "FARM_FINGERPRINT", "XXHASH64", "SHA256"}


@dataclass
class LineageResult:
    cache_hits: int = 0
    edges: list[Edge] = field(default_factory=list)
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)   # (node, col, motivo)
    columns_total: int = 0
    # INDIRECT dependencies (model level): a column used in JOIN/WHERE/GROUP BY/ORDER BY/window/CASE affects the whole model
    indirect: list[tuple[str, str, str, str]] = field(default_factory=list)   # (src_uid, src_col, dst_uid, via)

    @property
    def precision_report(self) -> dict:
        ok = self.columns_total - len(self.unresolved)
        return {"columns": self.columns_total, "resolved": ok,
                "unresolved": len(self.unresolved),
                "resolved_ratio": round(ok / self.columns_total, 3) if self.columns_total else 1.0}

    def upstream_of(self, uid: str, col: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == uid and e.dst_col == col]

    def downstream_of(self, uid: str, col: str) -> list[Edge]:
        return [e for e in self.edges if e.src == uid and e.src_col == col]

    def indirect_of(self, uid: str, col: str) -> list[tuple[str, str]]:
        """[(model, via)] that depend on `uid.col` only through JOIN/filter/grouping/ordering/window/CASE."""
        return [(d, via) for s, c, d, via in self.indirect if s == uid and c in (col, "*")]


def _flags(path: list) -> tuple[str, bool]:
    kind, masked = "direct", False
    for nd in path:
        e = nd.expression
        inner = e.this if isinstance(e, exp.Alias) else e
        if isinstance(inner, exp.Table):
            continue
        if not isinstance(inner, (exp.Column, exp.Table)):
            # `select *` expands into columns; any other expression is derived
            if not isinstance(inner, exp.Star):
                kind = "derived"
        for f in inner.find_all(exp.Func):
            if f.sql_name().upper() in MASK_FUNCS or type(f).__name__.upper() in MASK_FUNCS:
                masked = True
    return kind, masked


def _leaves(node, path=None):
    path = (path or []) + [node]
    if not node.downstream:
        yield node, path
    for d in node.downstream:
        yield from _leaves(d, path)


def _walk(node):
    yield node
    for d in node.downstream:
        yield from _walk(d)


# ------------------------------------------------------------ indirect dependencies
def _set_branches(scope) -> list:
    """SELECT branches of a UNION/INTERSECT/EXCEPT (nested ones flattened). `set_operation_scopes` in sqlglot ≥ 28; `union_scopes` before."""
    kids = getattr(scope, "set_operation_scopes", None)
    if kids is None:
        kids = getattr(scope, "union_scopes", None)
    out = []
    for k in kids or []:
        if isinstance(k.expression, exp.Select):
            out.append(k)
        else:
            out.extend(_set_branches(k))
    return out


def _provenance(source, name: str, rel: dict, depth: int = 0) -> set[tuple[str, str]]:
    """Physical columns (uid, column) that column `name` of a scope (CTE/subquery) derives from."""
    if depth > 12 or not isinstance(source, Scope):
        return set()
    sel = source.expression
    branches = [source] if isinstance(sel, exp.Select) else _set_branches(source)
    out: set[tuple[str, str]] = set()
    for br in branches:
        s = br.expression
        if not isinstance(s, exp.Select):
            continue
        names = [p.alias_or_name for p in s.expressions]
        if name not in names:
            if len(branches) > 1 and names:           # UNION: match by position with the first branch
                first = [p.alias_or_name for p in branches[0].expression.expressions]
                idx = first.index(name) if name in first else -1
                if idx < 0 or idx >= len(names):
                    continue
                proj = s.expressions[idx]
            else:
                continue
        else:
            proj = s.expressions[names.index(name)]
        for c in proj.find_all(exp.Column):
            out |= _resolve_column(c, br, rel, depth + 1)
    return out


def _resolve_column(c: exp.Column, scope: Scope, rel: dict, depth: int = 0) -> set[tuple[str, str]]:
    src = scope.sources.get(c.table) if c.table else None
    if isinstance(src, exp.Table):
        uid = rel.get((src.catalog.lower(), src.db.lower(), src.name.lower()))
        return {(uid, c.name.lower())} if uid else set()
    if isinstance(src, Scope):
        return _provenance(src, c.name, rel, depth)
    return set()


def _indirect_refs(expression, rel: dict) -> set[tuple[str, str, str]]:
    """(uid, column, via) of physical columns read only as a condition: JOIN/WHERE/GROUP BY/HAVING/ORDER BY/QUALIFY/window/CASE."""
    out: set[tuple[str, str, str]] = set()

    def add(nodes, scope, via):
        for n in nodes:
            if n is None:
                continue
            for c in n.find_all(exp.Column):
                for uid, col in _resolve_column(c, scope, rel):
                    out.add((uid, col, via))
    for scope in traverse_scope(expression):
        s = scope.expression
        if not isinstance(s, exp.Select):
            continue
        for j in s.args.get("joins") or []:
            add([j.args.get("on")] + list(j.args.get("using") or []), scope, "join")
        add([(s.args.get("where") or exp.Null()).this if s.args.get("where") else None], scope, "filter")
        add([s.args.get("having").this] if s.args.get("having") else [], scope, "filter")
        add([s.args.get("qualify").this] if s.args.get("qualify") else [], scope, "filter")
        add(list((s.args.get("group").expressions if s.args.get("group") else []) or []), scope, "group")
        add(list((s.args.get("order").expressions if s.args.get("order") else []) or []), scope, "sort")
        for w in s.find_all(exp.Window):
            add(list(w.args.get("partition_by") or []) + ([w.args["order"]] if w.args.get("order") else []), scope, "window")
        for case in s.find_all(exp.Case):
            add([i.this for i in case.args.get("ifs") or []], scope, "conditional")
    return out


def _model_key(project: Project, uid: str, dialect) -> str:
    """Changes if the SQL, the dialect or the parents' schema change — only then is the model's lineage recomputed."""
    n = project.nodes[uid]
    parents = sorted(
        (project.nodes[d].relation, tuple(sorted((c.name, c.type) for c in project.nodes[d].columns.values())))
        for d in n.depends_on if d in project.nodes)
    cols = tuple(sorted((c.name, c.actual) for c in n.columns.values()))
    raw = json.dumps([n.compiled_code, dialect, parents, cols, "v9"], default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _model_edges(project: Project, uid: str, schema: dict, rel: dict, dialect) -> tuple[list[Edge], list[tuple], int, list[tuple]]:
    n = project.nodes[uid]
    edges: list[Edge] = []
    unresolved: list[tuple] = []
    total = 0
    has_catalog = bool(project.actual_columns(uid))
    try:   # qualify and build the scope ONCE per model (it was the dominant cost)
        expression = _qualify.qualify(sqlglot.parse_one(n.compiled_code, read=dialect), dialect=dialect, schema=schema,
                                      validate_qualify_columns=False, identify=False)
        scope = build_scope(expression)
    except Exception as ex:
        expression = scope = None
        prep_err = f"{type(ex).__name__}: {str(ex)[:80]}"
    indirect: list[tuple] = []
    if expression is not None:
        try:
            indirect = [(su, sc, uid, via) for su, sc, via in sorted(_indirect_refs(expression, rel))]
        except Exception:               # direct lineage is computed anyway; indirect is best-effort
            indirect = []
    seen: set[Edge] = set()
    declared = {c: col for c, col in n.columns.items() if not (has_catalog and not col.actual)}
    inferred: list[str] = []
    if scope is not None:     # without YAML/catalog: the output columns come from the compiled SELECT itself
        try:
            for nm in expression.named_selects:
                if nm == "*":
                    unresolved.append((uid, "*", N_("select * without the upstream schema")))
                    total += 1
                elif nm and nm not in declared and nm.lower() not in {d.lower() for d in declared}:
                    inferred.append(nm)
        except Exception:
            pass
    for cname in list(declared) + inferred:
        total += 1
        if scope is None:
            unresolved.append((uid, cname, prep_err))
            continue
        try:
            # trim_selects/copy=False: sqlglot deep-copied the tree for every column (92 % of the time); identical result, ~19x faster
            root = lineage(cname, expression, schema=schema, dialect=dialect, scope=scope, trim_selects=False, copy=False)
        except Exception as ex:
            unresolved.append((uid, cname, f"{type(ex).__name__}: {str(ex)[:80]}"))
            continue
        found = False
        for leaf, path in _leaves(root):
            t = leaf.expression
            if not isinstance(t, exp.Table):
                continue
            src = rel.get((t.catalog.lower(), t.db.lower(), t.name.lower()))
            if not src:
                continue
            kind, masked = _flags(path)
            e = Edge(src, leaf.name.split(".")[-1].strip('"`[]').lower(), uid, cname, kind, masked)
            if e not in seen:
                seen.add(e)
                edges.append(e)
            found = True
        if not found and any(not isinstance(lf.expression, exp.Table) and lf.expression.find(exp.Column) is not None
                             for lf, _ in _leaves(root)):
            unresolved.append((uid, cname, "no upstream table resolved"))
    # a declared parent without any column edge (unknown schema, `select *`, SQL generated without introspection):
    # the impact falls back to a model dependency, never to "nothing"
    linked = {e.src for e in edges} | {i[0] for i in indirect}
    indirect += [(d, "*", uid, "opaque") for d in sorted(n.depends_on) if d in project.nodes and d not in linked]
    return edges, unresolved, total, indirect


def _rel_for(project: Project, uid: str, rel: dict) -> dict:
    """The same table can be a seed AND a source (two nodes, one relation): the model links to what it declares in depends_on."""
    parents = [d for d in project.nodes[uid].depends_on if d in project.nodes and project.nodes[d].relation]
    if not parents:
        return rel
    rel_m = dict(rel)
    for d in parents:
        parts = tuple(p.strip('"`[]').lower() for p in project.nodes[d].relation.split("."))
        if len(parts) == 3:
            rel_m[parts] = d
    return rel_m


_CTX: tuple | None = None       # context initialized once in each child process


def _init_worker(project, schema, rel, dialect):
    global _CTX
    _CTX = (project, schema, rel, dialect)


def _worker(uid: str):
    project, schema, rel, dialect = _CTX
    return uid, _model_edges(project, uid, schema, _rel_for(project, uid, rel), dialect)


def _compute(project, todo: list[str], schema, rel, dialect, workers: int) -> dict:
    """Spawned processes: the context is sent once per worker, without inheriting native thread locks."""
    # The server keeps the serial path + cache so it does not open a pool per request.
    import multiprocessing as mp
    # a child process (e.g. a script without `if __name__ == "__main__"` re-imported by spawn) never opens another pool: avoids a cascade
    if workers > 1 and len(todo) >= 2 * workers and threading.active_count() == 1 and mp.parent_process() is None:
        try:
            ctx = mp.get_context("spawn")
            with ctx.Pool(workers, initializer=_init_worker, initargs=(project, schema, rel, dialect)) as pool:
                return dict(pool.imap_unordered(_worker, todo, chunksize=max(1, len(todo) // (workers * 8))))
        except Exception:       # process/pickle failure: recompute everything serially
            pass
    return {u: _model_edges(project, u, schema, _rel_for(project, u, rel), dialect) for u in todo}


def default_workers() -> int:
    try:
        return max(1, int(os.environ.get("INTENTFLOW_WORKERS", "")))
    except ValueError:
        return min(4, os.cpu_count() or 1)      # measured: 8 processes were worse than 4 (serialization/pool dominate)


def build(project: Project, cache=None, workers: int | None = None) -> LineageResult:
    """`cache`: object with `get(key)`/`set(key, value)` (e.g. the SQLite store) to reuse unchanged models.
    `workers`: processes for the models not in the cache (default: cores, max 4; `INTENTFLOW_WORKERS` overrides)."""
    res = LineageResult()
    # MappingSchema built ONCE: passing the dict makes sqlglot renormalize everything for every column
    dialect = DIALECTS.get(project.adapter, None)
    schema = MappingSchema(project.sqlglot_schema(), dialect=dialect)
    rel = project.by_relation()
    res.cache_hits = 0
    uids = [u for u, n in project.nodes.items() if n.resource_type in ("model", "snapshot") and n.compiled_code]
    keys = {u: (_model_key(project, u, dialect) if cache is not None else None) for u in uids}
    hits = {u: cache.get(keys[u]) for u in uids} if cache is not None else {}
    todo = [u for u in uids if hits.get(u) is None]
    done = _compute(project, todo, schema, rel, dialect, workers or default_workers()) if todo else {}
    for uid in uids:                      # stable order = the project's, regardless of who finished first
        hit = hits.get(uid)
        if hit is not None:
            edges = [Edge(**e) for e in hit["edges"]]
            unres, total = [tuple(x) for x in hit["unresolved"]], hit["total"]
            indirect = [tuple(x) for x in hit.get("indirect", [])]
            res.cache_hits += 1
        else:
            edges, unres, total, indirect = done[uid]
            if cache is not None:
                cache.set(keys[uid], {"edges": [{"src": e.src, "src_col": e.src_col, "dst": e.dst, "dst_col": e.dst_col,
                                                 "kind": e.kind, "masked": e.masked} for e in edges],
                                      "unresolved": unres, "total": total, "indirect": indirect})
        res.edges.extend(edges)
        res.indirect.extend(indirect)
        res.unresolved.extend(unres)
        res.columns_total += total
    return res
