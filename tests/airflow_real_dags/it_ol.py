from datetime import datetime
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.lineage.entities import Table

with DAG("it_ol", start_date=datetime(2026, 1, 1), schedule=None, catchup=False) as dag:
    BashOperator(task_id="load", bash_command="echo load",
                 inlets=[Table(database="db", cluster="pg", name="public.orders")],
                 outlets=[Table(database="db", cluster="pg", name="mart.sales")])
