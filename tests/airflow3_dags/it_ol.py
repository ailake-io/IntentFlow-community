from datetime import datetime
from airflow.sdk import DAG, Asset
from airflow.providers.standard.operators.bash import BashOperator

orders, sales = Asset("s3://lake/public/orders"), Asset("s3://lake/mart/sales")

with DAG("it_ol", start_date=datetime(2026, 1, 1), schedule=None, catchup=False) as dag:
    # No Airflow 3 um Asset usado só como inlet fica "inativo" e a task falha: alguém precisa produzi-lo (outlet)
    seed = BashOperator(task_id="seed", bash_command="echo seed", outlets=[orders])
    load = BashOperator(task_id="load", bash_command="echo load", inlets=[orders], outlets=[sales])
    seed >> load
