"""Jobs outside the project SQL (Spark, Python, notebooks): readers of a changed column, PII read, ambiguous datasets."""
from __future__ import annotations

from ..i18n import cat, tr
from ..lineage_api import impact
from ..model import Finding
from . import Context, check
from .schema_drift import diff_columns


@check("OL001", "External job reads a changed column", stage="pre-merge", emits=("OL002", "OL003", "OL004"))
def external_readers(ctx: Context):
    if not ctx.external:
        return []
    out = _ambiguous(ctx) + _pii(ctx)
    if ctx.base_project is None or ctx.base_lineage is None:
        return out
    for buid, _huid, removed, _added, changed in diff_columns(ctx.base_project, ctx.project):
        bn = ctx.base_project.nodes[buid]
        for col, what in [(c, tr("removed")) for c in removed] + [(c, tr("type {old} → {new}", old=t0, new=t1)) for c, (t0, t1) in changed.items()]:
            try:
                rows = impact(ctx.base_project, ctx.base_lineage, bn.name, col, external=ctx.external)["affected"]
            except ValueError:
                continue
            removed_col = col in removed
            for r in (x for x in rows if x.get("kind") == "job"):
                out.append(Finding("OL001", "critical" if removed_col else "high",
                                   _reads_title(r["name"], f"{bn.name}.{col}", what),
                                   (tr("External job ({source}) reads the output of job {via}, which reads this column.", source=r["source"], via=r["via_job"])
                                    if r.get("via_job") else
                                    tr("External job ({source}) reads this column or something derived from it and is not validated by dbt/SQLMesh.", source=r["source"])),
                                   [buid], "pre-merge",
                                   {"job": r["name"], "model": bn.name, "column": col, "change": "removed" if removed_col else "type", "source": r["source"],
                                    "reads": r["columns"], "writes": r["writes"], "owner": r["owner"], "via_job": r.get("via_job")}))
    return out


def _reads_title(job: str, column: str, what):
    return cat(tr("{job} reads {column}", job=job, column=column), " (", what, ")")


def _ambiguous(ctx: Context):
    """OL002: dataset read by a job that matches more than one model (the impact silently disappears); OL004: job without an application name."""
    from .. import external
    strict = bool(ctx.policies.get("external_warn_unmatched"))      # opt-in: a dataset without a matching node also becomes a warning
    out = []
    for ds, v in external.unmatched(ctx.project, ctx.external).items():
        ambiguous = v["reason"] == "ambiguous"
        if not ambiguous and not strict:
            continue
        why = (tr("More than one model ends with this name; the impact on the jobs that read it is not computed.") if ambiguous
               else tr("No model matches this name; project changes do not reach the jobs that read it."))
        out.append(Finding("OL002", "medium" if ambiguous else "low",
                           tr("dataset '{dataset}' is ambiguous in the project", dataset=ds) if ambiguous else tr("dataset '{dataset}' is missing from the project", dataset=ds),
                           why + " " + tr("Declare an alias: `intentflow lineage --alias DATASET=MODEL`."), [], "pre-merge",
                           {"dataset": ds, "jobs": v["jobs"], "reason": v["reason"], "suggestions": external.suggest(ctx.project, ds)}))
    for j in sorted({r.job for r in ctx.external if r.job.rsplit(".", 1)[-1].lower() in ("pyspark_shell", "spark-shell", "sparksession")}):
        out.append(Finding("OL004", "low", tr("job '{job}' without an application name", job=j),
                           tr("Spark used the default name; different jobs get mixed up. Set `appName(...)` (or `spark.app.name`)."),
                           [], "pre-merge", {"job": j}))
    return out


def _pii(ctx: Context):
    """An external job reads a PII column (or an unmasked derivative) and writes elsewhere: its masking cannot be verified from here."""
    from .. import external
    from .dbt_quality import _pii_columns
    if ctx.lineage is None:
        return []
    allowed = set(ctx.policies.get("pii_allowed_jobs", []))
    pii: dict[tuple[str, str], tuple[str, str]] = {}
    for u, c in _pii_columns(ctx.project):
        origin, frontier = f"{ctx.project.nodes[u].name}.{c}", [(u, c)]
        pii.setdefault((u, c), (origin, "direct"))
        while frontier:
            x = frontier.pop()
            for e in ctx.lineage.downstream_of(*x):
                if not e.masked and (e.dst, e.dst_col) not in pii:
                    pii[(e.dst, e.dst_col)] = (origin, "derived")
                    frontier.append((e.dst, e.dst_col))
    out, seen = [], set()
    for uid, col, r in external.resolve(ctx.project, ctx.external):
        hit = pii.get((uid, col))
        if not hit or r.job in allowed or (r.job, uid, col) in seen:
            continue
        seen.add((r.job, uid, col))
        out.append(Finding("OL003", "high", tr("{job} reads PII {column}", job=r.job, column=f"{ctx.project.nodes[uid].name}.{col}"),
                           tr("A job outside the project reads an unmasked PII column; its masking cannot be verified. Confirm it or list it in `pii_allowed_jobs`."),
                           [uid], "pre-merge", {"job": r.job, "model": ctx.project.nodes[uid].name, "column": col, "origin": hit[0],
                                                 "pii": hit[1], "writes": list(r.writes), "owner": r.owner, "source": r.source}))
    return out
