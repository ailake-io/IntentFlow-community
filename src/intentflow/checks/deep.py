"""Deeper checks: null-rate drift, join fan-out, plan regression, key types, contracts."""
from __future__ import annotations

import statistics
import time

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from ..i18n import tr
from ..lineage import DIALECTS
from ..model import Finding
from . import Context, check

NUMERIC = ("INT", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "NUMBER", "INTEGER", "LONG")


def _family(t: str) -> str:
    u = (t or "").upper()
    if any(u.startswith(n) for n in NUMERIC):
        return "number"
    if u.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return "string"
    if u.startswith(("DATE", "TIMESTAMP", "TIME")):
        return "datetime"
    return u or "?"


def _join_equalities(ctx: Context, n):
    """Yield (left_uid, left_col, right_uid, right_col) of `a.x = b.y` joins between physical project relations."""
    d = DIALECTS.get(ctx.project.adapter)
    rel = ctx.project.by_relation()
    schema = ctx.project.sqlglot_schema()
    try:
        from sqlglot.optimizer import qualify
        tree = qualify.qualify(sqlglot.parse_one(n.compiled_code, read=d), dialect=d, schema=schema, validate_qualify_columns=False, identify=False)
        scopes = traverse_scope(tree)
    except Exception:
        return
    for sc in scopes:
        phys = {}
        for alias, src in sc.sources.items():
            if isinstance(src, exp.Table):
                u = rel.get((src.catalog.lower(), src.db.lower(), src.name.lower()))
                if u:
                    phys[alias] = u
        for j in sc.expression.args.get("joins", []) or []:
            on = j.args.get("on")
            if on is None:
                continue
            for eq in on.find_all(exp.EQ):
                l, r = eq.left, eq.right
                if isinstance(l, exp.Column) and isinstance(r, exp.Column) and l.table in phys and r.table in phys:
                    yield phys[l.table], l.name.lower(), phys[r.table], r.name.lower()


@check("SQL015", "Join key with incompatible types", stage="pre-merge", cost="static")
def join_type_mismatch(ctx: Context):
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type not in ("model", "snapshot") or not n.compiled_code:
            continue
        for lu, lc, ru, rc in _join_equalities(ctx, n):
            lt = ctx.project.nodes[lu].columns.get(lc)
            rt = ctx.project.nodes[ru].columns.get(rc)
            if lt and rt and lt.type and rt.type and _family(lt.type) != _family(rt.type):
                out.append(Finding("SQL015", "medium", tr("{model}: join {left} ({left_type}) = {right} ({right_type})", model=n.name,
                                                          left=f"{ctx.project.nodes[lu].name}.{lc}", left_type=lt.type,
                                                          right=f"{ctx.project.nodes[ru].name}.{rc}", right_type=rt.type),
                                   tr("Types of different families force an implicit conversion: it prevents hash/index use, can fail at runtime and match wrong values."),
                                   [uid], "pre-merge", {"left": f"{lt.type}", "right": f"{rt.type}",
                                                        "recommendation": tr("align the types in the staging layer (one explicit cast)")}))
    return out


@check("PRB004", "Join fan-out (non-unique key)", stage="pre-run", cost="probe")
def join_fanout(ctx: Context):
    if ctx.warehouse is None:
        return []
    out, seen = [], set()
    for uid, n in ctx.project.nodes.items():
        if n.resource_type not in ("model", "snapshot") or not n.compiled_code:
            continue
        for lu, lc, ru, rc in _join_equalities(ctx, n):
            for side_uid, col in ((lu, lc), (ru, rc)):
                key = (uid, side_uid, col)
                node = ctx.project.nodes[side_uid]
                if key in seen or not node.relation:
                    continue
                seen.add(key)
                try:
                    dup = int(ctx.warehouse.scalar(f'select count(*) - count(distinct "{col}") from {node.relation} where "{col}" is not null'))
                    tot = int(ctx.warehouse.scalar(f"select count(*) from {node.relation}"))
                except Exception:
                    continue
                other_dup_side_unique = any(t["name"] == "unique" and t.get("column") == col for t in node.tests)
                if dup > 0 and not other_dup_side_unique:
                    out.append(Finding("PRB004", "high" if dup * 10 > tot else "medium",
                                       tr("{model}: join on {key} multiplies rows ({dup} duplicated out of {total})", model=n.name,
                                          key=f"{node.name}.{col}", dup=dup, total=tot),
                                       tr("The join key is not unique on this side: each repeated value multiplies the rows of the other side (inflated metrics, memory)."),
                                       [uid], "pre-run", {"duplicates": dup, "rows": tot, "side": f"{node.name}.{col}",
                                                          "recommendation": tr("aggregate before joining or join on a unique key")}))
    return out


@check("PRB003", "Null-rate drift", stage="pre-run", cost="probe")
def null_rate_drift(ctx: Context):
    if ctx.warehouse is None or ctx.store is None:
        return []
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type not in ("model", "seed") or not n.relation:
            continue
        cols = list(ctx.project.actual_columns(uid))[:15]
        if not cols:
            continue
        try:
            sel = ", ".join(f'avg(case when "{c}" is null then 1.0 else 0.0 end)' for c in cols)
            rates = ctx.warehouse.rows(f"select {sel} from {n.relation}")[0]
        except Exception:
            continue
        for c, r in zip(cols, rates):
            r = float(r or 0.0)
            key = f"null:{uid}:{c}"
            hist = [v for _, v in ctx.store.history(key)][-20:]
            ctx.store.add_history(key, time.time(), r)
            if len(hist) >= 3:
                med = statistics.median(hist)
                if r - med >= 0.2 and r >= 2 * max(med, 0.01):
                    out.append(Finding("PRB003", "high" if r - med >= 0.5 else "medium",
                                       tr("{column}: {rate} nulls (usually {median})", column=f"{n.name}.{c}", rate=f"{r:.0%}", median=f"{med:.0%}"),
                                       tr("The null rate went up a lot; it usually means a broken upstream column, a join that does not match or a format change."),
                                       [uid] + ctx.project.downstream(uid)[:10], "pre-run", {"rate": r, "median": med, "history": len(hist)}))
    return out


@check("SQL014", "Plan got worse in the PR", stage="pre-merge", cost="probe")
def plan_regression(ctx: Context):
    """Compare the estimated plan of the base and new version for models whose SQL changed."""
    if ctx.warehouse is None or ctx.base_project is None:
        return []
    base_by_rel = {n.relation: n for n in ctx.base_project.nodes.values() if n.relation}
    out = []
    for uid, n in ctx.project.nodes.items():
        b = base_by_rel.get(n.relation)
        if not b or n.resource_type != "model" or not n.compiled_code or b.compiled_code == n.compiled_code:
            continue
        try:
            pb, ph = ctx.warehouse.plan(b.compiled_code), ctx.warehouse.plan(n.compiled_code)
        except Exception:
            continue
        heavy = {"CROSS_PRODUCT", "NESTED_LOOP_JOIN", "BLOCKWISE_NL_JOIN", "Nested Loop"}
        new_heavy = sorted((set(ph["operators"]) & heavy) - set(pb["operators"]))
        metric = "cost" if ph.get("cost") and pb.get("cost") else "max_rows"
        bv, hv = pb.get(metric), ph.get(metric)
        grew = bv and hv and hv > 2 * bv and hv - bv > 1000
        if new_heavy or grew:
            title = (tr("{model}: plan got worse with the change (new operator {ops})", model=n.name, ops=", ".join(new_heavy)) if new_heavy
                     else tr("{model}: plan got worse with the change ({metric}: {old} → {new})", model=n.name, metric=metric, old=f"{bv:,.0f}", new=f"{hv:,.0f}"))
            out.append(Finding("SQL014", "high" if new_heavy else "medium", title,
                               tr("The changed version has a worse estimated plan than the base: risk of slowness and of running out of memory."),
                               [uid], "pre-merge", {"base": pb, "head": ph, "new_heavy_operators": new_heavy}))
    return out


@check("DBT005", "Exposed model without a contract", stage="pre-merge", cost="static")
def missing_contract(ctx: Context):
    out = []
    for uid, n in ctx.project.nodes.items():
        down = len(ctx.project.downstream(uid))
        if n.resource_type == "model" and down >= 3 and not n.contract_enforced:
            out.append(Finding("DBT005", "low", tr("{model}: {n} consumers and no model contract", model=n.name, n=down),
                               tr("Without `contract: {enforced: true}`, type/column changes only show up when someone consumes them."),
                               [uid], "pre-merge", {"downstream": down, "recommendation": tr("turn on contract.enforced and declare columns and types")}))
    return out
