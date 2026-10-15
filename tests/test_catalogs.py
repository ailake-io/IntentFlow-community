import json

import pytest

from intentflow import catalogs, sqlmesh_loader
from intentflow.checks import Context, run_checks
from intentflow.store import Store
from pathlib import Path

FX = Path(__file__).parent / "fixtures/sqlmesh_shop_export.json"
boto3 = pytest.importorskip("boto3")


def _project():
    return sqlmesh_loader.from_export(json.loads(FX.read_text()))


def test_glue_snapshot_then_drift_with_moto(monkeypatch, tmp_path):
    from moto import mock_aws
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        g = boto3.client("glue")
        g.create_database(DatabaseInput={"Name": "shop"})
        cols = [{"Name": "order_id", "Type": "bigint"}, {"Name": "amount", "Type": "double"}]
        g.create_table(DatabaseName="shop", TableInput={"Name": "fct_orders", "StorageDescriptor": {"Columns": cols},
                                                        "PartitionKeys": [{"Name": "dt", "Type": "string"}]})
        g.create_table(DatabaseName="shop", TableInput={"Name": "tmp_scratch", "StorageDescriptor": {"Columns": [{"Name": "x", "Type": "int"}]}})
        st = Store(str(tmp_path / "s.db"))
        st.set_setting("catalog.glue", True)
        st.set_setting("aws.region", "us-east-1")
        r1 = catalogs.collect(st)
        assert r1 == {"tables": 2, "changed": False, "errors": []}
        assert st.get_setting("state.catalog.cur")["glue:shop.fct_orders"] == {"order_id": "bigint", "amount": "double", "dt": "string"}
        assert run_checks(Context(project=_project(), store=st), only=["CAT001"]) == []          # a single snapshot: nothing to compare
        # change: amount disappears, order_id becomes a string, scratch is removed
        g.update_table(DatabaseName="shop", TableInput={"Name": "fct_orders", "StorageDescriptor": {"Columns": [{"Name": "order_id", "Type": "string"}]}})
        g.delete_table(DatabaseName="shop", Name="tmp_scratch")
        r2 = catalogs.collect(st)
        assert r2["changed"] and r2["tables"] == 1
        f = run_checks(Context(project=_project(), store=st), only=["CAT001"])
        by = {x.evidence["kind"]: x for x in f}
        assert by["schema_break"].severity == "critical" and by["schema_break"].nodes == ["model.sqlmesh.fct_orders"]
        assert by["schema_break"].evidence["removed"] == ["amount", "dt"] and by["schema_break"].evidence["retyped"] == ["order_id"]
        assert by["table_dropped"].severity == "high" and by["table_dropped"].nodes == ["catalog:glue:shop.tmp_scratch"]
        assert catalogs.collect(st)["changed"] is False                                          # no change: previous snapshot kept
        assert len(run_checks(Context(project=_project(), store=st), only=["CAT001"])) == 2


def test_unity_catalog_pagination_and_include_filter(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        if url.endswith("/catalogs"):
            return {"catalogs": [{"name": "main"}]}
        if "/schemas?catalog_name=main" in url:
            return {"schemas": [{"name": "raw"}, {"name": "scratch"}]}
        if "schema_name=raw" in url and "page_token" not in url:
            return {"tables": [{"name": "orders", "columns": [{"name": "ID", "type_text": "bigint"}]}], "next_page_token": "p2"}
        if "schema_name=raw" in url:
            return {"tables": [{"name": "customers", "columns": [{"name": "id", "type_name": "INT"}]}]}
        raise AssertionError(url)
    snap = catalogs.UnityCatalog("adb-1.azuredatabricks.net", fetch=fetch).snapshot(include=["main.raw."])
    assert snap == {"main.raw.orders": {"id": "bigint"}, "main.raw.customers": {"id": "INT"}}
    assert calls[0][1] == {"Authorization": "Bearer t"} and not any("scratch" in c[0] and "tables" in c[0] for c in calls)


def test_collect_survives_a_failing_catalog(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("catalog.unity", "host-inexistente.invalid")
    r = catalogs.collect(st, unity=catalogs.UnityCatalog("http://127.0.0.1:9", fetch=lambda u, h: (_ for _ in ()).throw(RuntimeError("sem conexão"))))
    assert r["tables"] == 0 and r["errors"] and "unity" in r["errors"][0]
