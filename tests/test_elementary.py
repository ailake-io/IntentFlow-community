"""`elementary_test_results` table in the schema documented by Elementary, created in real DuckDB (the dbt-elementary package is not run)."""
import json
from pathlib import Path

import duckdb

from intentflow import sqlmesh_loader
from intentflow.checks import Context, run_checks
from intentflow.store import Store
from intentflow.warehouses import DuckDBWarehouse

FX = Path(__file__).parent / "fixtures/sqlmesh_shop_export.json"


def _wh(tmp_path):
    db = str(tmp_path / "w.duckdb")
    c = duckdb.connect(db)
    c.execute("create schema analytics_elementary")
    c.execute("""create table analytics_elementary.elementary_test_results (
        test_unique_id varchar, model_unique_id varchar, table_name varchar, column_name varchar, test_name varchar, test_type varchar,
        test_sub_type varchar, status varchar, test_results_description varchar, severity varchar, detected_at timestamp)""")
    rows = [
        ("t.volume", "model.sqlmesh.fct_orders", "fct_orders", None, "volume_anomalies", "anomaly_detection", "volume", "fail",
         "row_count 120 vs expected 1000±150", "error", "2026-10-12 10:00:00"),
        ("t.volume", "model.sqlmesh.fct_orders", "fct_orders", None, "volume_anomalies", "anomaly_detection", "volume", "pass", "ok", "error", "2026-10-11 10:00:00"),
        ("t.nulls", "model.sqlmesh.stg_customers", "stg_customers", "email", "not_null", "generic", None, "warn", "3 nulls", "warn", "2026-10-12 09:00:00"),
        ("t.fresh", "model.sqlmesh.revenue_by_country", "revenue_by_country", None, "freshness_anomalies", "anomaly_detection", "freshness", "pass", "ok", "error", "2026-10-12 08:00:00"),
        ("t.ghost", "model.x.desconhecido", "tabela_fora_do_projeto", "c", "unique", "generic", None, "fail", "dups", "error", "2026-10-12 07:00:00"),
    ]
    c.executemany("insert into analytics_elementary.elementary_test_results values (?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.close()
    return db


def test_latest_failures_become_findings_with_downstream(tmp_path):
    p = sqlmesh_loader.from_export(json.loads(FX.read_text()))
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("elementary.schema", "analytics_elementary")
    f = run_checks(Context(project=p, store=st, warehouse=DuckDBWarehouse(_wh(tmp_path), read_only=False)), only=["ELM001"])
    by = {x.evidence["test"] + ":" + str(x.nodes[:1]): x for x in f}
    vol = next(x for x in f if x.evidence["test"] == "volume_anomalies")
    assert vol.severity == "high" and "anomaly" in vol.title and vol.evidence["downstream"] == 1 and "Affects 1" in vol.detail
    warn = next(x for x in f if x.evidence["test"] == "not_null")
    assert warn.severity == "medium" and warn.title.startswith("stg_customers.email")
    ghost = next(x for x in f if x.evidence["test"] == "unique")
    assert ghost.nodes == [] and ghost.evidence["unmapped_table"] == "tabela_fora_do_projeto"
    assert len(f) == 3 and not any(x.evidence["test"] == "freshness_anomalies" for x in f)       # passou → sem achado
    assert by


def test_disabled_without_setting_and_resilient_to_missing_schema(tmp_path):
    p = sqlmesh_loader.from_export(json.loads(FX.read_text()))
    wh = DuckDBWarehouse(_wh(tmp_path), read_only=False)
    st = Store(str(tmp_path / "s.db"))
    assert run_checks(Context(project=p, store=st, warehouse=wh), only=["ELM001"]) == []
    st.set_setting("elementary.schema", "nao_existe")
    assert run_checks(Context(project=p, store=st, warehouse=wh), only=["ELM001"]) == []
