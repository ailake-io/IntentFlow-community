import time

import duckdb
import pytest

from intentflow import datadiff, dbt_loader, lineage
from intentflow.checks import Context, run_checks
from intentflow.orchestrators import Run
from intentflow.warehouses import DuckDBWarehouse


@pytest.fixture()
def wh(tmp_path):
    db = tmp_path / "d.duckdb"
    c = duckdb.connect(str(db))
    c.execute("create schema prod; create schema dev;")
    c.execute("create table prod.orders as select * from (values (1,'a',10.0),(2,'b',20.0),(3,'c',30.0)) t(id,status,amount)")
    c.execute("create table dev.orders as select * from (values (1,'a',10.0),(2,'b',25.0),(4,'d',40.0)) t(id,status,amount)")
    c.close()
    return DuckDBWarehouse(str(db), read_only=False)


def test_data_diff_reports_rows_changes_and_samples(wh):
    d = datadiff.diff_relations(wh, "prod.orders", "dev.orders", ["id"])
    assert d["rows"] == {"base": 3, "head": 3}
    assert (d["only_in_base"], d["only_in_head"]) == (1, 1)
    assert d["changed_rows"] == 1 and d["changed_by_column"] == {"amount": 1}
    assert d["samples"][0]["base_amount"] == 20.0 and d["samples"][0]["head_amount"] == 25.0
    assert not d["identical"]
    assert "only in base: 1" in datadiff.to_markdown(d)


def test_data_diff_identical_and_schema(wh):
    wh.con.execute("create table dev.same as select * from prod.orders")
    assert datadiff.diff_relations(wh, "prod.orders", "dev.same", ["id"])["identical"] is True
    wh.con.execute("create table dev.wide as select *, 1 as extra from prod.orders")
    d = datadiff.diff_relations(wh, "prod.orders", "dev.wide", ["id"])
    assert d["schema"]["added"] == ["extra"] and not d["identical"]
    with pytest.raises(ValueError):
        datadiff.diff_relations(wh, "prod.orders", "dev.wide", ["nope"])


def test_cost_check_flags_cross_product_only(lab_target):
    p = dbt_loader.load(lab_target / "target", lab_target)
    wh = DuckDBWarehouse(str(lab_target / "target" / "lab.duckdb"))
    f = run_checks(Context(project=p, lineage=lineage.build(p), warehouse=wh, policies={}), only=["SQL013"])
    names = {p.nodes[x.nodes[0]].name for x in f}
    assert "bad_cross" in names and "bad_comma_join" in names
    assert "lin_window" not in names


def test_cost_check_row_limit_policy(lab_target):
    p = dbt_loader.load(lab_target / "target", lab_target)
    wh = DuckDBWarehouse(str(lab_target / "target" / "lab.duckdb"))
    f = run_checks(Context(project=p, lineage=lineage.build(p), warehouse=wh, policies={"max_estimated_rows": 3}), only=["SQL013"])
    assert any("estimate" in x.title for x in f)


def test_sla_and_staleness():
    now = time.time()
    runs = [Run("airflow", "etl", str(i), "success", now - 3600 * (10 - i), now - 3600 * (10 - i) + 540, 540.0) for i in range(8)]
    pol = {"sla.airflow:etl": {"minutes": 10}}
    f = run_checks(Context(runs=runs, policies=pol), only=["RUN003"])
    assert f and f[0].severity == "medium" and "p90" in f[0].title            # 9 min de 10 → margem pequena
    runs.append(Run("airflow", "etl", "x", "success", now - 100, now - 100 + 900, 900.0))
    assert run_checks(Context(runs=runs, policies=pol), only=["RUN003"])[0].severity == "high"   # 15 min > SLA
    old = [Run("kestra", "ns.f", "1", "success", now - 3600 * 50, now - 3600 * 50 + 5, 5.0)]
    s = run_checks(Context(runs=old, policies={"max_age_hours.kestra:ns.f": 24}), only=["RUN004"])
    assert s and "no success" in s[0].title


def test_sampled_diff_is_consistent_between_sides_and_estimates_total(tmp_path):
    import duckdb
    from intentflow.warehouses import DuckDBWarehouse
    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table a as select i as id, i % 7 as v from range(100000) t(i)")
    con.execute("create table b as select i as id, case when i % 10 = 0 then -1 else i % 7 end as v from range(100000) t(i)")
    con.close()
    wh = DuckDBWarehouse(str(db))
    d = datadiff.diff_relations(wh, "a", "b", ["id"], sample_pct=10)
    assert 0.05 * 100000 < d["rows"]["base"] < 0.15 * 100000 and d["rows"]["base"] == d["rows"]["head"]   # mesmas chaves nos dois lados
    assert d["only_in_base"] == 0 and d["only_in_head"] == 0
    assert abs(d["sampled"]["changed_rows"] - 10000) < 2000                                               # 10 % of the rows changed
    assert d["changed_by_column"]["v"] == d["changed_rows"] and "Sample" in datadiff.to_markdown(d)
    assert "sampled" not in datadiff.diff_relations(wh, "a", "b", ["id"])
