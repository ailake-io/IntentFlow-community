"""Kafka: growing consumer lag and incompatible schema changes (Schema Registry)."""
from __future__ import annotations

from ..i18n import tr
from ..model import Finding
from . import Context, check

DEFAULT_MAX_LAG = 10_000


def _topic_node(ctx, subject: str):
    """subject `orders-value` / `orders-key` → the node named after the topic (source or model)."""
    if ctx.project is None:
        return None
    topic = subject.rsplit("-", 1)[0] if subject.endswith(("-value", "-key")) else subject
    for uid, n in ctx.project.nodes.items():
        if n.name.lower() == topic.lower():
            return uid
    return None


@check("KAF001", "High and growing Kafka consumer lag", stage="continuous", cost="statistical")
def lag(ctx: Context):
    limit = float(ctx.policies.get("kafka_lag_max", DEFAULT_MAX_LAG) or DEFAULT_MAX_LAG)
    series: dict[str, list] = {}
    for s in sorted(ctx.samples, key=lambda x: x.ts):
        if s.source == "kafka" and s.metric == "consumer_lag":
            series.setdefault(s.scope, []).append(s)
    out = []
    for scope, v in series.items():
        last = v[-1].value
        rising = len(v) < 2 or last > v[0].value
        if last >= limit and rising:
            out.append(Finding("KAF001", "high" if last >= 10 * limit else "medium", tr("{scope}: lag of {n} messages", scope=scope, n=int(last)),
                               tr("The consumer does not keep up with production; data reaches the warehouse late and the lag can become loss through retention."),
                               [f"kafka:{scope}"], "continuous", {"lag": last, "limit": limit, "samples": len(v),
                                                                  "recommendation": tr("scale the consumer, check reprocessing/errors and the processing time per batch")}))
    return out


@check("KAF002", "Incompatible schema change in the Schema Registry", stage="continuous", cost="static")
def schema_change(ctx: Context):
    snap = (ctx.store.get_setting("state.kafka_schemas") if ctx.store is not None else None) or {}
    out = []
    for subject, vs in snap.items():
        if len(vs) < 2 or not vs[-1].get("fields") or not vs[-2].get("fields"):
            continue
        old, new = vs[-2]["fields"], vs[-1]["fields"]
        removed = sorted(set(old) - set(new))
        retyped = sorted(c for c in set(old) & set(new) if old[c] != new[c])
        if not removed and not retyped:
            continue
        uid = _topic_node(ctx, subject)
        down = len(ctx.project.downstream(uid)) if uid and ctx.project else 0
        what = [tr("`{field}` removed", field=c) for c in removed] + [f"`{c}`: {old[c]} → {new[c]}" for c in retyped]
        out.append(Finding("KAF002", "critical" if down else "high",
                           tr("{subject}: v{old} → v{new} breaks consumers ({changes})", subject=subject, old=vs[-2]["version"], new=vs[-1]["version"],
                              changes="; ".join(what)),
                           tr("Incompatible change in the topic contract.") + (" " + tr("Affects {n} downstream model(s).", n=down) if down else ""),
                           [uid] if uid else [f"kafka:{subject}"], "continuous",
                           {"removed": removed, "retyped": retyped, "downstream": down, "from": vs[-2]["version"], "to": vs[-1]["version"]}))
    return out
