"""Findings from Great Expectations and Soda results, linked to the graph (downstream impact)."""
from __future__ import annotations

from ..i18n import N_, tr
from ..model import Finding
from . import Context, check


def _index(project):
    idx: dict[str, list[str]] = {}
    for uid, n in project.nodes.items():
        keys = {n.name.lower()}
        if n.relation:
            parts = [p.strip('"`[]').lower() for p in n.relation.split(".")]
            keys |= {parts[-1], ".".join(parts[-2:])}
        for k in keys:
            idx.setdefault(k, []).append(uid)
    return idx


def _match(idx, table: str) -> str | None:
    t = (table or "").strip('"`[]').lower().replace('"', "")
    for key in (t, t.split(".")[-1], ".".join(t.split(".")[-2:])):
        hit = idx.get(key)
        if hit and len(set(hit)) == 1:
            return hit[0]
    return None


@check("DQ001", "Quality check failed (Great Expectations / Soda)", stage="continuous", cost="statistical")
def dq_failures(ctx: Context):
    if not ctx.dq_results or ctx.project is None:
        return []
    idx, out = _index(ctx.project), []
    for r in ctx.dq_results:
        if r.status == "pass":
            continue
        uid = _match(idx, r.table)
        down = len(ctx.project.downstream(uid)) if uid else 0
        where = f"{r.table}.{r.column}" if r.column else r.table or "?"
        sev = "medium" if r.status == "warn" or r.severity in ("warning", "info") else ("critical" if down >= 3 or r.severity == "critical" else "high")
        tool = "Great Expectations" if r.tool == "great_expectations" else "Soda"
        verb = tr({"fail": N_("failed"), "warn": N_("is warning"), "error": N_("did not run")}[r.status])
        out.append(Finding("DQ001", sev, tr("{where}: {tool} `{check}` {status}", where=where, tool=tool, check=r.check, status=verb),
                           (r.detail + (" " + tr("Observed: {value}.", value=r.observed) if r.observed else "")
                            + (" " + tr("Affects {n} downstream model(s).", n=down) if down else "")).strip(),
                           [uid] if uid else [], "continuous",
                           {"tool": r.tool, "suite": r.suite, "status": r.status, "downstream": down, "observed": r.observed,
                            **({} if uid else {"unmapped_table": r.table})}))
    return out
