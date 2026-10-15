from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator

with DAG("it_fail", start_date=datetime(2026, 1, 1), schedule=None, catchup=False,
         default_args={"retries": 0}) as dag:
    BashOperator(task_id="boom", bash_command="exit 3")
