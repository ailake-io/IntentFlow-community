from pathlib import Path

import pytest

pytest.importorskip("sqlfluff")

from intentflow import sqlmesh_loader  # noqa: E402
from intentflow.checks import Context, run_checks  # noqa: E402
from intentflow.checks.lint import lint_sql  # noqa: E402
from intentflow.dbt_loader import DbtNode, Project  # noqa: E402
from intentflow.store import Store  # noqa: E402


def test_rules_fire_on_known_bad_sql():
    sql = ("select * from a, b where a.id = b.id and a.x = null\n"
           "union\nselect distinct c.id from c group by c.id\n")
    rules = {v["rule"] for v in lint_sql(sql, "ansi", ["AM01", "AM02", "AM04", "CV05"])}
    assert {"AM01", "AM02", "AM04", "CV05"} <= rules
    assert {v["rule"] for v in lint_sql(sql, "duckdb", ["AM02"])} == set()        # DuckDB accepts UNION without ALL as DISTINCT


def test_clean_sql_is_silent():
    assert lint_sql("select a.id, b.v from a left join b on a.id = b.id", "duckdb", ["AM04", "RF02", "CV08"]) == []


def test_check_groups_per_model_uses_cache_and_adapter_dialect(tmp_path):
    p = Project(nodes={"m": DbtNode("m", "bad_model", "model", compiled_code="select * from raw.orders o, raw.customers c where o.cid = c.id", path="models/bad.sql"),
                       "g": DbtNode("g", "good_model", "model", compiled_code="select o.id from raw.orders as o")}, adapter="duckdb")
    st = Store(str(tmp_path / "s.db"))
    assert run_checks(Context(project=p, store=st), only=["LINT001"]) == []                # off by default
    st.set_setting("lint.enabled", True)
    f = run_checks(Context(project=p, store=st), only=["LINT001"])
    assert {x.nodes[0] for x in f} == {"m"} and {x.evidence["rule"] for x in f} >= {"AM04", "RF02"}
    am04 = next(x for x in f if x.evidence["rule"] == "AM04")
    assert am04.severity == "medium" and am04.evidence["file"] == "models/bad.sql" and "SELECT *" in am04.detail
    st.flush()
    again = run_checks(Context(project=p, store=st), only=["LINT001"])        # segunda passada vem do cache
    assert [x.id for x in again] == [x.id for x in f]


def test_policy_overrides_rule_list_and_bad_sql_does_not_crash():
    p = Project(nodes={"m": DbtNode("m", "x", "model", compiled_code="select * from a, b"),
                       "q": DbtNode("q", "broken", "model", compiled_code="selec ((( from")}, adapter="postgres")
    import tempfile
    st = Store(str(Path(tempfile.mkdtemp()) / "s.db"))
    st.set_setting("lint.enabled", True)
    f = run_checks(Context(project=p, store=st, policies={"lint_rules": ["AM04"]}), only=["LINT001"])
    assert [x.evidence["rule"] for x in f] == ["AM04"]
