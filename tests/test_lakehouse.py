"""Tabelas Delta (deltalake) e Iceberg (pyiceberg) reais gravadas em disco."""
import json
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pytest.importorskip("deltalake")

from intentflow import lakehouse, sqlmesh_loader  # noqa: E402
from intentflow.checks import Context, run_checks  # noqa: E402
from intentflow.store import Store  # noqa: E402

FX = Path(__file__).parent / "fixtures/sqlmesh_shop_export.json"


def _delta(tmp_path, name="fct_orders", appends=3):
    from deltalake import write_deltalake
    d = str(tmp_path / name)
    t = pa.table({"order_id": pa.array([1, 2, 3], pa.int64()), "amount": pa.array([1.0, 2.0, 3.0])})
    for _ in range(appends):
        write_deltalake(d, t, mode="append")
    return d, t


def test_delta_inspection_counts_files_and_schema(tmp_path):
    d, _ = _delta(tmp_path)
    t = lakehouse.inspect(d)
    assert t["format"] == "delta" and t["files"] == 3 and t["version"] == 2 and t["avg_file_bytes"] > 0
    assert set(t["schema"]) == {"order_id", "amount"} and t["name"] == "fct_orders"


def test_small_files_and_policy_threshold(tmp_path):
    d, _ = _delta(tmp_path, appends=5)
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("lakehouse.tables", [d])
    p = sqlmesh_loader.from_export(json.loads(FX.read_text()))
    assert run_checks(Context(project=p, store=st), only=["LAKE001"]) == []                    # 5 files < default limit (100)
    f = run_checks(Context(project=p, store=st, policies={"small_file_count": 3}), only=["LAKE001"])
    assert len(f) == 1 and f[0].evidence["kind"] == "small_files" and f[0].nodes == ["model.sqlmesh.fct_orders"]


def test_schema_break_between_versions_is_critical_with_downstream(tmp_path):
    from deltalake import write_deltalake
    d, t = _delta(tmp_path, appends=1)
    new = pa.table({"order_id": pa.array(["a", "b"]), "discount": pa.array([0.1, 0.2])})        # amount some, order_id muda de tipo
    write_deltalake(d, new, mode="overwrite", schema_mode="overwrite")
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("lakehouse.tables", d)                                                          # plain text works too
    p = sqlmesh_loader.from_export(json.loads(FX.read_text()))
    f = run_checks(Context(project=p, store=st), only=["LAKE001"])
    sb = next(x for x in f if x.evidence["kind"] == "schema_break")
    assert sb.severity == "critical" and sb.evidence["removed"] == ["amount"] and sb.evidence["retyped"] == ["order_id"] and sb.evidence["downstream"] == 1


def test_iceberg_table_is_read_and_unknown_path_is_reported(tmp_path):
    ice = pytest.importorskip("pyiceberg")
    from pyiceberg.catalog.memory import InMemoryCatalog
    cat = InMemoryCatalog("t", warehouse=f"file://{tmp_path}/wh")
    cat.create_namespace("shop")
    tbl = cat.create_table("shop.stg_customers", schema=pa.schema([("customer_id", pa.int64()), ("email", pa.string())]))
    for _ in range(3):
        tbl.append(pa.table({"customer_id": pa.array([1, 2], pa.int64()), "email": pa.array(["a", "b"])}))
    t = lakehouse.inspect(str(Path(tbl.metadata_location)))
    assert t["format"] == "iceberg" and t["files"] == 3 and set(t["schema"]) == {"customer_id", "email"}
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("lakehouse.tables", [tbl.metadata_location, str(tmp_path / "nada")])
    p = sqlmesh_loader.from_export(json.loads(FX.read_text()))
    f = run_checks(Context(project=p, store=st, policies={"small_file_count": 2}), only=["LAKE001"])
    assert {x.evidence["kind"] for x in f} == {"small_files", "unreadable"}
