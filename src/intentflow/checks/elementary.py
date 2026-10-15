"""Elementary results (dbt observability package) read from the warehouse: `<schema>.elementary_test_results`.

Config: `elementary.schema` (e.g. `analytics_elementary`). Reuses the anomaly tests (volume, freshness, distribution) that
Elementary already computes instead of reimplementing them; IntentFlow adds downstream impact, owner and grouped incidents.
"""
from __future__ import annotations

from ..i18n import N_, tr
from ..model import Finding
from . import Context, check

COLS = ("test_unique_id", "model_unique_id", "table_name", "column_name", "test_name", "test_type", "test_sub_type",
        "status", "test_results_description", "severity", "detected_at")
BAD = {"fail": N_("failed"), "warn": N_("is warning"), "error": N_("did not run")}


@check("ELM001", "Failing Elementary test/anomaly", stage="continuous", cost="statistical")
def elementary_failures(ctx: Context):
    schema = ctx.store.get_setting("elementary.schema") if ctx.store is not None else None
    if not schema or ctx.warehouse is None:
        return []
    schema = str(schema).replace('"', "").replace(";", "")
    try:
        rows = ctx.warehouse.rows(f"select {', '.join(COLS)} from {schema}.elementary_test_results order by detected_at desc limit 2000")
    except Exception:                         # missing schema or no permission: do not take the run down
        return []
    latest: dict[str, dict] = {}
    for r in rows:                             # only the latest result of each test
        d = dict(zip(COLS, r))
        latest.setdefault(str(d["test_unique_id"]), d)
    by_uid = ctx.project.nodes if ctx.project is not None else {}
    out = []
    for d in latest.values():
        st = str(d["status"]).lower()
        if st not in BAD:
            continue
        uid = d["model_unique_id"] if d["model_unique_id"] in by_uid else next(
            (u for u, n in by_uid.items() if n.name == d["table_name"] or n.name == str(d["model_unique_id"]).split(".")[-1]), None)
        down = len(ctx.project.downstream(uid)) if uid else 0
        where = f"{d['table_name']}.{d['column_name']}" if d.get("column_name") else str(d["table_name"])
        anomaly = "anomaly" in str(d["test_type"]).lower() or "anomal" in str(d["test_name"]).lower()
        sev = "medium" if st != "fail" or str(d.get("severity")).lower() == "warn" else ("critical" if down >= 3 else "high")
        title = (tr("{where}: Elementary anomaly `{test}` {status}", where=where, test=d["test_name"], status=tr(BAD[st])) if anomaly
                 else tr("{where}: Elementary test `{test}` {status}", where=where, test=d["test_name"], status=tr(BAD[st])))
        out.append(Finding("ELM001", sev, title,
                           (str(d.get("test_results_description") or "") + (" " + tr("Affects {n} downstream model(s).", n=down) if down else "")).strip(),
                           [uid] if uid else [], "continuous",
                           {"test": d["test_name"], "test_type": d["test_type"], "sub_type": d["test_sub_type"], "status": st,
                            "downstream": down, "detected_at": str(d["detected_at"]), **({} if uid else {"unmapped_table": d["table_name"]})}))
    return out
