"""Findings from the actual result of the last dbt run (run_results.json)."""
from __future__ import annotations

from ..dbt_results import median_history
from ..business_rules import rule_owner
from ..i18n import cat, tr
from ..model import Finding
from . import Context, check


def _node(ctx, uid):
    return ctx.project.nodes.get(uid)


@check("DBTR001", "Failing dbt test", stage="continuous", cost="statistical")
def failing_tests(ctx: Context):
    r = ctx.dbt_results
    if not r:
        return []
    out = []
    for x in r["results"]:
        if not x["unique_id"].startswith(("test.", "unit_test.")) or x["status"] not in ("fail", "error", "warn"):
            continue
        att, tname, col = ctx.project.tests_by_id.get(x["unique_id"], (None, x["unique_id"].split(".")[2], None))
        definition = ctx.project.test_definitions.get(x["unique_id"], {})
        dependencies = definition.get("models") or ([att] if att else [])
        if att is None and dependencies:
            att = dependencies[0]
        n = _node(ctx, att)
        downstream = {v for u in dependencies for v in ctx.project.downstream(u)} - set(dependencies)
        down = len(downstream)
        where = f"{n.name}.{col}" if n and col else (n.name if n else x["unique_id"])
        warn = x["status"] == "warn"
        title = tr("{where}: test `{test}` is warning", where=where, test=tname) if warn else tr("{where}: test `{test}` failed", where=where, test=tname)
        if x["failures"]:
            title = cat(title, " (", tr("{n} row(s)", n=x["failures"]), ")")
        out.append(Finding("DBTR001", "medium" if warn else ("critical" if down >= 3 else "high"), title,
                           (x["message"] or "") + (" " + tr("Affects {n} downstream model(s).", n=down) if down else ""),
                           dependencies, "continuous",
                           {"test": tname, "test_id": x["unique_id"], "status": x["status"], "failures": x["failures"], "downstream": down,
                            **({"owner": rule_owner(ctx.project, definition)} if definition else {}),
                            **({"business_rule": True} if "business_rule" in definition.get("tags", []) else {})}))
    return out


@check("DBTR002", "Model with a run error", stage="continuous", cost="statistical", emits=("DBTR003",))
def failed_models(ctx: Context):
    r = ctx.dbt_results
    if not r:
        return []
    out = []
    skipped = [x["unique_id"] for x in r["results"] if x["status"] == "skipped" and not x["unique_id"].startswith(("test.", "unit_test."))]
    for x in r["results"]:
        if x["unique_id"].startswith(("test.", "unit_test.")) or x["status"] != "error":
            continue
        n = _node(ctx, x["unique_id"])
        out.append(Finding("DBTR002", "high", tr("{model}: run error", model=n.name if n else x["unique_id"]),
                           (x["message"] or "")[:300], [x["unique_id"]], "continuous", {"skipped_downstream": len(skipped)}))
    # A failing test can block descendants even when every executed model succeeded.
    if skipped and any(x["status"] in ("fail", "error") for x in r["results"]):
        out.append(Finding("DBTR003", "medium", tr("{n} model(s) did not run because of an upstream failure", n=len(skipped)),
                           tr("dbt skipped them; their data is out of date."), skipped[:20], "continuous", {}))
    return out


@check("DBTR004", "Model got slower", stage="continuous", cost="statistical")
def slow_models(ctx: Context):
    r = ctx.dbt_results
    if not r or ctx.store is None:
        return []
    out = []
    for x in r["results"]:
        if x["unique_id"].startswith(("test.", "unit_test.")) or x["status"] not in ("success", "pass"):
            continue
        med, n = median_history(ctx.store, x["unique_id"])
        # the history already includes this run when recorded first; require a sample and a clear slowdown
        if med and n >= 5 and x["time"] > 2 * med and x["time"] - med > 5:
            node = _node(ctx, x["unique_id"])
            out.append(Finding("DBTR004", "medium", tr("{model}: {s}s (median {median}s)", model=node.name if node else x["unique_id"],
                                                       s=f"{x['time']:.0f}", median=f"{med:.0f}"),
                               tr("More than twice the usual time; risk of missing the window and of using more memory."),
                               [x["unique_id"]], "continuous", {"time": x["time"], "median": med}))
    return out
