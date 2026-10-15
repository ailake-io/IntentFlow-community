"""Diff between two versions of the project/catalog: removed column, changed type, likely rename."""
from __future__ import annotations

import difflib

from ..i18n import tr
from ..model import Finding
from . import Context, check


def diff_columns(base, head):
    """Yield (uid, removed, added, changed) per node present in both versions (matched by relation name)."""
    head_by_rel = {n.relation: u for u, n in head.nodes.items() if n.relation}
    for buid, bn in base.nodes.items():
        huid = head_by_rel.get(bn.relation)
        if not huid:
            continue
        hn = head.nodes[huid]
        bc, hc = base.actual_columns(buid), head.actual_columns(huid)
        if not bc or not hc:
            continue      # node without catalog in one of the versions: nothing to compare (avoids a false "removed")
        b, h = set(bc), set(hc)
        changed = {c: (bc[c].type, hc[c].type) for c in b & h
                   if bc[c].type and hc[c].type and bc[c].type.lower() != hc[c].type.lower()}
        yield buid, huid, sorted(b - h), sorted(h - b), changed


def _rename_guess(old: str, candidates: list[str], bn, hn) -> tuple[str | None, float]:
    best, score = None, 0.0
    for c in candidates:
        s = difflib.SequenceMatcher(None, old, c).ratio()
        if bn.columns[old].type and bn.columns[old].type.lower() == hn.columns[c].type.lower():
            s += 0.25      # same type strengthens the guess
        if s > score:
            best, score = c, s
    return (best, min(score, 1.0)) if score >= 0.6 else (None, score)


@check("SCH001", "Schema change with impact", stage="pre-merge", emits=("SCH002", "SCH003"))
def schema_drift(ctx: Context):
    if ctx.base_project is None or ctx.base_lineage is None:
        return []
    out = []
    blind = [w for w, pr in (("base", ctx.base_project), ("head", ctx.project))
             if pr.nodes and not any(pr.actual_columns(u) for u in pr.nodes)]
    if blind:      # without catalog.json the diff compares nothing: say so instead of passing silently
        out.append(Finding("SCH003", "medium", tr("Schema diff is blind: no catalog in {side}", side="+".join(blind)),
                           tr("Run `dbt docs generate` in each version so target/catalog.json exists; without it removed or renamed columns are not detected."),
                           [], "pre-merge", {"missing_catalog": blind}))
    for buid, huid, removed, added, changed in diff_columns(ctx.base_project, ctx.project):
        bn, hn = ctx.base_project.nodes[buid], ctx.project.nodes[huid]
        for col in removed:
            consumers = ctx.base_lineage.downstream_of(buid, col)
            indirect = ctx.base_lineage.indirect_of(buid, col) if hasattr(ctx.base_lineage, "indirect_of") else []
            impacted = sorted({e.dst for e in consumers} | {d for d, _ in indirect})
            guess, conf = _rename_guess(col, added, bn, hn)
            if not impacted and not guess:
                continue
            fix = None
            if guess:
                fix = {"kind": "rename_column", "node": bn.name, "old": col, "new": guess,
                       "targets": [ctx.base_project.nodes[u].name for u in impacted]}
            sev = "critical" if impacted else "low"
            from ..lineage_api import impact      # transitive blast radius (direct consumers break; the ones behind, in cascade)
            blast = [r["name"] for r in impact(ctx.base_project, ctx.base_lineage, bn.name, col)["affected"] if r["name"] != bn.name] if impacted else []
            if impacted:
                detail = tr("{n} downstream model(s) read this column and will fail.", n=len(impacted))
                if indirect:
                    detail += " " + tr("{n} only use it in JOIN/filter/grouping.", n=len(indirect))
                if len(blast) > len(impacted):
                    detail += " " + tr("Transitive impact: {models}.", models=", ".join(blast))
            else:
                detail = tr("No known consumer.")
            title = (tr("{column} removed (renamed to {new}?)", column=f"{bn.name}.{col}", new=guess) if guess
                     else tr("{column} removed", column=f"{bn.name}.{col}"))
            out.append(Finding("SCH001", sev, title, detail, [huid] + [u for u in impacted],
                               "pre-merge", {"column": col, "rename_candidate": guess, "rename_confidence": round(conf, 2),
                                             "consumers": [(ctx.base_project.nodes[e.dst].name, e.dst_col) for e in consumers],
                                             "indirect_consumers": [(ctx.base_project.nodes[d].name, via) for d, via in indirect],
                                             "downstream_total": blast},
                               fix, confidence=conf if guess else 1.0))
        for col, (t0, t1) in changed.items():
            consumers = ctx.base_lineage.downstream_of(buid, col)
            ind = ctx.base_lineage.indirect_of(buid, col) if hasattr(ctx.base_lineage, "indirect_of") else []
            out.append(Finding("SCH002", "high" if consumers or ind else "low", tr("{column}: type {old} → {new}", column=f"{bn.name}.{col}", old=t0, new=t1),
                               tr("A type change can truncate, lose precision or break casts and joins downstream."),
                               [huid] + sorted({e.dst for e in consumers} | {d for d, _ in ind}), "pre-merge",
                               {"column": col, "from": t0, "to": t1}))
    return out
