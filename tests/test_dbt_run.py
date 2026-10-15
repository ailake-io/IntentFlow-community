import shutil, subprocess, sys
from pathlib import Path

import pytest

from intentflow import dbt_loader, dbt_results, lineage, runner
from intentflow.checks import Context, run_checks
from intentflow.store import Store


@pytest.fixture(scope="module")
def failing_run(lab_target, tmp_path_factory):
    """Lab with a `unique` test at `error` severity + a model that breaks: real dbt run_results."""
    d = tmp_path_factory.mktemp("failrun") / "lab"
    shutil.copytree(lab_target, d, ignore=shutil.ignore_patterns("logs"))
    sch = d / "models/schema.yml"
    sch.write_text(sch.read_text().replace("severity: warn", "severity: error"))
    (d / "models/broken.sql").write_text("select no_such_column from {{ ref('users') }}\n")
    (d / "models/after_broken.sql").write_text("select * from {{ ref('broken') }}\n")
    dbt = str(Path(sys.executable).parent / "dbt")
    r = subprocess.run([dbt, "build", "--profiles-dir", "."], cwd=d, capture_output=True, text=True)
    assert r.returncode != 0
    return d


def test_failing_tests_and_models_become_findings(failing_run):
    d = failing_run
    res = dbt_results.load(d / "target")
    assert res["command"] == "build" and {"fail", "error", "skipped"} <= {r["status"] for r in res["results"]}
    p = dbt_loader.load(d / "target", d)
    f = run_checks(Context(project=p, lineage=lineage.build(p), dbt_results=res, store=Store()), only=["DBTR"])
    by = {x.check: x for x in f}
    assert "dup_keys.id" in by["DBTR001"].title and "failed" in by["DBTR001"].title and by["DBTR001"].evidence["failures"] == 1
    assert "broken" in by["DBTR002"].title
    assert "did not run" in by["DBTR003"].title
    s = dbt_results.summary(res, p)
    assert s["counts"]["fail"] >= 1 and s["failing"]


def test_slow_model_regression_uses_history(failing_run):
    d = failing_run
    res = dbt_results.load(d / "target")
    p = dbt_loader.load(d / "target", d)
    st = Store()
    uid = next(u for u in p.nodes if p.nodes[u].name == "users")
    for i in range(6):
        st.add_history(f"dbt_time:{uid}", i, 2.0)
    for r in res["results"]:
        if r["unique_id"] == uid:
            r["time"], r["status"] = 20.0, "success"
    f = run_checks(Context(project=p, lineage=lineage.build(p), dbt_results=res, store=st), only=["DBTR004"])
    assert f and "users" in f[0].title and f[0].evidence["median"] == 2.0


def test_runner_ingests_results_once_and_exposes_summary(failing_run, tmp_path):
    st = Store()
    st.set_setting("dbt.target", str(failing_run / "target"))
    runner.run_check(st, only=["DBTR"])
    runner.run_check(st, only=["DBTR"])
    uid = next(k for k in st.con.execute("select distinct key from history where key like 'dbt_time:%'").fetchall())[0]
    assert len(st.history(uid)) == 1                          # the same invocation does not duplicate the history
    from intentflow import overview
    ov = overview.build(st)
    assert ov["dbt_run"]["command"] == "build" and ov["dbt_run"]["counts"].get("fail")
