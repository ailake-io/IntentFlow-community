"""Estimated cost before running: the warehouse plan (EXPLAIN), without running the query."""
from __future__ import annotations

from ..i18n import tr
from ..model import Finding
from . import Context, check

HEAVY = {"CROSS_PRODUCT", "NESTED_LOOP_JOIN", "BLOCKWISE_NL_JOIN", "Nested Loop"}


@check("SQL013", "Expensive plan (estimate)", stage="pre-run", cost="probe")
def expensive_plan(ctx: Context):
    if ctx.warehouse is None:
        return []
    limit = int(ctx.policies.get("max_estimated_rows", 10_000_000))
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type not in ("model", "snapshot") or not n.compiled_code:
            continue
        try:
            plan = ctx.warehouse.plan(n.compiled_code)
        except Exception:
            continue    # the relation may not exist yet (new model in the PR): no plan, no finding
        heavy = sorted(set(plan["operators"]) & HEAVY)
        big = plan["max_rows"] is not None and plan["max_rows"] > limit
        if heavy and (big or plan["max_rows"] is None or any("CROSS" in h for h in heavy)):
            out.append(Finding("SQL013", "high", tr("{model}: plan uses {ops}", model=n.name, ops=", ".join(heavy)),
                               tr("The planner chose a join without hash/merge (nested loop or cartesian product): the cost grows quadratically with volume."),
                               [uid], "pre-run", {"operators": heavy, "estimated_max_rows": plan["max_rows"],
                                                  "recommendation": tr("make the join condition an equality or aggregate first")}))
        elif big:
            out.append(Finding("SQL013", "medium", tr("{model}: estimate of {rows} rows in one operator", model=n.name, rows=f"{plan['max_rows']:,}"),
                               tr("Above the policy limit ({limit}); memory and time risk.", limit=f"{limit:,}"), [uid], "pre-run",
                               {"estimated_max_rows": plan["max_rows"], "limit": limit}))
    return out
