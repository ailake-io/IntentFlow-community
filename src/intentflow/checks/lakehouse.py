"""Health of Delta/Iceberg tables: small files (cost and latency) and schema evolving incompatibly."""
from __future__ import annotations

from .. import lakehouse
from ..i18n import tr
from ..model import Finding
from . import Context, check

SMALL_BYTES, SMALL_COUNT = 32 * 1024 * 1024, 100


@check("LAKE001", "Lakehouse table with small files or a breaking schema", stage="continuous", cost="statistical")
def lakehouse_health(ctx: Context):
    paths = lakehouse.tables(ctx.store.get_setting("lakehouse.tables") if ctx.store is not None else None)
    if not paths:
        return []
    small_b = float(ctx.policies.get("small_file_bytes", SMALL_BYTES))
    small_n = int(ctx.policies.get("small_file_count", SMALL_COUNT))
    nodes = ctx.project.nodes if ctx.project is not None else {}
    out = []
    for p in paths:
        try:
            t = lakehouse.inspect(p)
        except Exception as e:
            out.append(Finding("LAKE001", "low", tr("{path}: unreadable table ({error})", path=p, error=type(e).__name__), str(e)[:200], [], "continuous",
                               {"path": p, "kind": "unreadable"}))
            continue
        uid = next((u for u, n in nodes.items() if n.name == t["name"]), None)
        down = len(ctx.project.downstream(uid)) if uid else 0
        ref = [uid] if uid else [f"lake:{t['name']}"]
        if t["files"] >= small_n and t["avg_file_bytes"] < small_b:
            out.append(Finding("LAKE001", "medium", tr("{table}: {n} files averaging {mb} MB", table=t["name"], n=t["files"], mb=f"{t['avg_file_bytes'] / 1e6:.1f}"),
                               tr("Many small files raise the listing/open cost and the query latency."), ref, "continuous",
                               {"kind": "small_files", "files": t["files"], "avg_bytes": t["avg_file_bytes"], "format": t["format"],
                                "recommendation": tr("compact (OPTIMIZE in Delta; rewrite_data_files in Iceberg) and tune the target write size")}))
        old, new = t.get("prev_schema"), t["schema"]
        if old:
            removed = sorted(set(old) - set(new))
            retyped = sorted(c for c in set(old) & set(new) if old[c] != new[c])
            if removed or retyped:
                what = [tr("`{column}` removed", column=c) for c in removed] + [f"`{c}`: {old[c]} → {new[c]}" for c in retyped]
                out.append(Finding("LAKE001", "critical" if down else "high",
                                   tr("{table}: schema changed incompatibly ({changes})", table=t["name"], changes="; ".join(what)),
                                   tr("Consumers that read these columns break.") + (" " + tr("Affects {n} downstream model(s).", n=down) if down else ""), ref, "continuous",
                                   {"kind": "schema_break", "removed": removed, "retyped": retyped, "downstream": down, "format": t["format"]}))
    return out
