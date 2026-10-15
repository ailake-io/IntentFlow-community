"""Badly written SQL and optimization opportunities (static AST analysis, nothing is executed).

Each finding carries an objective recommendation; when a deterministic rewrite exists it goes in
`evidence.suggested_sql` (over the compiled SQL, for review — never applied on its own).
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer import optimize

from ..i18n import cat, tr
from ..lineage import DIALECTS
from ..model import Finding
from . import Context, check

NON_SARGABLE = (exp.Lower, exp.Upper, exp.Cast, exp.TryCast, exp.Substring, exp.Trim, exp.Abs, exp.Length)


def _parse(n, project):
    d = DIALECTS.get(project.adapter)
    try:
        return sqlglot.parse_one(n.compiled_code, read=d)
    except Exception:
        return None


def _models(ctx):
    for uid, n in ctx.project.nodes.items():
        if n.resource_type in ("model", "snapshot") and n.compiled_code:
            key = (uid, ctx.project.adapter, n.compiled_code)
            if key not in ctx.parsed_sql:
                ctx.parsed_sql[key] = _parse(n, ctx.project)
            tree = ctx.parsed_sql[key]
            if tree is not None:
                yield uid, n, tree


def _impact(ctx, uid) -> str:
    return "high" if len(ctx.project.downstream(uid)) >= 3 else "medium"


def _f(check_id, sev, uid, n, title, detail, **ev):
    return Finding(check_id, sev, cat(n.name, ": ", title), detail, [uid], "pre-merge", ev)


_STAR_REF = re.compile(r"select\s+\*\s+from\s+\{\{\s*ref\(\s*['\"](\w+)['\"]\s*\)\s*\}\}", re.I)


def star_fix(project, n):
    """Deterministic fix: `select * from {{ ref('x') }}` → the catalog columns of x."""
    refs = {}
    for name in _STAR_REF.findall(n.raw_code or ""):
        src = next((u for u, m in project.nodes.items() if m.name == name), None)
        cols = list(project.actual_columns(src)) if src else []
        if cols:
            refs[name] = cols
    return {"kind": "expand_star", "node": n.name, "refs": refs} if refs else None


def _import_star(star, tree, project) -> bool:
    """`select * from <one table>` without join/filter, in an import CTE or as the whole model (tmp/staging),
    whose upstream has no known columns: nothing mechanical to expand (an idiom of packages like Fivetran)."""
    sel = star.parent
    if not isinstance(sel, exp.Select) or len(sel.expressions) != 1:
        return False
    if any(sel.args.get(k) for k in ("joins", "where", "group", "having", "qualify", "distinct", "limit")):
        return False
    frm = sel.args.get("from_") or sel.args.get("from")
    if frm is None or not isinstance(frm.this, exp.Table):
        return False
    if not (isinstance(sel.parent, exp.CTE) or sel is tree):
        return False
    t = frm.this
    uid = project.by_relation().get((t.catalog.lower(), t.db.lower(), t.name.lower()))
    return not (uid and (project.actual_columns(uid) or project.nodes[uid].columns))


@check("SQL001", "SELECT * in a model")
def select_star(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        # `select *` in the final SELECT alone is enough to break with new/removed columns
        stars = [s for s in tree.find_all(exp.Star) if not isinstance(s.parent, exp.Count) and not _import_star(s, tree, ctx.project)]
        if stars:
            f = _f("SQL001", "medium", uid, n, tr("{n} use(s) of SELECT *", n=len(stars)),
                   tr("SELECT * couples the model to the upstream schema (a new/removed column changes the output without warning) and reads unneeded columns (cost and memory)."),
                   recommendation=tr("list only the columns you use"), occurrences=len(stars))
            f.fix = star_fix(ctx.project, n)
            out.append(f)
    return out


@check("SQL002", "Cartesian product")
def cartesian(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for j in tree.find_all(exp.Join):
            cross = (j.args.get("kind") or "").upper() == "CROSS" or (not j.args.get("on") and not j.args.get("using") and not j.args.get("side") and (j.args.get("kind") or "") != "NATURAL")
            if cross and not isinstance(j.this, exp.Unnest):
                out.append(_f("SQL002", "high", uid, n, tr("JOIN without a condition (cartesian product)"),
                              tr("Without ON/USING, the row count is the product of both inputs; it is the most common cause of running out of memory."),
                              join=j.sql()[:120], recommendation=tr("add the join condition or aggregate first")))
    return out


@check("SQL003", "UNION without ALL")
def union_distinct(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for u in tree.find_all(exp.Union):
            if u.args.get("distinct", True) and not isinstance(u, (exp.Except, exp.Intersect)):
                out.append(_f("SQL003", "low", uid, n, tr("UNION deduplicates"),
                              tr("UNION sorts/groups the whole result to remove duplicates. If the inputs are already disjoint, UNION ALL is cheaper."),
                              recommendation=tr("use UNION ALL when duplicates are impossible")))
                break
    return out


@check("SQL004", "Unneeded ORDER BY")
def useless_order(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for sub in list(tree.find_all(exp.Subquery)) + list(tree.find_all(exp.CTE)):
            inner = sub.this
            if isinstance(inner, exp.Select) and inner.args.get("order") and not inner.args.get("limit"):
                out.append(_f("SQL004", "low", uid, n, tr("ORDER BY in a subquery/CTE without LIMIT"),
                              tr("The consumer throws the ordering away and it costs memory/disk."), recommendation=tr("remove the inner ORDER BY")))
                break
    return out


@check("SQL005", "Non-sargable predicate")
def non_sargable(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        hits = []
        for cond in list(tree.find_all(exp.Where)) + list(tree.find_all(exp.Join)):
            for cmp_ in cond.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ, exp.In):
                lhs = cmp_.this
                if isinstance(lhs, NON_SARGABLE) and lhs.find(exp.Column):
                    hits.append(cmp_.sql()[:100])
        if hits:
            out.append(_f("SQL005", "medium", uid, n, tr("function applied to the column in a filter/join"),
                          tr("Applying a function to the column prevents using an index, partition or file pruning (full scan)."),
                          examples=hits[:3], recommendation=tr("move the function to the constant side or materialize the transformed column")))
    return out


@check("SQL006", "Suspicious DISTINCT")
def distinct_masking(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for s in tree.find_all(exp.Select):
            if s.args.get("distinct") and s.args.get("joins"):
                out.append(_f("SQL006", "medium", uid, n, tr("SELECT DISTINCT combined with JOIN"),
                              tr("DISTINCT after a JOIN often hides duplicates (fan-out) caused by a non-unique key; it costs a sort and masks a modeling error."),
                              recommendation=tr("fix the join cardinality (aggregate first or use a unique key)")))
                break
    return out


@check("SQL007", "Correlated subquery")
def correlated(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for sq in tree.find_all(exp.Subquery):
            if isinstance(sq.parent, (exp.Select, exp.Alias, exp.EQ, exp.GT, exp.LT)) and not isinstance(sq.parent, exp.From):
                inner_tables = {t.alias_or_name for t in sq.find_all(exp.Table)}
                outer_refs = {c.table for c in sq.find_all(exp.Column) if c.table and c.table not in inner_tables}
                if outer_refs and isinstance(sq.this, exp.Select):
                    out.append(_f("SQL007", "medium", uid, n, tr("correlated subquery"),
                                  tr("Runs once per row in many engines (quadratic cost)."),
                                  recommendation=tr("rewrite as a JOIN with prior aggregation or a window function")))
                    break
    return out


@check("SQL008", "LIKE with a leading wildcard")
def leading_wildcard(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for lk in tree.find_all(exp.Like, exp.ILike):
            pat = lk.expression
            if isinstance(pat, exp.Literal) and pat.is_string and pat.name.startswith("%"):
                out.append(_f("SQL008", "low", uid, n, tr("LIKE '%…' prevents using an index"), tr("A leading wildcard forces a full scan."),
                              pattern=pat.name[:40], recommendation=tr("use a prefix search, a trigram index or a derived column")))
                break
    return out


@check("SQL009", "NOT IN with a subquery")
def not_in_subquery(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for nt in tree.find_all(exp.Not):
            if isinstance(nt.this, exp.In) and nt.this.args.get("query"):
                out.append(_f("SQL009", "medium", uid, n, tr("NOT IN (subquery)"),
                              tr("Returns nothing if the subquery contains NULL (three-valued logic) and often gets a bad plan."),
                              recommendation=tr("use NOT EXISTS or LEFT JOIN ... IS NULL")))
                break
    return out


@check("SQL010", "Very large IN list")
def big_in(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for i in tree.find_all(exp.In):
            if len(i.expressions) > 100:
                out.append(_f("SQL010", "low", uid, n, tr("IN with {n} literals", n=len(i.expressions)), tr("Large lists degrade planning."),
                              recommendation=tr("load the values into a table/seed and JOIN")))
                break
    return out


@check("SQL011", "OR condition in a JOIN")
def or_join(ctx: Context):
    out = []
    for uid, n, tree in _models(ctx):
        for j in tree.find_all(exp.Join):
            on = j.args.get("on")
            if on is not None and on.find(exp.Or):
                out.append(_f("SQL011", "medium", uid, n, tr("JOIN with OR in the condition"),
                              tr("Prevents a hash join in most engines and falls back to a nested loop."),
                              recommendation=tr("split into two JOINs and combine them with UNION ALL")))
                break
    return out


def equivalent(wh, a_sql: str, b_sql: str) -> bool | None:
    """Compare the results as multisets in the warehouse itself. None = could not be checked."""
    try:
        q = (f"select (select count(*) from (({a_sql}) except all ({b_sql}))) + "
             f"(select count(*) from (({b_sql}) except all ({a_sql})))")
        return int(wh.scalar(q)) == 0
    except Exception:
        return None


@check("SQL012", "Optimized rewrite available")
def optimizer_rewrite(ctx: Context):
    """Optimized SQL (qualified, predicates pushed down, CTEs/subqueries simplified).
    With a warehouse available, equivalence is checked by running both versions."""
    out = []
    schema = ctx.project.sqlglot_schema()
    d = DIALECTS.get(ctx.project.adapter)
    for uid, n, tree in _models(ctx):
        try:
            opt = optimize(tree.copy(), schema=schema, dialect=d).sql(dialect=d, pretty=True)
        except Exception:
            continue
        base = tree.sql(dialect=d, pretty=True)
        try:   # the optimizer output may not re-parse in the dialect (e.g. JSONExtract): no suggestion, no check crash
            n_opt_ctes = len(list(sqlglot.parse_one(opt, read=d).find_all(exp.CTE)))
        except Exception:
            continue
        if opt == base or n_opt_ctes >= len(list(tree.find_all(exp.CTE))):
            continue
        eq = equivalent(ctx.warehouse, n.compiled_code, opt) if ctx.warehouse is not None else None
        if eq is False:
            out.append(Finding("SQL012", "info", tr("{model}: the optimizer rewrite is NOT equivalent (discarded)", model=n.name),
                               tr("The result differs from the original when run; the suggestion was discarded for safety."), [uid], "pre-merge",
                               {"verified_equivalent": False}))
            continue
        out.append(Finding("SQL012", "info", tr("{model}: the SQLGlot optimizer simplifies this SQL", model=n.name),
                           tr("Fewer CTEs/subqueries and predicates pushed to the source. Review and adopt it if the semantics are the same."),
                           [uid], "pre-merge", {"suggested_sql": opt, "verified_equivalent": eq,
                                                "equivalence": tr("checked by execution (EXCEPT ALL both ways)") if eq else tr("not checked: no warehouse")}))
    return out
