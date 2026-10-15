"""Lineage for cloud dialects with compiled fixture SQL (synthetic manifest; no account connection).
Checks SQLGlot parsing/qualification in the dialects, not execution in the warehouses."""
import json

import pytest

from intentflow import dbt_loader, lineage

CASES = {
    "snowflake": {
        "rel": lambda n: f'"ANALYTICS"."PUBLIC"."{n.upper()}"',
        "src_cols": {"orders": {"ID": "NUMBER", "AMOUNT": "NUMBER", "STATUS": "VARCHAR", "PAYLOAD": "VARIANT"}},
        "sql": ('select o.id as order_id, iff(o.status = \'done\', o.amount, 0) as paid, o.payload:customer::varchar as customer, '
                'to_varchar(o.amount) as amount_txt from "ANALYTICS"."PUBLIC"."ORDERS" o qualify row_number() over (partition by o.id order by o.amount) = 1'),
        "out": ["order_id", "paid", "customer", "amount_txt"],
        "truth": {("orders", "id", "order_id"), ("orders", "amount", "paid"), ("orders", "status", "paid"), ("orders", "payload", "customer"),
                  ("orders", "amount", "amount_txt")},
    },
    "bigquery": {
        "rel": lambda n: f"`proj`.`ds`.`{n}`",
        "src_cols": {"orders": {"id": "INT64", "amount": "NUMERIC", "tags": "ARRAY<STRING>"}},
        "sql": ("select o.id as order_id, safe_cast(o.amount as string) as amount_txt, tag as tag "
                "from `proj`.`ds`.`orders` o cross join unnest(o.tags) as tag"),
        "out": ["order_id", "amount_txt", "tag"],
        "truth": {("orders", "id", "order_id"), ("orders", "amount", "amount_txt"), ("orders", "tags", "tag")},
    },
    "databricks": {
        "rel": lambda n: f"`main`.`sales`.`{n}`",
        "src_cols": {"orders": {"id": "BIGINT", "amount": "DECIMAL(10,2)", "items": "ARRAY<STRING>"}},
        "sql": ("select o.id as order_id, o.amount * 2 as double_amount, explode(o.items) as item "
                "from `main`.`sales`.`orders` o where o.amount > 0"),
        "out": ["order_id", "double_amount", "item"],
        "truth": {("orders", "id", "order_id"), ("orders", "amount", "double_amount"), ("orders", "items", "item")},
    },
}


@pytest.mark.parametrize("dialect", list(CASES))
def test_lineage_in_cloud_dialects(dialect, tmp_path):
    c = CASES[dialect]
    nodes, cat = {}, {"nodes": {}, "sources": {}}
    for name, cols in c["src_cols"].items():
        uid = f"seed.p.{name}"
        nodes[uid] = {"unique_id": uid, "name": name, "resource_type": "seed", "relation_name": c["rel"](name), "original_file_path": f"{name}.csv",
                      "config": {}, "meta": {}, "tags": [], "depends_on": {"nodes": []}, "columns": {}}
        cat["nodes"][uid] = {"columns": {k: {"name": k, "type": t, "index": i} for i, (k, t) in enumerate(cols.items())}}
    mid = "model.p.out"
    nodes[mid] = {"unique_id": mid, "name": "out", "resource_type": "model", "relation_name": c["rel"]("out"), "original_file_path": "out.sql",
                  "raw_code": "", "compiled_code": c["sql"], "config": {}, "meta": {}, "tags": [], "columns": {},
                  "depends_on": {"nodes": ["seed.p.orders"]}}
    cat["nodes"][mid] = {"columns": {k: {"name": k, "type": "VARCHAR", "index": i} for i, k in enumerate(c["out"])}}
    (tmp_path / "target").mkdir()
    (tmp_path / "target/manifest.json").write_text(json.dumps({"metadata": {"adapter_type": dialect}, "nodes": nodes, "sources": {}}))
    (tmp_path / "target/catalog.json").write_text(json.dumps(cat))
    p = dbt_loader.load(tmp_path / "target", tmp_path)
    r = lineage.build(p)
    got = {(p.nodes[e.src].name, e.src_col, e.dst_col) for e in r.edges}
    assert r.unresolved == [], r.unresolved
    assert got == c["truth"], (dialect, "extra:", got - c["truth"], "faltando:", c["truth"] - got)
