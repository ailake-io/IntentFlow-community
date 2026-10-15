import json, shutil
from pathlib import Path

import duckdb

from intentflow import dbt_loader, lineage
from intentflow.checks import Context, run_checks
from intentflow.store import Store
from intentflow.warehouses import DuckDBWarehouse


def _mini_project(tmp_path, a_type, b_type):
    nodes = {
        "model.p.a": {"unique_id": "model.p.a", "name": "a", "resource_type": "model", "relation_name": '"db"."main"."a"', "original_file_path": "a.sql",
                      "raw_code": "", "compiled_code": "", "config": {}, "meta": {}, "tags": [], "depends_on": {"nodes": []}, "columns": {}},
        "model.p.b": {"unique_id": "model.p.b", "name": "b", "resource_type": "model", "relation_name": '"db"."main"."b"', "original_file_path": "b.sql",
                      "raw_code": "", "compiled_code": "", "config": {}, "meta": {}, "tags": [], "depends_on": {"nodes": []}, "columns": {}},
        "model.p.c": {"unique_id": "model.p.c", "name": "c", "resource_type": "model", "relation_name": '"db"."main"."c"', "original_file_path": "c.sql",
                      "raw_code": "", "config": {}, "meta": {}, "tags": [], "depends_on": {"nodes": ["model.p.a", "model.p.b"]}, "columns": {},
                      "compiled_code": 'select a.id as aid, b.v from "db"."main"."a" a join "db"."main"."b" b on a.id = b.id'},
    }
    cat = {"nodes": {"model.p.a": {"columns": {"id": {"name": "id", "type": a_type, "index": 0}}},
                     "model.p.b": {"columns": {"id": {"name": "id", "type": b_type, "index": 0}, "v": {"name": "v", "type": "INTEGER", "index": 1}}},
                     "model.p.c": {"columns": {"aid": {"name": "aid", "type": a_type, "index": 0}, "v": {"name": "v", "type": "INTEGER", "index": 1}}}}, "sources": {}}
    (tmp_path / "target").mkdir(parents=True)
    (tmp_path / "target/manifest.json").write_text(json.dumps({"metadata": {"adapter_type": "duckdb"}, "nodes": nodes, "sources": {}}))
    (tmp_path / "target/catalog.json").write_text(json.dumps(cat))
    return dbt_loader.load(tmp_path / "target", tmp_path)


def test_join_key_type_mismatch(tmp_path):
    p = _mini_project(tmp_path, "INTEGER", "VARCHAR")
    f = run_checks(Context(project=p, lineage=lineage.build(p)), only=["SQL015"])
    assert len(f) == 1 and "INTEGER" in f[0].title and "VARCHAR" in f[0].title


def test_compatible_numeric_types_are_not_flagged(tmp_path):
    p = _mini_project(tmp_path, "INTEGER", "BIGINT")
    assert run_checks(Context(project=p, lineage=lineage.build(p)), only=["SQL015"]) == []


def test_join_fanout_probe_on_lab(lab_target):
    p = dbt_loader.load(lab_target / "target", lab_target)
    wh = DuckDBWarehouse(str(lab_target / "target" / "lab.duckdb"))
    f = run_checks(Context(project=p, lineage=lineage.build(p), warehouse=wh), only=["PRB004"])
    names = {p.nodes[x.nodes[0]].name for x in f}
    assert {"bad_distinct_join", "lin_self_join"} <= names
    hit = next(x for x in f if p.nodes[x.nodes[0]].name == "bad_distinct_join")
    assert hit.evidence["side"] == "events.user_id" and hit.evidence["duplicates"] > 0


def test_null_rate_drift(shop_target, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    db = tmp_path / "a" / "shop.duckdb"          # the file name is the catalog name in DuckDB
    shutil.copy(shop_target / "target/shop.duckdb", db)
    p = dbt_loader.load(shop_target / "target", shop_target)
    st = Store()
    wh = DuckDBWarehouse(str(db))
    for _ in range(4):                                      # history: 0% nulls
        assert run_checks(Context(project=p, lineage=lineage.build(p), warehouse=wh, store=st), only=["PRB003"]) == []
    db2 = tmp_path / "b" / "shop.duckdb"
    shutil.copy(db, db2)
    con = duckdb.connect(str(db2))
    con.execute("update main.raw_customers set email = null")
    con.close()
    wh = DuckDBWarehouse(str(db2))
    f = run_checks(Context(project=p, lineage=lineage.build(p), warehouse=wh, store=st), only=["PRB003"])
    assert any("raw_customers.email" in x.title and "100%" in x.title for x in f)


def test_plan_regression_between_base_and_head(lab_target, tmp_path):
    base = dbt_loader.load(lab_target / "target", lab_target)
    head = dbt_loader.load(lab_target / "target", lab_target)
    uid = next(u for u, n in head.nodes.items() if n.name == "lin_window")
    head.nodes[uid].compiled_code = 'select u.id as user_id from "lab"."main"."users" u cross join "lab"."main"."events" e'
    wh = DuckDBWarehouse(str(lab_target / "target" / "lab.duckdb"))
    f = run_checks(Context(project=head, lineage=lineage.build(base), base_project=base, warehouse=wh), only=["SQL014"])
    assert f and "lin_window" in f[0].title and "CROSS_PRODUCT" in f[0].evidence["new_heavy_operators"]


def test_missing_contract(shop_target):
    p = dbt_loader.load(shop_target / "target", shop_target)
    f = run_checks(Context(project=p, lineage=lineage.build(p)), only=["DBT005"])
    assert {p.nodes[x.nodes[0]].name for x in f} <= {"stg_customers", "stg_orders", "stg_payments", "customers", "orders"}
