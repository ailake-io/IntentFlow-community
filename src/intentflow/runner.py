"""Runs a full check: used by the CLI, the UI and the scheduler. Reads sources and policies from the store."""
from __future__ import annotations

import time
from pathlib import Path

from . import dbt_loader, dbt_results, lineage as lin
from .checks import Context, run_checks
from .orchestrators import Run
from .store import Store
from .i18n import tr

SOURCE_KEYS = {
    "target": "dbt.target", "root": "dbt.root", "base": "dbt.base_target", "base_root": "dbt.base_root",
    "airflow_dags": "airflow.dags", "kestra_flows": "kestra.flows", "duckdb": "warehouse.duckdb",
    "postgres_env": "warehouse.postgres_env",
}


def resolve_options(store: Store, overrides: dict) -> dict:
    """flags > saved settings."""
    return {k: (overrides.get(k) if overrides.get(k) is not None else store.get_setting(sk)) for k, sk in SOURCE_KEYS.items()}


def sync_dbt_cloud(store: Store) -> str:
    """Download the configured job's artifacts into a folder next to the store and return it (used as `dbt.target`)."""
    from .orchestrators.dbtcloud import DbtCloudClient
    job = str(store.get_setting("dbt_cloud.job_id"))
    base = Path(store.get_setting("dbt_cloud.dir") or (Path(store.path).parent / ".intentflow_dbt_cloud" if store.backend == "sqlite" else Path.cwd() / ".intentflow_dbt_cloud"))
    c = DbtCloudClient(store.get_setting("dbt_cloud.host"), store.get_setting("dbt_cloud.account_id"))
    r = c.pull_artifacts(job, base / f"job-{job}", store.get_setting("dbt_cloud.run_id"))
    return r["dir"]


def run_check(store: Store, overrides: dict | None = None, only: list[str] | None = None, stages: set[str] | None = None) -> dict:
    o = resolve_options(store, overrides or {})
    if not o["target"] and store.get_setting("dbt_cloud.job_id") and store.get_setting("dbt_cloud.account_id"):
        o["target"] = sync_dbt_cloud(store)
    if not o["target"]:
        raise ValueError(tr("dbt target directory not configured (`intentflow config set dbt.target <dir>` or in the UI)"))
    project = dbt_loader.load(o["target"], o["root"] or Path(o["target"]).parent)
    lineage = lin.build(project, cache=store)
    store.flush()
    base = base_l = None
    if o["base"]:
        base = dbt_loader.load(o["base"], o["base_root"] or Path(o["base"]).parent)
        base_l = lin.build(base, cache=store)
        store.flush()
    from . import warehouses
    wh = warehouses.connect(lambda k: o["duckdb"] if k == "warehouse.duckdb" and o["duckdb"] else
                            o["postgres_env"] if k == "warehouse.postgres_env" and o["postgres_env"] else store.get_setting(k))
    dres = dbt_results.load(o["target"])
    if dres:
        fresh = dbt_results.record(store, dres)       # timings enter the history once per invocation
        store.set_setting("state.dbt_run", dbt_results.summary(dres, project))
        _ = fresh
    from . import external
    declared = store.get_setting("lineage.declared") or str(Path(o["root"] or Path(o["target"]).parent) / external.DECLARED_FILE)
    ctx = Context(project=project, external=external.load(store, declared, project), lineage=lineage, base_project=base, base_lineage=base_l, policies=store.policies(), dbt_results=dres,
                  airflow_dags_dir=o["airflow_dags"], kestra_flows_dir=o["kestra_flows"], store=store, warehouse=wh,
                  dq_results=__import__("intentflow.dq_results", fromlist=["x"]).load_all(store.get_setting("dq.results")),
                  contracts=__import__("intentflow.contracts", fromlist=["x"]).load_all(store.get_setting("contracts.dir")),
                  samples=store.recent_samples(time.time() - 24 * 3600))
    ctx.runs = [Run(r["system"], r["pipeline"], r["run_id"], r["state"], r["start"], r["end"], r["duration"], r["url"]) for r in store.runs(1000)]
    try:
        findings = run_checks(ctx, only, stages=stages)
    finally:
        if wh is not None:
            wh.close()
    for f in findings:   # owner and file help routing and CI annotations
        for n in f.nodes:
            node = project.nodes.get(n)
            if node:
                f.evidence.setdefault("owner", node.meta.get("owner") or node.meta.get("team"))
                f.evidence.setdefault("file", node.path)
                break
        if f.evidence.get("owner") is None:
            f.evidence.pop("owner", None)
    if not store.get_setting("scan.include_packages"):
        # findings whose nodes all belong to installed packages (dbt_utils, project-evaluator...) are not actionable by the team
        findings = [f for f in findings if not f.nodes or any(n not in project.nodes or project.in_root_package(n) for n in f.nodes)]
    res = store.sync_findings(findings, ctx.completed_checks)
    store.set_setting("state.last_check", {"ts": time.time(), "findings": len(findings), **res, "lineage": lineage.precision_report})
    store.audit("system", "check", {"findings": len(findings), **res})
    return {"findings": findings, "lineage": lineage.precision_report, "store": res, "project": project}


def collect_runs(store: Store, airflow: str | None = None, airflow_api: str | None = None, kestra: str | None = None,
                 prefect: str | None = None, dagster: str | None = None,
                 airbyte: str | None = None, fivetran: str | None = None,
                 spark: str | None = None, databricks: str | None = None,
                 argo: str | None = None, mage: str | None = None, dbt_cloud: bool | None = None) -> dict:
    """Collect runs from the configured orchestrators. URLs come from the argument or from the saved settings."""
    from .orchestrators.airflow import AirflowClient
    from .orchestrators.kestra import KestraClient
    airflow = airflow or store.get_setting("airflow.url")
    kestra = kestra or store.get_setting("kestra.url")
    api = airflow_api or store.get_setting("airflow.api", "v1")
    n, errors = 0, []
    from .orchestrators.dagster import DagsterClient
    from .orchestrators.prefect import PrefectClient
    prefect = prefect or store.get_setting("prefect.url")
    dagster = dagster or store.get_setting("dagster.url")
    from .orchestrators.ingestion import AirbyteClient, FivetranClient
    airbyte = airbyte or store.get_setting("airbyte.url")
    fivetran = fivetran or store.get_setting("fivetran.url")          # "https://api.fivetran.com" (or a proxy)
    from .orchestrators.spark import DatabricksJobsClient, SparkClient
    spark = spark or store.get_setting("spark.url")
    databricks = databricks or store.get_setting("databricks.jobs_host")      # ex.: adb-123.azuredatabricks.net (token: DATABRICKS_TOKEN)
    from .orchestrators.more import ArgoClient, MageClient
    argo = argo or store.get_setting("argo.url")
    mage = mage or store.get_setting("mage.url")
    from .orchestrators import extra
    extras = []
    for key, build in (("kafka_connect.url", lambda u: extra.KafkaConnectClient(u)), ("flink.url", lambda u: extra.FlinkClient(u)),
                       ("luigi.url", lambda u: extra.LuigiClient(u)), ("mlflow.url", lambda u: extra.MLflowClient(u)),
                       ("rundeck.url", lambda u: extra.RundeckClient(u, store.get_setting("rundeck.project") or "default"))):
        u = store.get_setting(key)
        if u:
            extras.append((key.split(".")[0], build(u)))
    if store.get_setting("aws.region") or store.get_setting("aws.endpoint_url"):
        from .orchestrators import aws
        rg, ep = store.get_setting("aws.region"), store.get_setting("aws.endpoint_url")
        for flag, cls in (("aws.glue", aws.GlueClient), ("aws.stepfunctions", aws.StepFunctionsClient), ("aws.athena", aws.AthenaClient)):
            if store.get_setting(flag):
                try:
                    extras.append((flag.split(".")[1], cls(rg, ep)))
                except RuntimeError as e:          # boto3 ausente
                    extras.append((flag.split(".")[1], type("_Err", (), {"runs": lambda self, e=e: (_ for _ in ()).throw(e)})()))
    if store.get_setting("azure.data_factory"):
        from .orchestrators.more import AzureDataFactoryClient
        extras.append(("adf", AzureDataFactoryClient(store.get_setting("azure.data_factory"))))
    dbtc = None
    if dbt_cloud is not False and store.get_setting("dbt_cloud.account_id"):
        from .orchestrators.dbtcloud import DbtCloudClient
        dbtc = DbtCloudClient(store.get_setting("dbt_cloud.host"), store.get_setting("dbt_cloud.account_id"))
    clients = ([("airflow", AirflowClient(airflow, api))] if airflow else []) + ([("kestra", KestraClient(kestra))] if kestra else []) \
        + ([("prefect", PrefectClient(prefect))] if prefect else []) + ([("dagster", DagsterClient(dagster))] if dagster else []) \
        + ([("airbyte", AirbyteClient(airbyte))] if airbyte else []) + ([("fivetran", FivetranClient(fivetran))] if fivetran else []) \
        + ([("spark", SparkClient(spark))] if spark else []) + ([("databricks", DatabricksJobsClient(databricks))] if databricks else []) \
        + ([("dbt_cloud", dbtc)] if dbtc else []) + extras + ([("argo", ArgoClient(argo, store.get_setting("argo.namespace")))] if argo else []) + ([("mage", MageClient(mage))] if mage else [])
    for name, c in clients:
        try:
            rs = c.runs()
            store.upsert_runs(rs)
            n += len(rs)
        except RuntimeError as e:
            errors.append(f"{name}: {e}")
    if not clients:
        errors.append(tr("no orchestrator configured") + " (airflow.url / kestra.url / prefect.url / dagster.url / airbyte.url / fivetran.url / spark.url / databricks.jobs_host / argo.url / mage.url / dbt_cloud.account_id / kafka_connect.url / flink.url / luigi.url / mlflow.url / rundeck.url / aws.*)")
    return {"runs": n, "errors": errors}


def collect_samples(store: Store, host: bool = True, docker: bool = True) -> int:
    from . import monitoring
    from .orchestrators.airflow import AirflowClient
    s = []
    if host:
        s += monitoring.host_memory()
    if docker:
        s += monitoring.docker_containers()
    spark_url = store.get_setting("spark.url")
    if spark_url:
        from .orchestrators.spark import SparkClient
        try:
            s += SparkClient(spark_url).samples()
        except RuntimeError:
            pass
    try:
        from . import warehouses
        wh = warehouses.connect(store.get_setting)
        if wh is not None:
            try:
                s += monitoring.warehouse_spill(wh)
                s += monitoring.warehouse_memory_failures(wh)
            finally:
                wh.close()
    except Exception:                      # missing driver / no credential: does not take the collection down
        pass
    if store.get_setting("kubernetes.enabled"):
        s += monitoring.kubernetes_pods(context=store.get_setting("kubernetes.context"), namespace=store.get_setting("kubernetes.namespace"))
    if store.get_setting("prometheus.url"):
        try:
            qs = store.get_setting("prometheus.queries")
            if isinstance(qs, str):
                import json as _json
                qs = _json.loads(qs)
            s += monitoring.prometheus_samples(store.get_setting("prometheus.url"), qs or None)
        except (RuntimeError, ValueError):
            pass
    if store.get_setting("catalog.glue") or store.get_setting("catalog.unity"):
        from . import catalogs
        catalogs.collect(store)
    if store.get_setting("kafka.bootstrap") or store.get_setting("kafka.schema_registry"):
        from . import streaming
        streaming.collect(store)
    url = store.get_setting("airflow.url")
    if url:
        try:
            s += monitoring.airflow_pools(AirflowClient(url, store.get_setting("airflow.api", "v1")))
        except RuntimeError:
            pass
    store.add_samples(s)
    return len(s)


def watch(store: Store, interval_s: float = 300, iterations: int | None = None, sleep=time.sleep) -> int:
    """Simple scheduler: collect runs/memory, run the checks and notify; repeat every `interval_s`."""
    n = 0
    while iterations is None or n < iterations:
        t0 = time.time()
        try:
            collect_runs(store)
            collect_samples(store)
            if store.get_setting("dbt.target"):
                run_check(store)
                from . import dbt_loader, incidents, notify
                target = store.get_setting("dbt.target")
                proj = dbt_loader.load(target)
                notify.send(store, incidents.group(store.findings("open"), proj))
        except Exception as e:     # a bad cycle does not take the scheduler down
            store.audit("system", "watch_error", {"error": str(e)[:300]})
        n += 1
        if iterations is None or n < iterations:
            sleep(max(0.0, interval_s - (time.time() - t0)))
    return n
