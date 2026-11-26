import os, shutil
from pathlib import Path

import pytest

from intentflow import dbt_loader, lineage
from intentflow.enterprise import proposals
from intentflow.checks import Context, run_checks
from intentflow.store import Store

ROOT = Path(__file__).resolve().parent.parent

OL = [
    {"eventType": "START", "eventTime": "2026-10-11T10:00:00.000Z", "run": {"runId": "r1"}, "job": {"namespace": "airflow", "name": "etl.load"},
     "inputs": [{"namespace": "pg", "name": "public.orders"}], "outputs": [], "producer": "p", "schemaURL": "u"},
    {"eventType": "COMPLETE", "eventTime": "2026-10-11T10:02:30.000Z", "run": {"runId": "r1"}, "job": {"namespace": "airflow", "name": "etl.load"},
     "inputs": [{"namespace": "pg", "name": "public.orders"}],
     "outputs": [{"namespace": "wh", "name": "mart.sales", "facets": {"columnLineage": {"fields": {
         "total": {"inputFields": [{"namespace": "pg", "name": "public.orders", "field": "amount"}]}}}}}],
     "producer": "p", "schemaURL": "u"},
    {"eventType": "START", "eventTime": "2026-10-11T11:00:00.000Z", "run": {"runId": "r2"}, "job": {"namespace": "spark", "name": "agg"}, "inputs": [], "outputs": []},
    {"eventType": "FAIL", "eventTime": "2026-10-11T11:00:10.000Z", "run": {"runId": "r2"}, "job": {"namespace": "spark", "name": "agg"}, "inputs": [], "outputs": []},
]


@pytest.mark.usefixtures("sandbox_image")
def test_expand_star_fix_validated_by_build(shop_target, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(shop_target, work, ignore=shutil.ignore_patterns("logs"))
    p = dbt_loader.load(work / "target", work)
    f = [x for x in run_checks(Context(project=p, lineage=lineage.build(p)), only=["SQL001"]) if p.nodes[x.nodes[0]].name == "customers"][0]
    assert f.fix and f.fix["kind"] == "expand_star" and "stg_orders" in f.fix["refs"]
    st = Store()
    st.sync_findings([f], {"SQL001"})
    prop = proposals.create(st, p, st.findings(None)[0])
    assert "select order_id, customer_id, order_date, status from" in prop["changes"][0]["after"]
    proposals.approve(st, prop["id"], "t")
    assert proposals.validate_and_store(st, prop["id"], "build")["validation"]["ok"]
    proposals.apply(st, prop["id"])
    assert "select *" not in (work / "models/marts/customers.sql").read_text().lower().replace("select * from {{ ref('stg_customers') }}", "")


PG = os.environ.get("INTENTFLOW_PG_URL")
