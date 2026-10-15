"""SQL lint with SQLFluff over each model's compiled SQL (optional: `pip install intentflow[lint]`).

Only rules that point to correctness/performance risk, not style (layout and capitalization are left out). Customizable with
`policy.lint_rules` (list of codes, e.g. ["AM04","RF02"]). Results are cached by SQL hash.
"""
from __future__ import annotations

import hashlib
import json

from ..i18n import N_, tr
from ..model import Finding
from . import Context, check

DEFAULT_RULES = ["AM01", "AM02", "AM03", "AM04", "AM06", "CV05", "CV08", "RF01", "RF02", "RF03", "ST03", "ST05", "ST07", "ST08"]
DIALECT = {"spark": "sparksql", "databricks": "databricks", "sqlserver": "tsql", "mysql": "mysql", "duckdb": "duckdb"}
SEVERITY = {"AM04": "medium", "AM02": "medium", "RF02": "medium", "CV05": "medium", "AM06": "low", "ST07": "low"}
WHY = {
    "AM01": N_("DISTINCT with GROUP BY: the DISTINCT is redundant and costs an extra sort."),
    "AM02": N_("UNION without ALL deduplicates (costs a sort/hash); use UNION ALL if you do not need to deduplicate."),
    "AM03": N_("ORDER BY with an ambiguous direction across columns."),
    "AM04": N_("`SELECT *`: the column set changes with the upstream and silently breaks consumers."),
    "AM05": N_("JOIN without a qualifier (INNER/LEFT): the intent stays implicit."),
    "AM06": N_("GROUP BY/ORDER BY by position and by name mixed: fragile to order changes."),
    "CV05": N_("Comparison with NULL using `=`/`<>`: always unknown; use IS [NOT] NULL."),
    "CV08": N_("RIGHT JOIN: use LEFT JOIN swapping the order; more readable and less error-prone."),
    "RF01": N_("Reference to a table/alias that is not in the FROM."),
    "RF02": N_("Unqualified column in a query with several tables: ambiguous, and it breaks when the upstream gains the column."),
    "RF03": N_("Mix of qualified and unqualified columns."),
    "ST03": N_("CTE defined and never used."),
    "ST05": N_("Subquery in a JOIN/FROM that would be clearer and cheaper as a CTE."),
    "ST07": N_("`USING` instead of an explicit `ON`: column ambiguity."),
    "ST08": N_("`DISTINCT(col)` with parentheses is misleading: DISTINCT applies to the whole row."),
}


def _linter(dialect: str, rules: list[str]):
    from sqlfluff.core import FluffConfig, Linter
    return Linter(config=FluffConfig(overrides={"dialect": dialect, "rules": ",".join(rules)}))


def lint_sql(sql: str, dialect: str, rules: list[str]) -> list[dict]:
    res = _linter(dialect, rules).lint_string(sql)
    # PRS/LXR/TMP = parse/template error of SQLFluff itself, not a problem in the SQL
    return [{"rule": v.rule_code(), "line": v.line_no, "desc": v.desc()} for v in res.get_violations() if v.rule_code() not in ("PRS", "LXR", "TMP")]


@check("LINT001", "SQL problem reported by SQLFluff", stage="pre-merge", cost="static")
def sql_lint(ctx: Context):
    # opt-in: SQLFluff takes seconds per model; `intentflow config set lint.enabled true` turns it on (results cached by SQL hash)
    if ctx.project is None or ctx.store is None or not ctx.store.get_setting("lint.enabled"):
        return []
    try:
        import sqlfluff  # noqa: F401
    except ImportError:
        return []
    rules = list(ctx.policies.get("lint_rules") or DEFAULT_RULES)
    d = ctx.project.adapter or "ansi"
    dialect = DIALECT.get(d, d)
    out = []
    for uid, n in ctx.project.nodes.items():
        sql = n.compiled_code or ""
        if n.resource_type not in ("model", "snapshot") or not sql.strip():
            continue
        key = "lint:" + hashlib.sha1(f"{sql}|{dialect}|{','.join(rules)}|v1".encode()).hexdigest()
        hit = ctx.store.get(key) if ctx.store is not None and hasattr(ctx.store, "get") else None
        if hit is None:
            try:
                hit = {"v": lint_sql(sql, dialect, rules)}
            except Exception:                       # unknown dialect or SQL the SQLFluff parser does not understand
                hit = {"v": []}
            if ctx.store is not None and hasattr(ctx.store, "set"):
                ctx.store.set(key, hit)
        by_rule: dict[str, list[int]] = {}
        for v in hit["v"]:
            by_rule.setdefault(v["rule"], []).append(v["line"])
        for rule, lines in sorted(by_rule.items()):
            why = tr(WHY[rule]) if rule in WHY else tr("SQLFluff rule.")
            out.append(Finding("LINT001", SEVERITY.get(rule, "low"), f"{n.name}: {rule} ({len(lines)}x)", why, [uid],
                               "pre-merge", {"rule": rule, "lines": lines[:10], "count": len(lines), "file": n.path,
                                             "recommendation": why if rule in WHY else ""}))
    return out
