"""Warehouse probes (read-only): duplicated keys and volume anomalies."""
from __future__ import annotations

import statistics
import time

from ..i18n import tr
from ..model import Finding
from . import Context, check


def _rel(n):
    return n.relation


@check("PRB001", "Duplicated key (probe)", stage="pre-run", cost="probe")
def duplicate_keys(ctx: Context):
    if ctx.warehouse is None:
        return []
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type not in ("model", "seed") or not n.relation:
            continue
        for t in n.tests:
            if t["name"] != "unique" or not t.get("column"):
                continue
            col = t["column"]
            try:
                d = ctx.warehouse.scalar(f'select count(*) - count(distinct "{col}") from {n.relation} where "{col}" is not null')
            except Exception as e:
                out.append(Finding("PRB001", "info", tr("{column}: probe failed", column=f"{n.name}.{col}"), str(e)[:160], [uid], "pre-run"))
                continue
            if d and int(d) > 0:
                out.append(Finding("PRB001", "critical" if ctx.project.downstream(uid) else "high",
                                   tr("{column}: {n} duplicated row(s) on the key", column=f"{n.name}.{col}", n=int(d)),
                                   tr("The key should be unique; consumers that join on it will multiply rows."), [uid] + ctx.project.downstream(uid)[:10],
                                   "pre-run", {"duplicates": int(d)}))
    return out


@check("PRB002", "Volume anomaly (probe)", stage="pre-run", cost="probe")
def volume_anomaly(ctx: Context):
    if ctx.warehouse is None or ctx.store is None:
        return []
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type not in ("model", "seed") or not n.relation:
            continue
        try:
            now_rows = int(ctx.warehouse.scalar(f"select count(*) from {n.relation}"))
        except Exception:
            continue
        hist = [v for _, v in ctx.store.history(f"rows:{uid}")][-20:]
        ctx.store.add_history(f"rows:{uid}", time.time(), now_rows)
        if len(hist) < 3:
            continue
        med = statistics.median(hist)
        if med > 0 and (now_rows < 0.5 * med or now_rows > 2 * med):
            out.append(Finding("PRB002", "high" if now_rows < 0.5 * med else "medium",
                               tr("{model}: {rows} rows vs median {median}", model=n.name, rows=now_rows, median=int(med)),
                               tr("Volume outside the historical pattern; partial or duplicated load or a broken upstream filter."),
                               [uid] + ctx.project.downstream(uid)[:10], "pre-run", {"rows": now_rows, "median": med, "history": len(hist)}))
    return out
