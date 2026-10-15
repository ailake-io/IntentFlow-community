"""Portão antes do dbt: `intentflow gate` sai com código != 0 se houver achado crítico no estágio pre-run."""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG("gated_dbt", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False, max_active_runs=1,
         default_args={"retries": 1, "retry_delay": timedelta(minutes=5), "execution_timeout": timedelta(hours=1),
                       "email_on_failure": True}) as dag:
    gate = BashOperator(task_id="intentflow_gate", bash_command="intentflow gate --stage pre-run --fail-on critical")
    build = BashOperator(task_id="dbt_build", bash_command="dbt build")
    gate >> build
