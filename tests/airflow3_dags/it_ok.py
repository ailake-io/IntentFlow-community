from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator

with DAG("it_ok", start_date=datetime(2026, 1, 1), schedule=None, catchup=False) as dag:
    BashOperator(task_id="hi", bash_command="echo ok")
