"""Data contract × reality (project catalog/tests): what the consumer agreed to and what actually exists."""
from __future__ import annotations

from ..contracts import family
from ..i18n import tr
from ..model import Finding
from . import Context, check

UNIQUE_TESTS = {"unique", "unique_combination_of_columns", "dbt_utils.unique_combination_of_columns"}
NOT_NULL_TESTS = {"not_null"}


@check("CTR001", "Data contract violated or not covered", stage="pre-merge")
def contract_checks(ctx: Context):
    if not ctx.contracts or ctx.project is None:
        return []
    by_name = {}
    for uid, n in ctx.project.nodes.items():
        if n.resource_type in ("model", "seed", "snapshot"):
            by_name.setdefault(n.name.lower(), uid)
    out = []
    for c in ctx.contracts:
        uid = by_name.get(c.name.lower())
        if not uid:
            out.append(Finding("CTR001", "high", tr("contract `{name}`: the model does not exist in the project", name=c.name),
                               tr("Declared in {source}; consumers expect a table that is not produced.", source=c.source), [], "pre-merge",
                               {"contract": c.source, "kind": "missing_model"}))
            continue
        n = ctx.project.nodes[uid]
        down = len(ctx.project.downstream(uid))
        have = ctx.project.actual_columns(uid) or n.columns
        tested = {(t.get("column") or "").lower(): set() for t in n.tests}
        for t in n.tests:
            tested.setdefault((t.get("column") or "").lower(), set()).add(str(t.get("name")))
        affects = " " + tr("Affects {n} downstream model(s).", n=down) if down else ""
        for col, rule in c.columns.items():
            actual = have.get(col)
            if actual is None:
                out.append(Finding("CTR001", "critical" if down else "high", tr("{column}: contract column does not exist", column=f"{n.name}.{col}"),
                                   tr("{source} promises the column; the model does not produce it.", source=c.source) + affects,
                                   [uid], "pre-merge", {"contract": c.source, "kind": "missing_column", "downstream": down}))
                continue
            want, got = family(rule.type), family(actual.type)
            if want and got and want != got:
                out.append(Finding("CTR001", "high", tr("{column}: type {actual} differs from the contract ({expected})", column=f"{n.name}.{col}",
                                                        actual=actual.type, expected=rule.type),
                                   tr("Contract in {source}.", source=c.source), [uid], "pre-merge",
                                   {"contract": c.source, "kind": "type_mismatch", "expected": rule.type, "actual": actual.type}))
            ts = tested.get(col, set()) | tested.get("", set())        # column test or model test (e.g. unique combination)
            declared_key = n.unique_key is not None and col in str(n.unique_key).lower()      # dbt unique_key / SQLMesh grain
            if rule.unique and not (ts & UNIQUE_TESTS or declared_key):
                out.append(Finding("CTR001", "medium", tr("{column}: the contract requires uniqueness but no test enforces it", column=f"{n.name}.{col}"),
                                   tr("Nothing guarantees the contract's promise."), [uid], "pre-merge", {"contract": c.source, "kind": "unenforced_unique"}))
            if rule.required and not (ts & NOT_NULL_TESTS):
                out.append(Finding("CTR001", "medium", tr("{column}: the contract requires a value but there is no `not_null` test", column=f"{n.name}.{col}"),
                                   tr("Nothing guarantees the contract's promise."), [uid], "pre-merge", {"contract": c.source, "kind": "unenforced_required"}))
    return out
