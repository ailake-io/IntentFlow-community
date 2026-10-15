"""Schema change in catalogs (Glue/Unity) between two collections: removed column, changed type, dropped table."""
from __future__ import annotations

from ..i18n import tr
from ..model import Finding
from . import Context, check


@check("CAT001", "Schema changed in the data catalog", stage="continuous", cost="static")
def catalog_changes(ctx: Context):
    if ctx.store is None:
        return []
    cur, prev = ctx.store.get_setting("state.catalog.cur"), ctx.store.get_setting("state.catalog.prev")
    if not cur or not prev:
        return []
    names: dict[str, str] = {}
    if ctx.project is not None:
        for uid, n in ctx.project.nodes.items():
            if n.relation:
                parts = [p.strip('"`[]').lower() for p in n.relation.split(".")]
                names[".".join(parts[-2:])] = uid
                names[parts[-1]] = names.get(parts[-1], uid)
    out = []
    for fqn, old in prev.items():
        kind, _, name = fqn.partition(":")
        short = ".".join(name.split(".")[-2:])
        uid = names.get(short) or names.get(name.split(".")[-1])
        down = len(ctx.project.downstream(uid)) if uid and ctx.project else 0
        ref = [uid] if uid else [f"catalog:{fqn}"]
        new = cur.get(fqn)
        if new is None:
            out.append(Finding("CAT001", "critical" if down else "high", tr("{table}: table disappeared from the catalog ({catalog})", table=name, catalog=kind),
                               tr("Consumers of this table will fail.") + (" " + tr("Affects {n} downstream model(s).", n=down) if down else ""),
                               ref, "continuous", {"kind": "table_dropped", "catalog": kind, "downstream": down}))
            continue
        removed = sorted(set(old) - set(new))
        retyped = sorted(c for c in set(old) & set(new) if old[c] != new[c])
        if removed or retyped:
            what = [tr("`{column}` removed", column=c) for c in removed] + [f"`{c}`: {old[c]} → {new[c]}" for c in retyped]
            out.append(Finding("CAT001", "critical" if down else "high", tr("{table}: schema changed ({changes})", table=name, changes="; ".join(what)),
                               tr("Incompatible change observed in the catalog.") + (" " + tr("Affects {n} downstream model(s).", n=down) if down else ""),
                               ref, "continuous", {"kind": "schema_break", "catalog": kind, "removed": removed, "retyped": retyped, "downstream": down}))
    return out
