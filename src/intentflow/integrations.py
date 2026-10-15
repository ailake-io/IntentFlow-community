"""State of each integration for the UI: configured? has data? when was the last collection?

Only reads the store (settings, runs, samples): never opens a connection to any service.
"""
from __future__ import annotations

import time

from . import extensions
from .i18n import N_, tr

RUN_SYSTEMS = [
    ("airflow", "Airflow", "airflow.url"), ("kestra", "Kestra", "kestra.url"), ("prefect", "Prefect", "prefect.url"),
    ("dagster", "Dagster", "dagster.url"), ("dbt_cloud", "dbt Cloud", "dbt_cloud.account_id"), ("airbyte", "Airbyte", "airbyte.url"),
    ("fivetran", "Fivetran", "fivetran.url"), ("spark", "Spark", "spark.url"), ("databricks", "Databricks Jobs", "databricks.jobs_host"),
    ("argo", "Argo Workflows", "argo.url"), ("mage", "Mage", "mage.url"), ("kafka_connect", "Kafka Connect", "kafka_connect.url"),
    ("flink", "Flink", "flink.url"), ("luigi", "Luigi", "luigi.url"), ("mlflow", "MLflow", "mlflow.url"), ("rundeck", "Rundeck", "rundeck.url"),
    ("glue", "AWS Glue", "aws.glue"), ("stepfunctions", "AWS Step Functions", "aws.stepfunctions"), ("athena", N_("AWS Athena (queries)"), "aws.athena"),
    ("adf", "Azure Data Factory", "azure.data_factory"),
    ("openlineage", N_("OpenLineage (received)"), None),
]
SAMPLE_SOURCES = [
    ("kubernetes", "Kubernetes", "kubernetes.enabled"), ("prometheus", "Prometheus", "prometheus.url"), ("kafka", N_("Kafka (lag)"), "kafka.bootstrap"),
    ("spark", N_("Spark (memory)"), "spark.url"), ("docker", "Docker", None), ("host", N_("Local machine"), None), ("warehouse", N_("Warehouse (spill/OOM)"), None),
]


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "—"
    d = max(0, now - ts)
    return (tr("now") if d < 90 else tr("{n} min ago", n=int(d // 60)) if d < 5400 else tr("{n} h ago", n=int(d // 3600)) if d < 172800
            else tr("{n} d ago", n=int(d // 86400)))


def build(store, now: float | None = None) -> dict:
    now = now or time.time()
    get = store.get_setting
    cards: list[dict] = []

    def card(group, id_, name, configured, state, summary, keys=(), detail=None):
        cards.append({"group": group, "id": id_, "name": name, "configured": bool(configured), "state": state, "summary": summary,
                      "keys": list(keys), "detail": detail or {}})

    # ---- runs
    g_runs, g_mem, g_data = tr("Runs"), tr("Cluster and memory"), tr("Data and quality")
    off = tr("Not configured")
    runs = store.runs(2000)
    by: dict[str, list[dict]] = {}
    for r in runs:
        by.setdefault(r["system"], []).append(r)
    for sid, name, key in RUN_SYSTEMS:
        rs = by.get(sid, [])
        configured = bool(get(key)) if key else bool(rs)
        if not configured and not rs:
            card(g_runs, sid, tr(name), False, "off", off, [key] if key else [])
            continue
        failed = sum(r["state"] == "failed" for r in rs)
        last = max([r.get("end") or r.get("start") or 0 for r in rs], default=0)
        state = "warn" if (not rs or failed and failed == len(rs)) else "ok"
        card(g_runs, sid, tr(name), True, state,
             tr("{n} run(s), {failed} failed · last {ago}", n=len(rs), failed=failed, ago=_ago(last, now)) if rs else tr("Configured, no runs collected yet"),
             [key] if key else [], {"runs": len(rs), "failed": failed, "last": last})

    # ---- memory / resource samples
    samples = store.recent_samples(now - 24 * 3600)
    sb: dict[str, list] = {}
    for s in samples:
        sb.setdefault(s.source, []).append(s)
    for sid, name, key in SAMPLE_SOURCES:
        ss = sb.get(sid, [])
        configured = bool(get(key)) if key else bool(ss)
        if not configured and not ss:
            card(g_mem, sid, tr(name), False, "off", off, [key] if key else [])
            continue
        oom = sum(1 for s in ss if s.metric == "oom_killed" and s.value > 0)
        lag = max([s.value for s in ss if s.metric == "consumer_lag"], default=None)
        parts = [tr("{n} sample(s) in the last 24 h", n=len(ss))] + ([f"{oom} OOM"] if oom else []) + ([tr("max lag {n}", n=int(lag))] if lag is not None else [])
        card(g_mem, sid, tr(name), True, "warn" if oom or not ss else "ok",
             " · ".join(parts) if ss else tr("Configured, no samples"), [key] if key else [], {"samples": len(ss), "oom": oom, "lag": lag})

    # ---- warehouse
    wt = get("warehouse.type") or ("duckdb" if get("warehouse.duckdb") else "postgres" if get("warehouse.postgres_env") else None)
    card(g_data, "warehouse", tr("Warehouse (probes, plan, diff)"), bool(wt), "ok" if wt else "off",
         tr("Type: {type}", type=wt) if wt else off, ["warehouse.type", "warehouse.duckdb", "warehouse.postgres_env"])
    from . import contracts as _ct, dq_results as _dq, lakehouse as _lh
    n_dq = len(_dq.load_all(get("dq.results"))) if get("dq.results") else 0
    card(g_data, "dq", "Great Expectations / Soda", bool(get("dq.results")), "ok" if n_dq else ("warn" if get("dq.results") else "off"),
         tr("{n} result(s) read", n=n_dq) if get("dq.results") else off, ["dq.results"])
    n_ct = len(_ct.load_all(get("contracts.dir"))) if get("contracts.dir") else 0
    card(g_data, "contracts", tr("Data contracts"), bool(get("contracts.dir")), "ok" if n_ct else ("warn" if get("contracts.dir") else "off"),
         tr("{n} contract(s)", n=n_ct) if get("contracts.dir") else off, ["contracts.dir"])
    card(g_data, "elementary", "Elementary", bool(get("elementary.schema")), "ok" if get("elementary.schema") else "off",
         f"Schema: {get('elementary.schema')}" if get("elementary.schema") else off, ["elementary.schema"])
    lk = _lh.tables(get("lakehouse.tables"))
    card(g_data, "lakehouse", "Delta Lake / Iceberg", bool(lk), "ok" if lk else "off",
         tr("{n} table(s)", n=len(lk)) if lk else off, ["lakehouse.tables"])
    card(g_data, "lint", tr("SQL lint (SQLFluff)"), bool(get("lint.enabled")), "ok" if get("lint.enabled") else "off",
         tr("On") if get("lint.enabled") else tr("Off (it is slow; turn it on in Settings)"), ["lint.enabled"])
    nct = len(get("state.catalog.cur") or {})
    card(g_data, "catalogs", tr("Catalogs (Glue / Unity)"), bool(get("catalog.glue") or get("catalog.unity")),
         "ok" if nct else ("warn" if get("catalog.glue") or get("catalog.unity") else "off"),
         tr("{n} table(s) observed", n=nct) if get("catalog.glue") or get("catalog.unity") else off, ["catalog.glue", "catalog.unity"])
    card(g_data, "schema_registry", "Kafka Schema Registry", bool(get("kafka.schema_registry")),
         "ok" if get("state.kafka_schemas") else ("warn" if get("kafka.schema_registry") else "off"),
         tr("{n} subject(s) observed", n=len(get("state.kafka_schemas") or {})) if get("kafka.schema_registry") else off, ["kafka.schema_registry"])

    extensions.each("integration_cards", store, card)      # e.g. AI (Enterprise)

    # ---- alerts
    sinks = get("notify.sinks", []) or []
    routes = get("notify.routes", []) or []
    card(tr("Alerts"), "notify", tr("Alert outputs"), bool(sinks), "ok" if sinks and routes else ("warn" if sinks else "off"),
         tr("{n} output(s): {names} · {routes} route(s)", n=len(sinks), names=", ".join(f"{s.get('name')} ({s.get('type')})" for s in sinks[:6]), routes=len(routes))
         if sinks else tr("No output configured"),
         ["notify.sinks", "notify.routes"], {"sinks": [{"name": s.get("name"), "type": s.get("type")} for s in sinks]})

    # ---- project and security
    tgt = get("dbt.target")
    card(tr("Project"), "target", tr("Transformation project (dbt/SQLMesh)"), bool(tgt or get("dbt_cloud.job_id")), "ok" if tgt or get("dbt_cloud.job_id") else "off",
         tr("Target: {target}", target=tgt) if tgt else (tr("dbt Cloud artifacts (job {job})", job=get("dbt_cloud.job_id")) if get("dbt_cloud.job_id") else off),
         ["dbt.target", "dbt_cloud.job_id"])
    bx = store.box.status()
    g_sec = tr("Security")
    card(g_sec, "secrets", tr("Secrets encrypted at rest"), bx["encrypted_at_rest"], "ok" if bx["encrypted_at_rest"] else "warn",
         tr("Encrypted (key: {source})", source=bx["key_source"]) if bx["encrypted_at_rest"] else tr("TOTP/SSO secrets in plain text: set INTENTFLOW_SECRET_KEY"),
         [tr("INTENTFLOW_SECRET_KEY (environment)")], bx)
    users = store.users_admin() if store.has_users() else []
    card(g_sec, "team", tr("Team and login"), bool(users), "ok" if users else "off",
         tr("{n} person(s), {mfa} with MFA", n=len(users), mfa=sum(1 for u in users if u.get("mfa"))) + (" · " + tr("SSO configured") if get("sso.issuer") else "") if users
         else tr("Local mode without login: add users before exposing it on a network"), [], {"sso": bool(get("sso.issuer"))})
    card(g_sec, "store", tr("Store"), True, "ok", f"{store.backend} " + (tr("(production)") if store.backend == "postgres" else tr("(local file)")), [])

    groups: dict[str, list[dict]] = {}
    for c in cards:
        groups.setdefault(c["group"], []).append(c)
    on = sum(1 for c in cards if c["configured"])
    return {"groups": [{"name": g, "cards": cs} for g, cs in groups.items()], "configured": on, "total": len(cards),
            "warnings": sum(1 for c in cards if c["configured"] and c["state"] == "warn")}
