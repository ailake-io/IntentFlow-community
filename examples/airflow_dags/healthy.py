from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

default_args = {"retries": 2, "retry_delay": timedelta(minutes=5), "execution_timeout": timedelta(hours=1),
                "email_on_failure": True}

with DAG(dag_id="healthy", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False, max_active_runs=1,
         default_args=default_args) as dag:
    BashOperator(task_id="dbt_build", bash_command="dbt build")
