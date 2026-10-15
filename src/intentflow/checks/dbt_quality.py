"""Checks on the dbt project: tests, idempotency, owner, PII propagation."""
from __future__ import annotations

from ..i18n import tr
from ..model import Finding
from . import Context, check


def _sev_by_impact(n_down: int, base: str = "medium") -> str:
    return "high" if n_down >= 3 else base


@check("DBT001", "Key without a uniqueness test")
def missing_key_tests(ctx: Context):
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type != "model" or not ctx.project.downstream(uid):
            continue   # without consumers the risk is lower
        has_unique = any(t["name"] in ("unique", "unique_combination_of_columns") for t in n.tests)
        if not has_unique:
            out.append(Finding("DBT001", _sev_by_impact(len(ctx.project.downstream(uid))),
                               tr("{model}: no uniqueness test", model=n.name),
                               tr("A model consumed by others declares no `unique` test; a duplicated key spreads through joins and inflates metrics."),
                               [uid], "pre-merge", {"downstream": len(ctx.project.downstream(uid)),
                                                    "recommendation": tr("add `unique` and `not_null` on the primary key")}))
    return out


@check("DBT002", "Incremental without unique_key")
def incremental_idempotency(ctx: Context):
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type == "model" and n.materialized == "incremental" and not n.unique_key \
                and n.incremental_strategy != "microbatch":
            out.append(Finding("DBT002", "high", tr("{model}: incremental without unique_key", model=n.name),
                               tr("Reprocessing a window (retry, backfill) inserts the same rows again: the result stops being idempotent."),
                               [uid], "pre-merge", {"recommendation": tr("set `unique_key` and the `merge`/`delete+insert` strategy"),
                                                    "key_candidates": [c for c in n.columns if c.lower() == "id" or c.lower().endswith("_id")][:6]}))
    return out


@check("DBT003", "Model without owner")
def missing_owner(ctx: Context):
    out = []
    for uid, n in ctx.project.nodes.items():
        if n.resource_type == "model" and not (n.meta.get("owner") or n.meta.get("team")):
            down = len(ctx.project.downstream(uid))
            out.append(Finding("DBT003", "medium" if down else "low", tr("{model}: no owner", model=n.name),
                               tr("Without `meta.owner`, alerts and fix proposals have no one to be routed to."),
                               [uid], "continuous", {"downstream": down}))
    return out


def _pii_columns(project):
    for uid, n in project.nodes.items():
        for c in n.columns.values():
            if c.meta.get("pii") or "pii" in c.tags:
                yield uid, c.name


@check("DBT004", "PII propagated without masking")
def pii_propagation(ctx: Context):
    """Propagate PII columns through the lineage; report arrival at a non-authorized model without hash/mask."""
    if ctx.lineage is None:
        return []
    allowed = set(ctx.policies.get("pii_allowed_models", []))
    out, reported = [], set()
    for src_uid, src_col in _pii_columns(ctx.project):
        frontier = [(src_uid, src_col, False)]
        seen = set()
        while frontier:
            uid, col, masked = frontier.pop()
            for e in ctx.lineage.downstream_of(uid, col):
                m = masked or e.masked
                key = (e.dst, e.dst_col, m)
                if key in seen:
                    continue
                seen.add(key)
                dst = ctx.project.nodes[e.dst]
                ok = dst.name in allowed or dst.meta.get("contains_pii") is True
                if not m and not ok and (e.dst, e.dst_col) not in reported:
                    reported.add((e.dst, e.dst_col))
                    origin = f"{ctx.project.nodes[src_uid].name}.{src_col}"
                    out.append(Finding("DBT004", "high", tr("{column}: PII from {origin} without masking", column=f"{dst.name}.{e.dst_col}", origin=origin),
                                       tr("A column marked as PII reaches a model that neither declares `contains_pii: true` nor goes through a hash/mask."),
                                       [e.dst], "pre-merge",
                                       {"origin": origin,
                                        "recommendation": tr("apply md5/sha256 or drop the column; or list the model in `pii_allowed_models`")}))
                frontier.append((e.dst, e.dst_col, m))
    return out
