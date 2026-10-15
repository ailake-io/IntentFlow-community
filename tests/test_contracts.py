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
    assert ("fct_orders.order_id", "unenforced_unique") not in kinds                  # a declared grain counts as a key
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
