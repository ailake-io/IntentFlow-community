import json
from pathlib import Path

from intentflow import contracts, sqlmesh_loader
from intentflow.checks import Context, run_checks

ROOT = Path(__file__).resolve().parent.parent


def _ctx(extra=None):
    p = sqlmesh_loader.from_export(json.loads((ROOT / "tests/fixtures/sqlmesh_shop_export.json").read_text()))
    return Context(project=p, contracts=contracts.load_all(str(ROOT / "examples/contracts")))


def test_both_formats_are_read():
    cs = {c.name: c for c in contracts.load_all(str(ROOT / "examples/contracts"))}
    assert set(cs) == {"fct_orders", "revenue_by_country"}
    assert cs["fct_orders"].columns["order_id"].required and cs["fct_orders"].columns["order_id"].unique
    assert cs["revenue_by_country"].columns["revenue"].type == "DECIMAL(12,2)" and cs["fct_orders"].owner == "data-platform"


def test_contract_violations_are_found_and_ranked():
    fs = run_checks(_ctx(), only=["CTR001"])
    kinds = {(f.title.split(":")[0], f.evidence["kind"]): f for f in fs}
    miss = kinds[("fct_orders.discount", "missing_column")]
    assert miss.severity == "critical" and miss.evidence["downstream"] == 1        # revenue_by_country consome fct_orders
    assert ("fct_orders.order_id", "unenforced_required") in kinds
    assert ("fct_orders.order_id", "unenforced_unique") in kinds                      # a declared grain is not an executed test
    assert ("revenue_by_country.country", "unenforced_required") in kinds
    assert not any(k[0] == "fct_orders.amount" and k[1] == "type_mismatch" for k in kinds)   # DECIMAL ~ decimal
    assert not any(k[0] == "revenue_by_country.revenue" for k in kinds)


def test_type_mismatch_and_missing_model(tmp_path):
    (tmp_path / "c.yaml").write_text("models:\n  fct_orders:\n    fields:\n      status: {type: integer}\n  nao_existe:\n    fields:\n      a: {type: string}\n")
    p = _ctx().project
    fs = run_checks(Context(project=p, contracts=contracts.load_all(str(tmp_path))), only=["CTR001"])
    assert {f.evidence["kind"] for f in fs} == {"type_mismatch", "missing_model"}


def test_family_normalisation():
    f = contracts.family
    assert f("VARCHAR(20)") == f("text") == "string" and f("BIGINT") == "integer" and f("DECIMAL(10,2)") == "number"
    assert f("timestamp with time zone") == "timestamp" and f("geometry") is None and f(None) is None


def test_incremental_key_and_composite_test_do_not_prove_column_uniqueness():
    from intentflow.dbt_loader import Col, DbtNode, Project
    from intentflow.contracts import ModelContract, ColumnRule
    node = DbtNode("model.shop.orders", "orders", "model", unique_key=["order_id", "store_id"], columns={"order_id": Col("order_id", "INT")},
                   tests=[{"name": "unique_combination_of_columns", "column": None, "kwargs": {"combination_of_columns": ["order_id", "store_id"]}}])
    ctx = Context(project=Project({node.unique_id: node}), contracts=[ModelContract("orders", {"order_id": ColumnRule("order_id", unique=True)})])
    assert any(f.evidence["kind"] == "unenforced_unique" for f in run_checks(ctx, only=["CTR001"]))
    node.tests = [{"name": "unique", "column": "order_id", "kwargs": {}}]
    assert not run_checks(ctx, only=["CTR001"])


def test_contract_model_name_must_be_unambiguous_and_can_be_qualified():
    from intentflow.dbt_loader import Col, DbtNode, Project
    from intentflow.contracts import ModelContract, ColumnRule
    nodes = {"model.sales.orders": DbtNode("model.sales.orders", "orders", "model", relation='"db"."sales"."orders"', columns={"id": Col("id", "INT")}),
             "model.finance.orders": DbtNode("model.finance.orders", "orders", "model", relation='"db"."finance"."orders"', columns={"id": Col("id", "VARCHAR")})}
    contract = ModelContract("orders", {"id": ColumnRule("id", type="integer")})
    ctx = Context(project=Project(nodes), contracts=[contract])
    findings = run_checks(ctx, only=["CTR001"])
    assert len(findings) == 1 and findings[0].evidence["kind"] == "ambiguous_model"
    assert set(findings[0].nodes) == set(nodes)
    for name in ("sales.orders", "model.sales.orders", "db.sales.orders"):
        contract.name = name
        assert not run_checks(ctx, only=["CTR001"])
    contract.name = "finance.orders"
    assert run_checks(ctx, only=["CTR001"])[0].evidence["kind"] == "type_mismatch"
    contract.name = "orders"
    ctx.required_checks = {"CTR001"}
    run_checks(ctx, only=["CTR001"])
    assert "CTR001" in ctx.failed_checks and "CTR001" not in ctx.completed_checks
