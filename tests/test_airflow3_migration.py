import shutil
from pathlib import Path

from intentflow.checks import Context, run_checks
from intentflow.checks.airflow3 import migrate_dir, rewrite_source, scan_source

T = Path(__file__).resolve().parent

OLD = '''
from datetime import datetime
from airflow import DAG
from airflow.decorators import dag, task
from airflow.operators.python import PythonOperator
from airflow.operators.dummy import DummyOperator
from airflow.operators.subdag import SubDagOperator
from airflow.utils.dates import days_ago
from airflow.datasets import Dataset
from airflow.utils.trigger_rule import TriggerRule
import requests

with DAG("x", schedule_interval="@daily", start_date=days_ago(1)) as d:
    t = PythonOperator(task_id="a", python_callable=f, provide_context=True,
                       op_kwargs={"d": "{{ execution_date }}"}, trigger_rule=TriggerRule.NONE_FAILED_OR_SKIPPED)
def f(**ctx):
    return ctx["execution_date"]
requests.get("http://af:8080/api/v1/dags")
'''


def test_scan_finds_the_documented_breaking_changes():
    rules = {p["rule"] for p in scan_source(OLD)}
    assert {"AF301", "AF302", "AF303", "AF304", "AF305", "AF306", "AF307", "AF309", "AF310", "AF311"} <= rules


def test_modern_dag_is_clean_and_catchup_default_is_flagged_only_when_scheduled():
    ok = '''
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator
with DAG("a", schedule="@daily", catchup=False):
    BashOperator(task_id="t", bash_command="echo {{ logical_date }}")
'''
    assert scan_source(ok) == []
    assert [p["rule"] for p in scan_source(ok.replace("catchup=False", "max_active_runs=1"))] == ["AF307"]
    assert scan_source(ok.replace('schedule="@daily", catchup=False', "schedule=None")) == []


def test_rewrite_is_exact_and_idempotent():
    new, n = rewrite_source(OLD)
    assert n == 3 and "from airflow.sdk import DAG" in new and "from airflow.sdk import dag, task" in new
    assert "from airflow.providers.standard.operators.python import PythonOperator" in new
    assert rewrite_source(new) == (new, 0)
    # mixed import (a name outside the SDK) is left alone
    assert rewrite_source("from airflow.models import DAG, DagRun")[1] == 0


def test_rewritten_dags_equal_the_ones_that_ran_on_real_airflow_3(tmp_path):
    for n in ("it_ok", "it_fail", "it_ol"):
        shutil.copy(T / "airflow_real_dags" / f"{n}.py", tmp_path / f"{n}.py")
    res = migrate_dir(tmp_path, write=True)
    assert all(r["written"] for r in res)
    for n in ("it_ok", "it_fail"):
        assert (tmp_path / f"{n}.py").read_text() == (T / "airflow3_dags" / f"{n}.py").read_text()
        assert (tmp_path / f"{n}.py.af2.bak").exists()
    # it_ol usa Table (removido): sobra item manual
    left = next(r for r in res if r["file"].endswith("it_ol.py"))["remaining"]
    assert [p["rule"] for p in left] == ["AF310"]


def test_check_is_registered_and_cli_exit_codes(tmp_path, capsys):
    from intentflow import cli
    d = tmp_path / "dags"
    d.mkdir()
    shutil.copy(T / "airflow_real_dags/it_ok.py", d / "it_ok.py")
    assert run_checks(Context(airflow_dags_dir=str(d)), only=["AF3"]) == []          # opt-in
    f = run_checks(Context(airflow_dags_dir=str(d), policies={"airflow_target_major": 3}), only=["AF3"])
    assert {x.check for x in f} == {"AF301"} and all(x.evidence["auto_fixable"] for x in f)
    assert cli.main(["airflow3", str(d)]) == 1                      # items left → fails in CI
    assert cli.main(["airflow3", str(d), "--write"]) == 0
    assert cli.main(["airflow3", str(d)]) == 0
    assert "from airflow.sdk import DAG" in (d / "it_ok.py").read_text()
