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
    def normalize(name):
        return ".".join(part.strip('"`[]').lower() for part in name.split("."))
    by_name = {}
    for uid, n in ctx.project.nodes.items():
        if n.resource_type in ("model", "seed", "snapshot"):
            aliases = {n.name, uid}
            parts = uid.split(".")
            if len(parts) >= 3 and parts[0] in ("model", "seed", "snapshot"):
                aliases.add(f"{parts[1]}.{n.name}")
            if n.relation:
                aliases.add(n.relation)
            for alias in aliases:
                by_name.setdefault(normalize(alias), set()).add(uid)
    out = []
    for c in ctx.contracts:
        candidates = sorted(by_name.get(normalize(c.name), set()))
        if not candidates:
            out.append(Finding("CTR001", "high", tr("contract `{name}`: the model does not exist in the project", name=c.name),
                               tr("Declared in {source}; consumers expect a table that is not produced.", source=c.source), [], "pre-merge",
                               {"contract": c.source, "kind": "missing_model"}))
            continue
        if len(candidates) > 1:
            if "CTR001" in ctx.required_checks:
                ctx.failed_checks.add("CTR001")
            out.append(Finding("CTR001", "high", tr("contract `{name}`: model name is ambiguous", name=c.name),
                               tr("Qualify the contract model with its package, full dbt id or relation; no arbitrary match is accepted."),
                               candidates, "pre-merge", {"contract": c.source, "kind": "ambiguous_model", "candidates": candidates}))
            continue
        uid = candidates[0]
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
            ts = tested.get(col, set())
            # A configured incremental key/grain is not a uniqueness assertion.
            # Likewise uniqueness of (a,b) does not prove that a alone is unique.
            single_column_unique = any(t.get("name") in UNIQUE_TESTS and
                                       [str(c).lower() for c in (t.get("kwargs", {}).get("combination_of_columns") or [])] == [col]
                                       for t in n.tests)
            enforcement_severity = "high" if "CTR001" in ctx.required_checks else "medium"
            if rule.unique and not ("unique" in ts or single_column_unique):
                out.append(Finding("CTR001", enforcement_severity, tr("{column}: the contract requires uniqueness but no test enforces it", column=f"{n.name}.{col}"),
                                   tr("Nothing guarantees the contract's promise."), [uid], "pre-merge", {"contract": c.source, "kind": "unenforced_unique"}))
            if rule.required and not (ts & NOT_NULL_TESTS):
                out.append(Finding("CTR001", enforcement_severity, tr("{column}: the contract requires a value but there is no `not_null` test", column=f"{n.name}.{col}"),
                                   tr("Nothing guarantees the contract's promise."), [uid], "pre-merge", {"contract": c.source, "kind": "unenforced_required"}))
    return out
