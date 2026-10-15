"""Memory pressure and resource saturation (samples from the cluster, orchestrators and warehouse)."""
from __future__ import annotations

from collections import defaultdict

from ..i18n import tr
from ..model import Finding
from ..monitoring import time_to_exhaustion
from . import Context, check


def _series(ctx: Context):
    s = defaultdict(list)
    for x in ctx.samples:
        s[(x.source, x.scope, x.metric)].append(x)
    for v in s.values():
        v.sort(key=lambda x: x.ts)
    return s


@check("CLU001", "High memory use", stage="continuous", cost="statistical")
def memory_high(ctx: Context):
    out = []
    for (src, scope, metric), v in _series(ctx).items():
        if metric != "memory_bytes" or not v[-1].limit:
            continue
        ratio = v[-1].value / v[-1].limit
        if ratio >= 0.7:
            sev = "critical" if ratio >= 0.9 else "high" if ratio >= 0.8 else "medium"
            out.append(Finding("CLU001", sev, tr("{where} uses {ratio} of its memory", where=f"{src}:{scope}", ratio=f"{ratio:.0%}"),
                               tr("Above 70% the chance of an OOM kill goes up (aggregation/join peaks)."), [f"{src}:{scope}"], "continuous",
                               {"used_bytes": v[-1].value, "limit_bytes": v[-1].limit,
                                "recommendation": tr("raise the limit, reduce parallelism/pool or optimize the queries that use the most")}))
    return out


@check("CLU002", "Memory exhaustion trend", stage="continuous", cost="statistical")
def memory_trend(ctx: Context):
    out = []
    for (src, scope, metric), v in _series(ctx).items():
        if metric != "memory_bytes" or not v[-1].limit or len(v) < 4:
            continue
        eta = time_to_exhaustion([(x.ts, x.value) for x in v], v[-1].limit)
        if eta is not None and eta < 24 * 3600 and v[-1].value / v[-1].limit < 0.9:
            out.append(Finding("CLU002", "high" if eta < 3600 else "medium",
                               tr("{where} runs out of memory in ~{hours} h", where=f"{src}:{scope}", hours=f"{eta/3600:.1f}"),
                               tr("Continuous growth (leak or runs piling up). Linear projection over the recent samples."),
                               [f"{src}:{scope}"], "continuous", {"eta_seconds": round(eta), "samples": len(v)}))
    return out


@check("CLU003", "Process killed for lack of memory", stage="continuous", cost="statistical")
def oom_killed(ctx: Context):
    return [Finding("CLU003", "critical", tr("{where} was killed by OOM", where=f"{src}:{scope}"),
                    tr("The kernel/container killed the process (OOMKilled). Running tasks were lost."), [f"{src}:{scope}"], "continuous",
                    {"recommendation": tr("raise the container memory limit or reduce the concurrent load")})
            for (src, scope, metric), v in _series(ctx).items() if metric == "oom_killed" and v[-1].value > 0]


@check("CLU004", "Queries spilling to disk", stage="continuous", cost="statistical")
def spill(ctx: Context):
    out = []
    for (src, scope, metric), v in _series(ctx).items():
        if metric == "spilled_bytes" and v[-1].value > 0:
            gb = v[-1].value / 1e9
            out.append(Finding("CLU004", "high" if gb >= 10 else "medium", tr("query {query} spilled {gb} GB to disk", query=scope, gb=f"{gb:.1f}"),
                               tr("Spill means the query did not fit in the warehouse/cluster memory: slow and an OOM candidate."),
                               [scope], "continuous", {"spilled_bytes": v[-1].value,
                                                       "recommendation": tr("filter before joining, aggregate first, read fewer columns or grow the warehouse")}))
    return out


@check("CLU005", "Saturated pool/worker", stage="continuous", cost="statistical")
def saturation(ctx: Context):
    out = []
    for (src, scope, metric), v in _series(ctx).items():
        if metric == "queued" and len(v) >= 2 and all(x.value > 0 for x in v[-3:]):
            out.append(Finding("CLU005", "medium", tr("{where} has tasks queued continuously", where=f"{src}:{scope}"),
                               tr("Tasks wait for a slot across several samples in a row: SLA at risk and backfills piling up."), [f"{src}:{scope}"], "continuous",
                               {"queued": v[-1].value}))
    return out
