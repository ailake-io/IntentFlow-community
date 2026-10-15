"""What can break in the whole process, stated plainly: visibility gaps, fragile points and failures of the process itself.

Each item says **what** it is, **what breaks** (consequence) and **how to fix it**. It only reads the project, the lineage and the store.
Areas and severities are stable English keys (`visibility`, `fragility`, `external_jobs`, `process`; `high`, `medium`, `low`);
the text is translated.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import external as ext
from .i18n import N_, tr

SEV = {"high": 0, "medium": 1, "low": 2}
AREAS = {"visibility": N_("Visibility"), "fragility": N_("Fragility"), "external_jobs": N_("External jobs"), "process": N_("Process")}


def _item(area, sev, what, breaks, fix, count=None, examples=None):
    return {"area": area, "severity": sev, "what": what, "breaks": breaks, "fix": fix, "count": count, "examples": (examples or [])[:8]}


def _down_models(proj, res, uid, col, limit=5000):
    """Models reached downstream of a column (direct + indirect), without exposures."""
    seen, stack, models = {(uid, col)}, [(uid, col)], set()
    while stack and len(seen) < limit:
        u, c = stack.pop()
        for e in res.downstream_of(u, c):
            if (e.dst, e.dst_col) not in seen:
                seen.add((e.dst, e.dst_col))
                models.add(e.dst)
                stack.append((e.dst, e.dst_col))
        for d, _via in res.indirect_of(u, c):
            if d not in models:
                models.add(d)
                for dc in proj.nodes[d].columns:
                    if (d, dc) not in seen:
                        seen.add((d, dc))
                        stack.append((d, dc))
    return models


def build(proj, res, store=None, base=None, declared: str | None = None, reads=None, top: int = 10) -> dict:
    items: list[dict] = []
    models = {u: n for u, n in proj.nodes.items() if n.resource_type in ("model", "snapshot") and proj.in_root_package(u)}
    name = lambda u: proj.nodes[u].name
    pol = store.policies() if store is not None else {}

    # 1. visibility: where IntentFlow does not see, the impact disappears
    nocat = [name(u) for u in models if not proj.actual_columns(u)]
    if nocat:
        items.append(_item("visibility", "high", tr("{n} model(s) without a catalog (catalog.json)", n=len(nocat)),
                           tr("column lineage and removed columns are not detected in these models"),
                           tr("run `dbt docs generate` after the build and point `--target` at that `target/`"), len(nocat), nocat))
    unres: dict[str, int] = {}
    for u, c, _why in res.unresolved:
        if u in proj.nodes:
            unres[name(u)] = unres.get(name(u), 0) + 1
    if unres:
        items.append(_item("visibility", "medium", tr("{n} column(s) with unresolved lineage in {models} model(s)", n=sum(unres.values()), models=len(unres)),
                           tr("changing the source of these columns alerts no one (falls back to a whole-model dependency)"),
                           tr("avoid `select *` without a schema, declare the columns, or run `dbt docs generate`"),
                           sum(unres.values()), [f"{m} ({n})" for m, n in sorted(unres.items(), key=lambda x: -x[1])]))
    opaque = sorted({name(d) for _s, _c, d, via in res.indirect if via == "opaque" and d in proj.nodes})
    if opaque:
        items.append(_item("visibility", "medium", tr("{n} model(s) with only an opaque dependency", n=len(opaque)),
                           tr("the impact is on the whole model, not the column: too many or too few alarms"),
                           tr("SQL with `select *`, macros or table functions; make the columns explicit"), len(opaque), opaque))

    # 2. fragile points: columns with the largest blast radius and little protection
    ext_reads = reads or []
    cand = []
    for u in models:
        for c in proj.nodes[u].columns:
            n = len(res.downstream_of(u, c)) + len(res.indirect_of(u, c.lower()))
            if n:
                cand.append((n, u, c))
    cand.sort(reverse=True)
    frag = []
    for _n, u, c in cand[:200]:       # ponytail: only the 200 highest-degree columns; widen if projects have flatter graphs
        down = _down_models(proj, res, u, c)
        jobs = {r.job for uid, col, r in ext.resolve(proj, ext_reads) if uid == u and col in (c.lower(), "*")}
        n = proj.nodes[u]
        tested = any((t.get("column") or "").lower() == c.lower() for t in n.tests)
        frag.append({"model": n.name, "column": c, "downstream_models": len(down), "external_jobs": len(jobs), "owner": n.meta.get("owner"),
                     "tested": tested, "contract": n.contract_enforced})
    frag.sort(key=lambda x: -(x["downstream_models"] + 2 * x["external_jobs"]))
    weak = [f for f in frag if f["downstream_models"] + f["external_jobs"] >= 3 and not (f["tested"] and f["contract"])][:top]
    if weak:
        items.append(_item("fragility", "high", tr("{n} wide-reaching column(s) without a test or contract", n=len(weak)),
                           tr("a change in them breaks several consumers and nothing stops it in the PR"),
                           tr("add a test on the column and `contract: {enforced: true}` on the model (`DBT005`/`SCH001` start to block)"),
                           len(weak), [tr("{column} → {models} model(s), {jobs} external job(s)", column=f"{w['model']}.{w['column']}",
                                          models=w["downstream_models"], jobs=w["external_jobs"]) + ("" if w["owner"] else ", " + tr("no owner"))
                                       for w in weak]))
    noowner = [name(u) for u in models if not proj.nodes[u].meta.get("owner") and _down_models(proj, res, u, next(iter(proj.nodes[u].columns), ""))]
    if noowner:
        items.append(_item("fragility", "medium", tr("{n} model(s) with consumers and no owner", n=len(noowner)),
                           tr("no one is told when they break"), tr("set `meta.owner` (`DBT003` suggests one)"), len(noowner), noowner))

    # 3. external jobs (Spark/Python/notebooks)
    if ext_reads:
        um = ext.unmatched(proj, ext_reads)
        amb = {d: v for d, v in um.items() if v["reason"] == "ambiguous"}
        gone = {d: v for d, v in um.items() if v["reason"] == "missing"}
        if amb:
            items.append(_item("external_jobs", "high", tr("{n} dataset(s) read by external jobs are ambiguous", n=len(amb)),
                               tr("changes to the right model do not reach the job: the impact silently disappears"),
                               "`intentflow lineage --alias DATASET=MODEL`", len(amb), [f"{d} ← {', '.join(v['jobs'])}" for d, v in amb.items()]))
        if gone:
            items.append(_item("external_jobs", "low", tr("{n} dataset(s) read by external jobs without a matching model", n=len(gone)),
                               tr("normal for S3/external sources; if it should be a model, the impact does not reach the job"),
                               tr("`intentflow lineage --unmatched` (suggests the model) and `--alias`"), len(gone),
                               [f"{d} ← {', '.join(v['jobs'])}" for d, v in gone.items()]))
        cov = ext.coverage(ext_reads)
        if cov:
            items.append(_item("external_jobs", "medium", tr("{n} job(s) with conservative propagation (no known output column)", n=len(cov)),
                               tr("any change in one input alerts every reader of the output (too many alarms)"),
                               tr("declare `feeds`, use `sql_file`/`py_file` or emit `columnLineage`"), len(cov), [c["job"] for c in cov]))
        noown = sorted({r.job for r in ext_reads if not r.owner})
        if noown:
            items.append(_item("external_jobs", "medium", tr("{n} external job(s) without an owner", n=len(noown)),
                               tr("the `OL001` alert has no recipient"), tr("add `owner` in `intentflow.lineage.json`"), len(noown), noown))
        unnamed = sorted({r.job for r in ext_reads if r.job.rsplit(".", 1)[-1].lower() in ("pyspark_shell", "spark-shell")})
        if unnamed:
            items.append(_item("external_jobs", "low", tr("{n} Spark job(s) with the default name", n=len(unnamed)),
                               tr("different jobs show up as one"), tr("set `appName(...)`"), len(unnamed), unnamed))

    # 4. the process itself: what makes IntentFlow stop protecting
    if store is not None:
        if not store.get_setting("dbt.base_target") and base is None:
            items.append(_item("process", "high", tr("no `--base`/`dbt.base_target`"),
                               tr("the PR gate does not compare against `main`: removed columns and type changes get through"),
                               tr("build the `main` `target/` in CI and pass `--base`")))
        if declared and not Path(declared).is_file() and not store.ol_jobs():
            items.append(_item("process", "medium", tr("no source of external jobs (no `intentflow.lineage.json` and no OpenLineage events)"),
                               tr("Spark/Python/notebooks that read the project do not show up in the impact"),
                               tr("point OpenLineage at `intentflow openlineage serve` or create the declaration file")))
        old = [j["job"] for j in store.ol_jobs() if j["last_event"] and time.time() - j["last_event"] > 14 * 86400]
        if old:
            items.append(_item("process", "medium", tr("{n} OpenLineage job(s) without an event for more than 14 days", n=len(old)),
                               tr("the impact uses old reads: new or removed jobs are not reflected"), tr("check the emitter or remove the job"), len(old), old))
        runs = store.runs(200)
        failed = sorted({r["pipeline"] for r in runs if r["state"] == "failed"})
        if failed:
            items.append(_item("process", "medium", tr("{n} pipeline(s) with a recent failed run", n=len(failed)),
                               tr("stale data downstream; IntentFlow sees it but does not fix it"), tr("see `intentflow runs`"), len(failed), failed))
        off = list(pol.get("disabled_checks", []) or [])
        if off:
            items.append(_item("process", "medium", tr("{n} check(s) turned off by policy", n=len(off)),
                               tr("these problems are no longer reported"), tr("review `disabled_checks`"), len(off), off))
        if not store.recent_samples(time.time() - 7 * 86400):
            items.append(_item("process", "low", tr("no monitoring samples in the last 7 days"),
                               tr("volume/freshness anomalies are not detected (`intentflow monitor`)"), tr("schedule `intentflow monitor`")))
    items.sort(key=lambda i: (SEV[i["severity"]], -(i["count"] or 0)))
    return {"items": items, "weak_columns": frag[:top], "models": len(models), "columns": res.columns_total,
            "summary": {s: sum(1 for i in items if i["severity"] == s) for s in SEV}}


def to_markdown(r: dict) -> str:
    icon = {"high": "🔴", "medium": "🟠", "low": "⚪"}
    s = r["summary"]
    L = ["### " + tr("What can break"), "",
         tr("{models} model(s), {columns} column(s). High: **{high}** · medium: **{medium}** · low: **{low}**",
            models=r["models"], columns=r["columns"], high=s["high"], medium=s["medium"], low=s["low"]), ""]
    area = None
    for i in r["items"]:
        if i["area"] != area:
            area = i["area"]
            L += [f"#### {tr(AREAS[area])}", ""]
        L.append(f"- {icon[i['severity']]} **{i['what']}**")
        L.append("  - " + tr("breaks: {what}", what=i["breaks"]))
        L.append("  - " + tr("fix: {what}", what=i["fix"]))
        for e in i["examples"]:
            L.append(f"  - `{e}`")
    if not r["items"]:
        L.append(tr("Nothing found in the visibility, fragility and process checks."))
    return "\n".join(L)
