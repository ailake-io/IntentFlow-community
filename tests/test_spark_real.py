"""Real Spark → OpenLineage → receiver → OL001. Opt-in: downloads nothing.

INTENTFLOW_SPARK_PYTHON  Python 3.9–3.12 with pyspark 3.5 (e.g. `uv venv -p 3.12 v && uv pip install pyspark==3.5.3`; Java 17/21 on PATH).
                         PySpark 3.5 cannot serialize Python objects on 3.14.
INTENTFLOW_OL_SPARK_JAR  openlineage-spark_2.12-*.jar (Maven Central, io.openlineage)
"""
import os
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from intentflow import dbt_loader, external, lineage, openlineage
from intentflow.checks import Context, run_checks
from intentflow.store import Store
from conftest import new_db

PY, JAR = os.environ.get("INTENTFLOW_SPARK_PYTHON"), os.environ.get("INTENTFLOW_OL_SPARK_JAR")
MASTER = os.environ.get("INTENTFLOW_SPARK_MASTER", "local[1]")      # ex.: local-cluster[1,1,1024] (executores em JVMs separadas)
pytestmark = pytest.mark.skipif(not (PY and JAR and Path(JAR).is_file() and shutil.which("java")),
                                reason="Spark/OpenLineage unavailable (see docstring)")

JOB = """
import sys
from pyspark.sql import SparkSession
url, d, jar, mk, master = sys.argv[1:6]
b = SparkSession.builder.master(master).appName("ltv_spark").config("spark.ui.enabled", "false")
if mk == "mk":
    sp = b.getOrCreate()
    sp.createDataFrame([(1, "a@x"), (2, "b@x")], ["id", "email"]).write.parquet(d + "/customers")     # dados Python
else:
    sp = (b.config("spark.jars", jar).config("spark.extraListeners", "io.openlineage.spark.agent.OpenLineageSparkListener")
          .config("spark.openlineage.transport.type", "http").config("spark.openlineage.transport.url", url)
          .config("spark.openlineage.namespace", "spark-test").getOrCreate())
    sp.read.parquet(d + "/customers").selectExpr("id", "upper(email) as email_up").write.mode("overwrite").parquet(d + "/ltv")
sp.stop()
"""


def test_real_spark_job_is_seen_as_external_reader(shop_target, tmp_path):
    db = new_db(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), openlineage.make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    script = tmp_path / "job.py"
    script.write_text(JOB)
    data = tmp_path / "data"
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for mode in ("mk", "run"):
            r = subprocess.run([PY, str(script), url, str(data), JAR, mode, MASTER], capture_output=True, text=True, timeout=240,
                               env={**os.environ, "PYSPARK_PYTHON": PY})
            assert r.returncode == 0, r.stderr[-1500:]
    finally:
        srv.shutdown()
    with Store(db) as st:
        reads = external.load_openlineage(st)
    assert any(r.column == "email" and r.dataset.endswith("/customers") for r in reads)        # aresta de coluna real do Spark
    assert {r.job for r in reads} == {"spark-test.ltv_spark"}                                   # actions grouped by application

    head = tmp_path / "head"
    shutil.copytree(shop_target, head, ignore=shutil.ignore_patterns("logs"))
    f = head / "models/staging/stg_customers.sql"
    f.write_text(f.read_text().replace("    email,", "    email as email_address,"))
    dbt = str(Path(sys.executable).parent / "dbt")
    for cmd in (["run", "--select", "stg_customers"], ["docs", "generate", "--select", "stg_customers"]):
        subprocess.run([dbt, *cmd, "--profiles-dir", "."], cwd=head, capture_output=True, text=True)
    # the Spark dataset is a `.../customers` path; it matches the `customers` model, which reads stg_customers.email
    bp, hp = dbt_loader.load(shop_target / "target", shop_target), dbt_loader.load(head / "target", head)
    ctx = Context(project=hp, lineage=lineage.build(hp), base_project=bp, base_lineage=lineage.build(bp), external=reads)
    # the job reads customers.email; removing stg_customers.email reaches customers.email through lineage
    found = [x for x in run_checks(ctx, only=["OL001"]) if x.check == "OL001"]
    assert any(x.evidence["column"] == "email" and x.evidence["job"] == "spark-test.ltv_spark" and x.severity == "critical" for x in found)
