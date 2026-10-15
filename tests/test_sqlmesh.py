import json
import copy, json, os
from pathlib import Path

import pytest

from intentflow import dbt_loader, lineage, lineage_api, sqlmesh_loader
from intentflow.checks import Context, run_checks

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "tests/fixtures/sqlmesh_shop_export.json"
PY = os.environ.get("INTENTFLOW_SQLMESH_PYTHON")


@pytest.fixture
def data():
    return json.loads(SNAP.read_text())


def test_snapshot_loads_through_the_normal_loader(tmp_path, data):
    f = tmp_path / "sqlmesh_export.json"
    f.write_text(json.dumps(data))
    p = dbt_loader.load(f)                       # o mesmo ponto de entrada de todo o produto
    assert p.adapter == "duckdb" and len(p.nodes) == 5
    fct = p.nodes["model.sqlmesh.fct_orders"]
    assert fct.relation == '"shop"."shop"."fct_orders"' and fct.meta["owner"] == "data-platform"
    assert set(fct.depends_on) == {"model.sqlmesh.raw_orders", "model.sqlmesh.stg_customers"}
    assert p.nodes["model.sqlmesh.raw_orders"].resource_type == "seed"
    stg = p.nodes["model.sqlmesh.stg_customers"]
    assert stg.columns["email"].description.startswith("E-mail") and [t["name"] for t in stg.tests] == ["not_null", "unique_values"]
    assert dbt_loader.load(tmp_path).nodes.keys() == p.nodes.keys()      # a folder with the snapshot works too


def test_column_lineage_and_multi_hop_impact(data):
    p = sqlmesh_loader.from_export(data)
    res = lineage.build(p)
    assert res.unresolved == [] if hasattr(res, "unresolved") else True
    # quoted identifiers must not remain in the edges
    assert all('"' not in e.src_col for e in res.edges)
    imp = lineage_api.impact(p, res, "raw_orders", "amount")
    names = json.dumps(imp)
    assert "fct_orders" in names and "revenue_by_country" in names


def test_schema_drift_is_critical_with_rename_suggestion(data):
    base = sqlmesh_loader.from_export(data)
    new = copy.deepcopy(data)
    for m in new["models"]:
        if m["fqn"].endswith('"raw_orders"'):
            m["columns"].pop("amount")
            m["columns"]["amount_cents"] = "BIGINT"
    np_ = sqlmesh_loader.from_export(new)
    ctx = Context(project=np_, lineage=lineage.build(np_), base_project=base, base_lineage=lineage.build(base))
    f = [x for x in run_checks(ctx, only=["SCH001"])]
    assert f and f[0].severity == "critical" and "amount_cents" in f[0].title


def test_same_name_in_two_schemas_gets_distinct_ids(data):
    d = copy.deepcopy(data)
    clone = copy.deepcopy(next(m for m in d["models"] if m["fqn"].endswith('"stg_customers"')))
    clone["fqn"] = '"shop"."other"."stg_customers"'
    clone["depends_on"] = []
    d["models"].append(clone)
    ids = set(sqlmesh_loader.from_export(d).nodes)
    assert "model.sqlmesh.shop.stg_customers" in ids and "model.sqlmesh.other.stg_customers" in ids


def test_missing_sqlmesh_gives_actionable_error(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "config.yaml").write_text("gateways: {}")
    import subprocess, sys
    with pytest.raises(RuntimeError, match="INTENTFLOW_SQLMESH_PYTHON"):
        sqlmesh_loader.export(tmp_path, python=sys.executable)    # the project's Python has no sqlmesh


@pytest.mark.skipif(not PY, reason="real SQLMesh not configured (INTENTFLOW_SQLMESH_PYTHON)")
def test_real_sqlmesh_project_matches_the_snapshot_fixture(data, monkeypatch):
    monkeypatch.setenv("INTENTFLOW_SQLMESH_ALLOW_EXEC", "1")
    p = dbt_loader.load(ROOT / "examples/sqlmesh_shop")          # exporta de verdade pelo interpretador do SQLMesh
    ref = sqlmesh_loader.from_export(data)
    assert {u: (n.relation, n.materialized, sorted(n.columns), n.depends_on) for u, n in p.nodes.items()} == \
           {u: (n.relation, n.materialized, sorted(n.columns), n.depends_on) for u, n in ref.nodes.items()}


def test_loading_a_sqlmesh_project_requires_explicit_opt_in_but_snapshots_do_not(tmp_path, monkeypatch, data):
    """Reading the project runs its config.py/Python models: it must not happen by default (e.g. CI for a fork PR)."""
    (tmp_path / "models").mkdir()
    (tmp_path / "config.yaml").write_text("gateways: {}")
    monkeypatch.delenv("INTENTFLOW_SQLMESH_ALLOW_EXEC", raising=False)
    with pytest.raises(ValueError, match="sqlmesh-export"):
        dbt_loader.load(tmp_path)
    snap = tmp_path / "sqlmesh_export.json"
    snap.write_text(json.dumps(data))
    assert dbt_loader.load(snap).nodes               # the exported JSON is data only: it loads without opt-in
