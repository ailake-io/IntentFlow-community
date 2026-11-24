"""Promotion must fail closed on incomplete evidence, independent of finding severity."""
import copy
import json
from datetime import datetime, UTC

import pytest

from intentflow import cli, preflight
from intentflow.store import Store

MODEL = "model.shop.orders"
TEST = "test.shop.unique_orders_id.abc"


@pytest.fixture
def artifacts(tmp_path):
    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "models/orders.sql").write_text("select 1 as id\n")
    target, base = root / "target", tmp_path / "base"
    target.mkdir()
    base.mkdir()
    meta = {"generated_at": datetime.now(UTC).isoformat(), "adapter_type": "duckdb", "project_name": "shop", "invocation_id": "build-1"}
    manifest = {"metadata": meta, "nodes": {
        MODEL: {"unique_id": MODEL, "name": "orders", "resource_type": "model", "package_name": "shop", "original_file_path": "models/orders.sql",
                "relation_name": '"db"."main"."orders"', "config": {"materialized": "table", "meta": {"owner": "finance"}}, "raw_code": "select 1 as id", "compiled_code": "select 1 as id"},
        TEST: {"unique_id": TEST, "name": "unique_orders_id", "resource_type": "test", "attached_node": MODEL,
               "test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}}, "depends_on": {"nodes": [MODEL]}}
    }}
    catalog = {"metadata": meta, "nodes": {MODEL: {"columns": {"id": {"type": "INTEGER"}}}}, "errors": None}
    results = {"metadata": meta, "args": {"which": "build"}, "results": [{"unique_id": MODEL, "status": "success", "compiled_code": "select 1 as id"}, {"unique_id": TEST, "status": "pass"}]}
    for folder in (target, base):
        (folder / "manifest.json").write_text(json.dumps(manifest))
        (folder / "catalog.json").write_text(json.dumps(catalog))
    (target / "run_results.json").write_text(json.dumps(results))
    return root, target, base


def edit(target, name, change):
    path = target / name
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))


def inspect(artifacts, **kwargs):
    root, target, base = artifacts
    return preflight.inspect_artifacts(target, base, root, **kwargs)


def test_complete_build_passes_with_different_docs_invocation(artifacts):
    root, target, base = artifacts
    edit(target, "manifest.json", lambda d: d["metadata"].update(invocation_id="docs-2"))
    with Store() as store:
        result = preflight.run(store, {"target": target, "base": base, "root": root})
    assert result["ok"], result
    assert set(preflight.DEFAULT_REQUIRED) <= set(result["completed_checks"])
    assert result["expected_resources"] == 2


@pytest.mark.parametrize("file", ["manifest.json", "catalog.json", "run_results.json"])
@pytest.mark.parametrize("damage", ["missing", "json", "list", "metadata"])
def test_unavailable_artifacts_never_pass(artifacts, file, damage):
    path = artifacts[1] / file
    if damage == "missing":
        path.unlink()
    else:
        path.write_text({"json": "{", "list": "[]", "metadata": "{}"}[damage])
    assert "artifact_unavailable" in {e["code"] for e in inspect(artifacts)["errors"]}


@pytest.mark.parametrize("name", ["manifest.json", "catalog.json", "run_results.json"])
@pytest.mark.parametrize("stamp", ["2020-01-01T00:00:00Z", "2999-01-01T00:00:00Z", "2026-01-01", None, "bad"])
def test_stale_future_or_unzoned_timestamps_block(artifacts, name, stamp):
    edit(artifacts[1], name, lambda d: d["metadata"].update(generated_at=stamp))
    assert any(e["code"] in {"artifact_stale", "artifact_timestamp"} for e in inspect(artifacts)["errors"])


@pytest.mark.parametrize("age", [0, -1, float("inf"), float("nan")])
def test_invalid_max_age_rejected(artifacts, age):
    with pytest.raises(ValueError):
        inspect(artifacts, max_age_hours=age)


@pytest.mark.parametrize("command", ["docs", "parse", "run", "test", None])
def test_non_build_results_block(artifacts, command):
    edit(artifacts[1], "run_results.json", lambda d: d["args"].update(which=command))
    assert "build_required" in {e["code"] for e in inspect(artifacts)["errors"]}


@pytest.mark.parametrize("status", ["fail", "error", "skipped", "runtime error", None, [], "cancelled"])
def test_failures_skips_unknown_statuses_block_even_if_findings_would_be_low(artifacts, status):
    edit(artifacts[1], "run_results.json", lambda d: d["results"][1].update(status=status))
    assert "build_unsuccessful" in {e["code"] for e in inspect(artifacts)["errors"]}


def test_warnings_visible_and_optionally_block(artifacts):
    edit(artifacts[1], "run_results.json", lambda d: d["results"][1].update(status="warn"))
    result = inspect(artifacts)
    assert not result["errors"] and result["warnings"][0]["total"] == 1
    assert "build_warnings" in {e["code"] for e in inspect(artifacts, warnings_as_errors=True)["errors"]}


@pytest.mark.parametrize("change,code", [
    (lambda d: d["results"].pop(), "build_incomplete"),
    (lambda d: d["results"].append(copy.deepcopy(d["results"][0])), "results_duplicate"),
    (lambda d: d["results"][0].update(unique_id="model.other.orders"), "results_mismatch"),
    (lambda d: d.update(results=[]), "results_invalid"),
    (lambda d: d["results"].append(None), "results_invalid"),
    (lambda d: d["results"][0].update(compiled_code="select 2 as id"), "compiled_code_mismatch"),
])
def test_partial_or_mixed_build_cannot_pass(artifacts, change, code):
    edit(artifacts[1], "run_results.json", change)
    assert code in {e["code"] for e in inspect(artifacts)["errors"]}


@pytest.mark.parametrize("side", [1, 2])
def test_catalog_coverage_required_in_both_versions(artifacts, side):
    edit(artifacts[side], "catalog.json", lambda d: d["nodes"].clear())
    assert "catalog_incomplete" in {e["code"] for e in inspect(artifacts)["errors"]}


def test_catalog_error_and_baseline_identity_block(artifacts):
    edit(artifacts[2], "manifest.json", lambda d: d["metadata"].update(adapter_type="snowflake"))
    assert "baseline_mismatch" in {e["code"] for e in inspect(artifacts)["errors"]}
    edit(artifacts[1], "catalog.json", lambda d: d.update(errors=["permission denied"]))
    assert "catalog_errors" in {e["code"] for e in inspect(artifacts)["errors"]}


def test_current_source_must_match_manifest(artifacts):
    (artifacts[0] / "models/orders.sql").write_text("select 2 as id\n")
    assert "source_changed" in {e["code"] for e in inspect(artifacts)["errors"]}


@pytest.mark.parametrize("name", ["manifest.json", "catalog.json", "run_results.json"])
def test_revision_binding_covers_each_candidate_artifact(artifacts, name):
    for file in ("manifest.json", "catalog.json", "run_results.json"):
        edit(artifacts[1], file, lambda d: d["metadata"].update(env={"INTENTFLOW_REVISION": "candidate-abc"}))
    assert not inspect(artifacts, revision="candidate-abc")["errors"]
    edit(artifacts[1], name, lambda d: d["metadata"].update(env={"INTENTFLOW_REVISION": "old-def"}))
    assert "revision_mismatch" in {e["code"] for e in inspect(artifacts, revision="candidate-abc")["errors"]}


def test_missing_tests_cannot_be_a_clean_build(artifacts):
    edit(artifacts[1], "manifest.json", lambda d: d["nodes"].pop(TEST))
    edit(artifacts[1], "run_results.json", lambda d: d["results"].pop())
    assert "tests_missing" in {e["code"] for e in inspect(artifacts)["errors"]}


def test_internal_check_error_and_disabled_required_check_block(artifacts, monkeypatch):
    from intentflow.checks import REGISTRY, load_all
    load_all()
    def broken(ctx):
        raise RuntimeError("backend unavailable")
    monkeypatch.setattr(REGISTRY["SQL001"], "fn", broken)
    root, target, base = artifacts
    with Store() as store:
        result = preflight.run(store, {"target": target, "base": base, "root": root}, fail_on="critical")
        assert not result["ok"] and "SQL001" in result["failed_checks"]
        assert any(e["code"] == "checks_incomplete" for e in result["errors"])
        store.set_setting("policy.disabled_checks", ["DBTR002"])
        result = preflight.run(store, {"target": target, "base": base, "root": root})
        assert "DBTR003" in next(e for e in result["errors"] if e["code"] == "checks_incomplete")["nodes"]


def test_unparsed_sql_is_missing_analysis_and_python_is_not_silently_parsed_as_sql(artifacts, monkeypatch):
    from intentflow.checks import sql_quality
    root,target,base=artifacts
    monkeypatch.setattr(sql_quality,'_parse',lambda node,project:None)
    with Store() as store:
        result=preflight.run(store,{'target':target,'base':base,'root':root})
    assert not result['ok'] and 'SQL001' in result['failed_checks']
    from intentflow.dbt_loader import DbtNode,Project
    from intentflow.checks import Context,run_checks
    node=DbtNode(MODEL,'orders','model',compiled_code='def model(dbt,session): pass',language='python')
    ctx=Context(project=Project({MODEL:node}),required_checks={'SQL001'})
    run_checks(ctx,only=['SQL001'])
    assert not ctx.failed_checks  # SQL lint is not a claim of Python static coverage


def test_unknown_required_check_and_missing_warehouse_are_not_success(artifacts):
    root, target, base = artifacts
    with Store() as store:
        for required, code in [(["NO_SUCH_CHECK"], "unknown_checks"), (["PRB001"], "warehouse_required")]:
            result = preflight.run(store, {"target": target, "base": base, "root": root}, required=required)
            assert not result["ok"] and code in {e["code"] for e in result["errors"]}


def test_caught_probe_query_error_is_still_a_failed_validation(artifacts, monkeypatch):
    from intentflow import warehouses
    from intentflow.checks import REGISTRY, load_all
    load_all()
    class BrokenWarehouse:
        def scalar(self, sql):
            raise RuntimeError("permission denied")
        def close(self):
            pass
    def catches_error(ctx):
        try:
            ctx.warehouse.scalar("select 1")
        except RuntimeError:
            return []
    monkeypatch.setattr(warehouses, "connect", lambda setting: BrokenWarehouse())
    monkeypatch.setattr(REGISTRY["PRB001"], "fn", catches_error)
    root, target, base = artifacts
    with Store() as store:
        result = preflight.run(store, {"target": target, "base": base, "root": root}, required=["PRB001"])
    assert not result["ok"] and "PRB001" in result["failed_checks"]
    assert "PRB001" not in result["completed_checks"]


def test_required_volume_baseline_needs_warmup(artifacts, tmp_path):
    import duckdb
    root, target, base = artifacts
    database = tmp_path / "db.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute("create table orders as select 1 as id")
    with Store() as store:
        options = {"target": target, "base": base, "root": root, "duckdb": str(database)}
        first = preflight.run(store, options, required=["PRB002"])
        assert not first["ok"] and "PRB002" in first["failed_checks"]
        assert store.history(f"rows:{MODEL}") == []  # candidate cannot train its own baseline
        for i in range(3):
            store.add_history(f"rows:{MODEL}", i, 1)
        result = preflight.run(store, options, required=["PRB002"])
        assert result["ok"], result
        assert len(store.history(f"rows:{MODEL}")) == 3


def test_required_contracts_cannot_be_silently_missing_or_partially_invalid(artifacts, tmp_path):
    root, target, base = artifacts
    folder = tmp_path / "contracts"
    folder.mkdir()
    valid = folder / "orders.yml"
    valid.write_text("models:\n  orders:\n    fields:\n      id:\n        type: integer\n        unique: true\n")
    with Store() as store:
        options = {"target": target, "base": base, "root": root}
        result = preflight.run(store, options, required=["CTR001"])
        assert "contracts_required" in {e["code"] for e in result["errors"]}
        store.set_setting("contracts.dir", str(folder))
        assert preflight.run(store, options, required=["CTR001"])["ok"]
        (folder / "broken.yml").write_text("models: [invalid\n")
        assert not preflight.run(store, options, required=["CTR001"])["ok"]


def test_cli_machine_readable_pass_and_block(artifacts, tmp_path, capsys):
    root, target, base = artifacts
    args = ["--store", str(tmp_path / "store.db"), "preflight", "--target", str(target), "--base", str(base), "--root", str(root), "--format", "json"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    (target / "run_results.json").unlink()
    assert cli.main(args) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_schema_diff_matches_logical_nodes_across_prod_and_homologation():
    from intentflow.dbt_loader import Col, DbtNode, Project
    from intentflow.checks.schema_drift import diff_columns
    def project(schema, column):
        return Project({MODEL: DbtNode(MODEL, "orders", "model", relation=f'"db"."{schema}"."orders"', columns={column: Col(column, "INT")})})
    assert list(diff_columns(project("prod", "id"), project("staging", "order_id"))) == [(MODEL, MODEL, ["id"], ["order_id"], {})]
