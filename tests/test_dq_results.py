"""Real Great Expectations 1.24 and Soda Core 3.5: the fixtures are outputs they wrote (tests/fixtures)."""
import json
from pathlib import Path

from intentflow import dq_results, runner, sqlmesh_loader
from intentflow.checks import Context, run_checks
from intentflow.store import Store

FX = Path(__file__).parent / "fixtures"


def _project():
    return sqlmesh_loader.from_export(json.loads((FX / "sqlmesh_shop_export.json").read_text()))


def test_great_expectations_validation_is_parsed():
    rs = dq_results.parse(json.loads((FX / "gx_validation.json").read_text()))
    by = {(r.column, r.check): r for r in rs}
    assert {r.table for r in rs} == {"fct_orders"} and len(rs) == 5
    assert by[("amount", "expect_column_values_to_not_be_null")].status == "fail"
    assert by[("amount", "expect_column_values_to_not_be_null")].observed["unexpected_count"] == 1
    assert by[(None, "expect_table_row_count_to_be_between")].status == "pass"
    assert {r.status for r in rs} == {"pass", "fail"} and sum(r.status == "fail" for r in rs) == 4


def test_soda_scan_is_parsed_with_readable_names():
    rs = dq_results.parse(json.loads((FX / "soda_scan.json").read_text()))
    names = {r.check: r for r in rs}
    assert names["amount non-negative"].status == "fail" and names["amount non-negative"].observed["value"] == -5.0
    assert names["row_count > 0"].status == "pass" and all("\n" not in r.check for r in rs)
    assert {r.table for r in rs} == {"fct_orders"} and {r.tool for r in rs} == {"soda"}


def test_failures_become_findings_linked_to_the_graph():
    p = _project()
    res = dq_results.load_all(str(FX))
    ctx = Context(project=p, dq_results=res)
    fs = run_checks(ctx, only=["DQ001"])
    assert len(fs) == 8                                   # 4 do GX + 4 do Soda
    assert all(f.nodes == ["model.sqlmesh.fct_orders"] for f in fs)
    gx_null = next(f for f in fs if "expect_column_values_to_not_be_null" in f.title)
    assert gx_null.evidence["downstream"] == 1 and "Great Expectations" in gx_null.title
    assert {f.severity for f in fs} <= {"high", "critical"}


def test_unmapped_table_still_reported_and_bad_files_ignored(tmp_path):
    doc = json.loads((FX / "soda_scan.json").read_text())
    for c in doc["checks"]:
        c["table"] = "tabela_inexistente"
    (tmp_path / "s.json").write_text(json.dumps(doc))
    (tmp_path / "lixo.json").write_text("{nao é json")
    (tmp_path / "outro.json").write_text(json.dumps({"qualquer": 1}))
    fs = run_checks(Context(project=_project(), dq_results=dq_results.load_all(str(tmp_path))), only=["DQ001"])
    assert fs and all(f.nodes == [] and f.evidence["unmapped_table"] == "tabela_inexistente" for f in fs)


def test_setting_drives_the_normal_check_run(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("dq.results", str(FX / "gx_validation.json") + "," + str(FX / "soda_scan.json"))
    assert len(dq_results.load_all(st.get_setting("dq.results"))) == 10
