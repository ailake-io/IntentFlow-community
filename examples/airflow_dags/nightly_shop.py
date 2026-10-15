from datetime import datetime

from airflow import DAG
from airflow.models import Variable
from airflow.operators.bash_operator import BashOperator

token = Variable.get("shop_api_token")          # roda a cada parse do scheduler

with DAG(
    dag_id="nightly_shop",
    start_date=datetime(2021, 1, 1),
    schedule_interval="@daily",
    catchup=True,
) as dag:
    BashOperator(task_id="dbt_build", bash_command="dbt build --select +customers")
