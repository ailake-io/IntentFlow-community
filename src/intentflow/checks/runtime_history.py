"""Checks over the orchestrators' run history (recurring failures, duration regression)."""
from __future__ import annotations

import statistics
from collections import defaultdict

from ..i18n import tr
from ..model import Finding
from . import Context, check


@check("RUN001", "Recurring failures", stage="continuous", cost="statistical")
def recurring_failures(ctx: Context):
    by = defaultdict(list)
    for r in ctx.runs:
        by[(r.system, r.pipeline)].append(r)
    out = []
    for (sys_, pipe), rs in by.items():
        rs.sort(key=lambda r: r.start or 0)
        last = [r.state for r in rs[-5:]]
        if len(rs) >= 3 and last[-3:] == ["failed"] * 3:
            out.append(Finding("RUN001", "high", tr("{pipeline} failed in the last 3 runs", pipeline=f"{sys_}:{pipe}"),
                               tr("Persistent failure; re-running does not fix it. Look at the first failure of the sequence."), [f"{sys_}:{pipe}"], "continuous",
                               {"states": last, "url": rs[-1].url}))
    return out


@check("RUN002", "Duration regression", stage="continuous", cost="statistical")
def duration_regression(ctx: Context):
    by = defaultdict(list)
    for r in ctx.runs:
        if r.state == "success" and r.duration:
            by[(r.system, r.pipeline)].append(r)
    out = []
    for (sys_, pipe), rs in by.items():
        rs.sort(key=lambda r: r.start or 0)
        if len(rs) < 6:
            continue
        base = statistics.median(r.duration for r in rs[:-1][-10:])
        if base > 0 and rs[-1].duration > 2 * base:
            out.append(Finding("RUN002", "medium", tr("{pipeline} became {x}× slower", pipeline=f"{sys_}:{pipe}", x=f"{rs[-1].duration/base:.1f}"),
                               tr("The last run took more than twice the median: risk of breaking the SLA and of piling up concurrent runs."),
                               [f"{sys_}:{pipe}"], "continuous", {"last_s": rs[-1].duration, "median_s": base}))
    return out


@check("RUN003", "SLA at risk", stage="continuous", cost="statistical")
def sla_risk(ctx: Context):
    """Policy `sla.<system>:<pipeline>` = {minutes}. Compares p90 and the last duration with the deadline."""
    out = []
    by = defaultdict(list)
    for r in ctx.runs:
        if r.duration and r.state in ("success", "failed"):
            by[f"{r.system}:{r.pipeline}"].append(r)
    for name, rs in by.items():
        sla = (ctx.policies.get(f"sla.{name}") or {}).get("minutes")
        if not sla:
            continue
        rs.sort(key=lambda r: r.start or 0)
        limit = float(sla) * 60
        ds = sorted(r.duration for r in rs[-20:])
        p90 = ds[min(len(ds) - 1, int(0.9 * len(ds)))]
        if rs[-1].duration > limit:
            out.append(Finding("RUN003", "high", tr("{pipeline}: last run ({minutes} min) broke the {sla} min SLA", pipeline=name,
                                                    minutes=f"{rs[-1].duration/60:.1f}", sla=f"{sla:g}"),
                               tr("Deadline already missed."), [name], "continuous", {"last_s": rs[-1].duration, "sla_s": limit}))
        elif p90 > 0.8 * limit:
            out.append(Finding("RUN003", "medium", tr("{pipeline}: p90 of {minutes} min uses {ratio} of the {sla} min SLA", pipeline=name,
                                                      minutes=f"{p90/60:.1f}", ratio=f"{p90/limit:.0%}", sla=f"{sla:g}"),
                               tr("The margin is small; any upstream delay breaks the deadline."), [name], "continuous", {"p90_s": p90, "sla_s": limit}))
    return out


@check("RUN004", "Pipeline without a recent run", stage="continuous", cost="statistical")
def stale(ctx: Context):
    """Policy `max_age_hours.<system>:<pipeline>` (number) — last successful run older than that."""
    import time
    out = []
    now = time.time()
    by = defaultdict(list)
    for r in ctx.runs:
        by[f"{r.system}:{r.pipeline}"].append(r)
    for name, rs in by.items():
        max_age = ctx.policies.get(f"max_age_hours.{name}")
        if not max_age:
            continue
        ok = [r.end or r.start or 0 for r in rs if r.state == "success"]
        age = (now - max(ok)) / 3600 if ok else None
        if age is None or age > float(max_age):
            title = (tr("{pipeline}: no success for more than {hours} h", pipeline=name, hours=format(max_age, "g")) if age is None
                     else tr("{pipeline}: no success for {hours} h", pipeline=name, hours=f"{age:.1f}"))
            out.append(Finding("RUN004", "high", title,
                               tr("Downstream data is older than expected."), [name], "continuous", {"max_age_hours": max_age}))
    return out
