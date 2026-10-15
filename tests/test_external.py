"""Jobs fora do SQL (OpenLineage + declarados): impacto, OL001 e CLI."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from intentflow import cli, dbt_loader, external, lineage, lineage_api
from intentflow.checks import Context, run_checks
from intentflow.store import Store
from conftest import new_db


def _decl(tmp_path, jobs):
    p = tmp_path / "intentflow.lineage.json"
    p.write_text(json.dumps({"jobs": jobs}))
    return p


JOB = {"name": "ltv_spark", "owner": "time-ds", "reads": [{"dataset": "customers", "columns": ["email"]}], "writes": ["wh.ltv"]}


def test_declared_file_and_matching(shop_target, tmp_path):
    reads = external.load_declared(_decl(tmp_path, [JOB, {"name": "whole", "reads": [{"dataset": "main.orders"}]}]))
    assert {(r.job, r.column) for r in reads} == {("ltv_spark", "email"), ("whole", "*")}
    proj = dbt_loader.load(shop_target / "target", shop_target)
    assert {proj.nodes[u].name for u, _, _ in external.resolve(proj, reads)} == {"customers", "orders"}
    assert external.match_dataset(proj, "spark:nao_existe") is None


def test_invalid_declared_file(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"jobs": [{"name": "a"}]}')
    with pytest.raises(ValueError):
        external.load_declared(p)


def test_openlineage_reads_become_external(shop_target, tmp_path):
    from intentflow import openlineage
    st = Store(new_db(tmp_path))
    openlineage.ingest_events(st, [{"eventType": "COMPLETE", "eventTime": "2026-01-01T00:00:00Z", "run": {"runId": "r1"},
        "job": {"namespace": "spark", "name": "ltv"},
        "inputs": [{"namespace": "duckdb", "name": "shop.main.customers"}, {"namespace": "duckdb", "name": "shop.main.orders"}],
        "outputs": [{"namespace": "s3", "name": "ltv", "facets": {"columnLineage": {"fields": {
            "score": {"inputFields": [{"namespace": "duckdb", "name": "shop.main.customers", "field": "email"}]}}}}}]}])
    reads = {(r.dataset.split(":")[1], r.column) for r in external.load_openlineage(st)}
    assert ("shop.main.customers", "email") in reads and ("shop.main.orders", "*") in reads      # coluna e dataset inteiro
    assert ("shop.main.customers", "*") not in reads


def test_impact_includes_external_job(shop_target, tmp_path):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    reads = external.load_declared(_decl(tmp_path, [JOB]))
    rows = lineage_api.impact(proj, res, "stg_customers", "email", external=reads)["affected"]
    job = next(r for r in rows if r.get("kind") == "job")
    assert job["name"] == "ltv_spark" and job["owner"] == "time-ds"
    assert not [r for r in lineage_api.impact(proj, res, "stg_customers", "first_name", external=reads)["affected"] if r.get("kind") == "job"]


def test_cli_impact_lists_job(shop_target, tmp_path, capsys):
    d = _decl(tmp_path, [JOB])
    cli.main(["--store", new_db(tmp_path), "lineage", "--target", str(shop_target / "target"), "--column", "stg_customers.email",
              "--impact", "--declared", str(d)])
    assert "job ltv_spark" in capsys.readouterr().out


@pytest.fixture(scope="module")
def renamed(shop_target, tmp_path_factory):
    head = tmp_path_factory.mktemp("ol") / "head"
    shutil.copytree(shop_target, head, ignore=shutil.ignore_patterns("logs"))
    f = head / "models/staging/stg_customers.sql"
    f.write_text(f.read_text().replace("    email,", "    email as email_address,"))
    dbt = str(Path(sys.executable).parent / "dbt")
    for cmd in (["run", "--select", "stg_customers"], ["docs", "generate", "--select", "stg_customers"]):
        subprocess.run([dbt, *cmd, "--profiles-dir", "."], cwd=head, capture_output=True, text=True)
    return head


def test_ol001_flags_external_reader_of_removed_column(shop_target, renamed, tmp_path):
    bp, hp = dbt_loader.load(shop_target / "target", shop_target), dbt_loader.load(renamed / "target", renamed)
    ctx = Context(project=hp, lineage=lineage.build(hp), base_project=bp, base_lineage=lineage.build(bp),
                  external=external.load_declared(_decl(tmp_path, [JOB])))
    f = [x for x in run_checks(ctx, only=["OL001"]) if x.check == "OL001"]
    assert len(f) == 1 and f[0].severity == "critical" and f[0].evidence["job"] == "ltv_spark"
    ctx.external = []
    assert not run_checks(ctx, only=["OL001"])


def test_alias_resolves_ambiguous_and_unknown_names(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    amb = next(n.name for n in proj.nodes.values() if n.name == "customers")
    path = tmp_path / "intentflow.lineage.json"
    path.write_text(json.dumps({"aliases": {"s3://lake/raw/c": "stg_customers"},
                                "jobs": [{"name": "j", "reads": [{"dataset": "s3://lake/raw/c", "columns": ["email"]}]},
                                         {"name": "k", "reads": [{"dataset": "inexistente"}]}]}))
    reads = external.load(None, path)
    assert external.match_dataset(proj, "s3://lake/raw/c") is None           # no match without an alias
    assert {proj.nodes[u].name for u, _, _ in external.resolve(proj, reads)} == {"stg_customers"}
    assert external.unmatched(proj, reads) == {"inexistente": {"reason": "missing", "jobs": ["k"]}}
    assert amb


def test_ambiguous_dataset_is_reported(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    dup = next(u for u, n in proj.nodes.items() if n.name == "orders")
    import copy
    p2 = copy.deepcopy(proj)
    n = copy.deepcopy(p2.nodes[dup])
    n.relation = n.relation.rsplit(".", 2)[0] + '."outro"."orders"'
    p2.nodes["model.shop.orders_b"] = n
    reads = [external.ExtRead("j", "orders", "*", "declared")]
    assert external.unmatched(p2, reads)["orders"]["reason"] == "ambiguous"
    ctx = Context(project=p2, lineage=lineage.build(p2), external=reads)
    assert [f.check for f in run_checks(ctx, only=["OL001"])] == ["OL002"]


def test_job_chain_propagates_through_outputs(shop_target, tmp_path):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    d = _decl(tmp_path, [JOB, {"name": "report", "owner": "bi", "reads": [{"dataset": "wh.ltv"}], "writes": ["wh.report", "customer_contacts"]}])
    rows = lineage_api.impact(proj, res, "stg_customers", "email", external=external.load_declared(d))["affected"]
    names = {r["name"]: r for r in rows}
    assert names["report"]["via_job"] == "ltv_spark" and names["report"]["distance"] > names["ltv_spark"]["distance"]


def test_job_output_that_is_a_project_model_exposes_its_consumers(shop_target, tmp_path):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    d = _decl(tmp_path, [{"name": "enrich", "reads": [{"dataset": "stg_payments", "columns": ["amount"]}], "writes": ["orders"]}])
    rows = lineage_api.impact(proj, res, "stg_payments", "amount", external=external.load_declared(d))["affected"]
    assert any(r["name"] == "enrich" for r in rows)
    assert any(r.get("via_job") == "enrich" for r in rows) or any(r["name"] == "orders" for r in rows)


def test_pii_read_by_external_job(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    reads = external.load_declared(_decl(tmp_path, [{"name": "spark_x", "reads": [{"dataset": "customers", "columns": ["email"]}]}]))
    ctx = Context(project=proj, lineage=lineage.build(proj), external=reads)
    f = [x for x in run_checks(ctx, only=["OL001"]) if x.check == "OL003"]
    assert f and f[0].evidence["job"] == "spark_x"
    ctx.policies = {"pii_allowed_jobs": ["spark_x"]}
    assert not [x for x in run_checks(ctx, only=["OL001"]) if x.check == "OL003"]


def test_spark_actions_are_grouped_by_application(tmp_path):
    from intentflow import openlineage
    st = Store(new_db(tmp_path))
    def ev(name, ins, outs, parent=True):
        rf = {"processing_engine": {"name": "spark"}}
        if parent:
            rf["parent"] = {"job": {"namespace": "ns", "name": "app"}}
        return {"eventType": "COMPLETE", "eventTime": "2026-01-01T00:00:00Z", "run": {"runId": "r", "facets": rf},
                "job": {"namespace": "ns", "name": name}, "inputs": [{"namespace": "f", "name": i} for i in ins],
                "outputs": [{"namespace": "f", "name": o} for o in outs]}
    openlineage.ingest_events(st, [ev("app.cmd1", ["a"], ["b"]), ev("app.cmd2", ["c"], ["d"])])
    jobs = {j["job"]: j for j in st.ol_jobs()}
    assert set(jobs) == {"ns.app"} and set(jobs["ns.app"]["inputs"]) == {"f:a", "f:c"} and set(jobs["ns.app"]["outputs"]) == {"f:b", "f:d"}


def test_column_level_propagation_with_feeds(shop_target, tmp_path):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    d = _decl(tmp_path, [
        {"name": "a", "reads": [{"dataset": "customers", "columns": ["email"], "feeds": [{"dataset": "wh.t", "columns": ["x"]}]}], "writes": ["wh.t"]},
        {"name": "uses_x", "reads": [{"dataset": "wh.t", "columns": ["x"]}]},
        {"name": "uses_y", "reads": [{"dataset": "wh.t", "columns": ["y"]}]},
        {"name": "uses_all", "reads": [{"dataset": "wh.t"}]}])
    names = {r["name"] for r in lineage_api.impact(proj, res, "stg_customers", "email", external=external.load_declared(d))["affected"]}
    assert {"a", "uses_x", "uses_all"} <= names and "uses_y" not in names


def test_path_suffix_disambiguates_same_table_name(shop_target):
    import copy
    proj = dbt_loader.load(shop_target / "target", shop_target)
    p2 = copy.deepcopy(proj)
    uid = next(u for u, n in p2.nodes.items() if n.name == "orders")
    n = copy.deepcopy(p2.nodes[uid])
    n.unique_id, n.relation = "model.shop.orders_b", n.relation.rsplit(".", 2)[0] + '."mart_b"."orders"'
    p2.nodes[n.unique_id] = n
    assert external.match(p2, "orders")[1] == "ambiguous"
    assert external.match(p2, "s3://lake/mart_b/orders.parquet") == ("model.shop.orders_b", "ok")
    assert external.match(p2, "s3://lake/mart_b/nada") == (None, "missing")


def test_group_by_parent_is_opt_in(tmp_path):
    from intentflow import openlineage
    st = Store(new_db(tmp_path))
    e = {"eventType": "COMPLETE", "eventTime": "2026-01-01T00:00:00Z", "run": {"runId": "r", "facets": {"parent": {"job": {"namespace": "af", "name": "dag"}}}},
         "job": {"namespace": "af", "name": "dag.task1"}, "inputs": [{"namespace": "p", "name": "a"}], "outputs": []}
    openlineage.ingest_events(st, [e])
    assert [j["job"] for j in st.ol_jobs()] == ["af.dag.task1"]
    st.set_setting("openlineage.group_by_parent", "true")
    openlineage.ingest_events(st, [{**e, "job": {"namespace": "af", "name": "dag.task2"}}])
    assert "af.dag" in {j["job"] for j in st.ol_jobs()}


def test_unmatched_warning_is_opt_in(shop_target):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    reads = [external.ExtRead("j", "s3://x/nada", "*", "declared")]
    ctx = Context(project=proj, lineage=lineage.build(proj), external=reads)
    assert not run_checks(ctx, only=["OL001"])
    ctx.policies = {"external_warn_unmatched": True}
    assert [f.evidence["reason"] for f in run_checks(ctx, only=["OL001"])] == ["missing"]


def test_output_node_exposes_only_consumers_of_the_fed_column(shop_target, tmp_path):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    j = lambda feeds: _decl(tmp_path, [{"name": "enrich", "reads": [{"dataset": "stg_payments", "columns": ["amount"], "feeds": feeds}],
                                        "writes": ["orders"]}])
    names = lambda d: {r["name"] for r in lineage_api.impact(proj, res, "stg_payments", "amount", external=external.load_declared(d))["affected"]}
    unknown = names(j([]))                                          # without feeds: every consumer of `orders`
    sub = names(j([{"dataset": "orders", "columns": ["order_id"]}]))   # only those reading orders.order_id (and indirect dependency)
    assert "enrich" in unknown and "orders" in unknown
    assert "orders" in sub and sub <= unknown


def test_alias_cli_roundtrip_suggestions_and_coverage(shop_target, tmp_path, capsys):
    db, t = new_db(tmp_path), str(shop_target / "target")
    d = _decl(tmp_path, [{"name": "j", "reads": [{"dataset": "s3://lake/customer"}], "writes": ["wh.x"]}])
    cli.main(["--store", db, "lineage", "--target", t, "--declared", str(d), "--unmatched", "--coverage"])
    out = capsys.readouterr().out
    assert "missing: s3://lake/customer" in out and "suggestion: customers" in out and "conservative propagation: j" in out
    cli.main(["--store", db, "lineage", "--target", t, "--declared", str(d), "--alias", "s3://lake/customer=customers", "--unmatched"])
    assert "missing" not in capsys.readouterr().out
    assert json.loads(d.read_text())["aliases"] == {"s3://lake/customer": "customers"}
    assert cli.main(["--store", db, "lineage", "--target", t, "--declared", str(d), "--alias", "x=nao_existe"]) == 2


def test_default_spark_app_name_is_flagged(shop_target):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    ctx = Context(project=proj, lineage=lineage.build(proj),
                  external=[external.ExtRead("spark-test.pyspark_shell", "customers", "*", "openlineage")])
    assert [f.check for f in run_checks(ctx, only=["OL001"])] == ["OL004"]


def test_dbt_and_own_events_do_not_become_external_jobs(tmp_path):
    from intentflow import openlineage
    st = Store(new_db(tmp_path))
    def ev(prod, name):
        return {"eventType": "COMPLETE", "eventTime": "2026-01-01T00:00:00Z", "producer": prod, "run": {"runId": name}, "job": {"namespace": "n", "name": name},
                "inputs": [{"namespace": "d", "name": "a"}],
                "outputs": [{"namespace": "d", "name": "b", "facets": {"columnLineage": {"fields": {"c": {"inputFields": [
                    {"namespace": "d", "name": "a", "field": "x"}]}}}}}]}
    r = openlineage.ingest_events(st, [ev("https://github.com/OpenLineage/OpenLineage/tree/1.40.0/integration/dbt", "dbtjob"),
                                       ev("https://github.com/OpenLineage/OpenLineage/tree/1.40.0/integration/spark", "sparkjob")])
    assert {j["job"] for j in st.ol_jobs()} == {"n.sparkjob"} and r["column_edges"] == 1 and r["runs"] == 2     # the dbt run is still recorded


def test_sql_job_lineage_is_computed_not_declared(shop_target, tmp_path):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    (tmp_path / "ltv.sql").write_text("select c.customer_id as id, upper(c.email) as email_up, c.first_name from customers c")
    d = _decl(tmp_path, [{"name": "ltv_sql", "sql_file": "ltv.sql", "dialect": "duckdb", "writes": ["wh.ltv"]},
                         {"name": "uses_email_up", "reads": [{"dataset": "wh.ltv", "columns": ["email_up"]}]},
                         {"name": "uses_name", "reads": [{"dataset": "wh.ltv", "columns": ["first_name"]}]}])
    reads = external.load_declared(d, proj)
    assert {(r.column, r.feeds) for r in reads if r.job == "ltv_sql" and r.dataset == "customers"} >= {("email", (("wh.ltv", "email_up"),))}
    names = {r["name"] for r in lineage_api.impact(proj, res, "stg_customers", "email", external=reads)["affected"]}
    assert "ltv_sql" in names and "uses_email_up" in names and "uses_name" not in names      # per-column propagation, without hand-written `feeds`
    assert external.coverage(reads) == [] or all(c["job"] != "ltv_sql" for c in external.coverage(reads))


def test_sql_job_unresolved_falls_back_to_tables(shop_target, tmp_path):
    reads = external.load_declared(_decl(tmp_path, [{"name": "j", "sql": "select * from customers", "dialect": "duckdb", "writes": ["x"]}]))
    assert [(r.dataset, r.column) for r in reads] == [("customers", "*")]
    with pytest.raises(ValueError):
        external.load_declared(_decl(tmp_path, [{"name": "j", "sql_file": "nao_existe.sql"}]))


def test_sql_file_cannot_escape_declaration_directory(tmp_path):
    secret = tmp_path / "segredo.txt"
    secret.write_text("TOKEN=abc123 not sql at all ;;;")
    sub = tmp_path / "decl"
    sub.mkdir()
    for ref in ("../segredo.txt", str(secret), "../../etc/passwd"):
        d = sub / "intentflow.lineage.json"
        d.write_text(json.dumps({"jobs": [{"name": "j", "sql_file": ref, "writes": ["x"]}]}))
        with pytest.raises(ValueError) as e:
            external.load_declared(d)
        assert "abc123" not in str(e.value)
    (sub / "ruim.sql").write_text("TOKEN=abc123 ;;; selec from")
    d.write_text(json.dumps({"jobs": [{"name": "j", "sql_file": "ruim.sql"}]}))
    with pytest.raises(ValueError) as e:
        external.load_declared(d)
    assert "abc123" not in str(e.value)


PY_JOB = '''
from pyspark.sql import SparkSession, functions as F
spark = SparkSession.builder.appName("ltv").getOrCreate()
c = spark.table("customers")
out = (c.select("customer_id", "email", "first_name")
        .withColumnRenamed("email", "contact")
        .withColumn("name_upper", F.upper(F.col("first_name"))))
out.write.mode("overwrite").parquet("s3://lake/marts/ltv")
'''


def test_python_job_columns_and_feeds_are_inferred(shop_target, tmp_path):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    (tmp_path / "ltv.py").write_text(PY_JOB)
    d = _decl(tmp_path, [{"name": "ltv_py", "py_file": "ltv.py"},
                         {"name": "uses_contact", "reads": [{"dataset": "s3://lake/marts/ltv", "columns": ["contact"]}]},
                         {"name": "uses_name", "reads": [{"dataset": "s3://lake/marts/ltv", "columns": ["name_upper"]}]},
                         {"name": "uses_other", "reads": [{"dataset": "s3://lake/marts/ltv", "columns": ["zzz"]}]}])
    reads = external.load_declared(d, proj)
    mine = {(r.column, r.feeds) for r in reads if r.job == "ltv_py"}
    mine = {(c, f) for c, f in mine}
    assert ("email", (("s3://lake/marts/ltv", "contact"),)) in mine                 # exact rename, no superset
    assert ("first_name", (("s3://lake/marts/ltv", "name_upper"),)) in mine
    assert not any(c == "*" for c, _ in mine)                       # known columns, not the whole dataset
    names = {r["name"] for r in lineage_api.impact(proj, res, "stg_customers", "email", external=reads)["affected"]}
    assert {"ltv_py", "uses_contact"} <= names and "uses_other" not in names


def test_python_job_with_embedded_sql_and_params(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    (tmp_path / "j.py").write_text('spark.sql(f"insert into wh.out select c.email as e from {schema}.customers c where ds = \'{ds}\'")\n')
    d = _decl(tmp_path, [{"name": "j", "py_file": "j.py", "params": {"ds": "2026-01-01"}}])
    reads = external.load_declared(d, proj)
    got = {(r.dataset.split(".")[-1], r.column, r.feeds) for r in reads}
    assert got == {("customers", "email", (("wh.out", "e"),)), ("customers", "ds", (("wh.out", "*"),))}      # coluna do WHERE: indireta


def test_sql_macros_params_and_multi_statement_script(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    sql = ("create table wh.a as select email as e from ${db}.customers;\n"
           "{% if x %}{% endif %}\n"
           "insert into wh.b select o.status from {{ ref('orders') }} o join {{ source('raw', 'raw_payments') }} p on p.order_id = o.order_id;")
    (tmp_path / "s.sql").write_text(sql)
    d = _decl(tmp_path, [{"name": "s", "sql_file": "s.sql", "dialect": "duckdb"}])
    reads = external.load_declared(d, proj)
    got = {(r.dataset, r.column, r.feeds) for r in reads}
    assert ("__param_db__.customers", "email", (("wh.a", "e"),)) in got           # a variable without a value becomes an identifier
    assert ("orders", "status", (("wh.b", "status"),)) in got
    assert ("raw.raw_payments", "order_id", (("wh.b", "*"),)) in got                  # a JOIN-only column: affects the whole output
    assert external.match_dataset(proj, "__param_db__.customers") is not None       # casa pelo sufixo
    d.write_text(json.dumps({"jobs": [{"name": "s", "sql_file": "s.sql", "dialect": "duckdb", "params": {"db": "main"}}]}))
    assert any(r.dataset == "main.customers" for r in external.load_declared(d, proj))


def test_py_file_stays_inside_declaration_dir(tmp_path):
    sub = tmp_path / "d"
    sub.mkdir()
    (tmp_path / "x.py").write_text("print(1)")
    d = sub / "intentflow.lineage.json"
    d.write_text(json.dumps({"jobs": [{"name": "j", "py_file": "../x.py"}]}))
    with pytest.raises(ValueError):
        external.load_declared(d)
    (sub / "bad.py").write_text("def (:")
    d.write_text(json.dumps({"jobs": [{"name": "j", "py_file": "bad.py"}]}))
    with pytest.raises(ValueError):
        external.load_declared(d)


def test_param_rendering_is_linear_on_adversarial_input():
    import time
    for bad in ("{%" * 400_000, "{{" + " " * 900_000, "${" + "a" * 900_000, "{{" + "a " * 450_000, "{#" * 400_000 + "{%"):
        t = time.time()
        external.render_sql(bad)
        assert time.time() - t < 2.0
    assert external.render_sql("select {{ ref('m') }} from ${db}.t where {x}=1", {"db": "d"})[0] == "select m from d.t where __param_x__=1"


def _py(tmp_path, proj, files, name="j.py", params=None):
    for fn, src in files.items():
        (tmp_path / fn).write_text(src)
    job = {"name": "j", "py_file": name, **({"params": params} if params else {})}
    return {(r.dataset, r.column, r.feeds) for r in external.load_declared(_decl(tmp_path, [job]), proj)}


def test_python_functions_imports_and_dynamic_sql(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    got = _py(tmp_path, proj, {
        "helpers.py": "def mask(df):\n    return df.withColumn('email_h', F.sha2(F.col('email'), 256)).drop('email')\n",
        "j.py": ("from helpers import mask\n"
                 "def load(spark):\n    return spark.table('customers').select('customer_id', 'email')\n"
                 "def main(spark):\n"
                 "    df = mask(load(spark))\n"
                 "    q = 'insert into wh.t select order_id, status from ' + 'orders' + ' where status = ' + \"'x'\"\n"
                 "    spark.sql(q)\n"
                 "    df.write.saveAsTable('wh.masked')\n"
                 "main(spark)\n")})
    assert ("customers", "email", (("wh.masked", "email_h"),)) in got              # function + sibling module + drop
    assert ("customers", "customer_id", (("wh.masked", "customer_id"),)) in got
    assert not any(c == "email" and f == (("wh.masked", "email"),) for _, c, f in got)       # `email` foi descartada
    assert ("orders", "status", (("wh.t", "status"),)) in got and ("orders", "status", (("wh.t", "*"),)) in got    # SQL concatenado


def test_python_groupby_filter_join_and_pandas(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    got = _py(tmp_path, proj, {"j.py": (
        "o = spark.table('orders').filter(F.col('status') == 'ok')\n"
        "p = spark.table('stg_payments')\n"
        "j = o.join(p, 'order_id').groupBy('customer_id').agg(F.sum('amount').alias('total'))\n"
        "j.write.parquet('wh/totals')\n"
        "import pandas as pd\n"
        "d = pd.read_csv('customers')\n"
        "d['full'] = d['first_name'] + d['last_name']\n"
        "d.to_parquet('wh/names')\n")})
    assert ("stg_payments", "amount", (("wh/totals", "total"),)) in got
    assert any(ds == "orders" and c == "status" and f == (("wh/totals", "*"),) for ds, c, f in got)       # filtro: indireta
    assert any(c == "order_id" and f == (("wh/totals", "*"),) for _, c, f in got)                          # join: indirect
    assert ("customers", "first_name", (("wh/names", "full"),)) in got                                    # pandas


def test_python_unknown_operation_goes_conservative(shop_target, tmp_path):
    proj = dbt_loader.load(shop_target / "target", shop_target)
    got = _py(tmp_path, proj, {"j.py": (
        "df = spark.table('customers').select('email', 'first_name').mapInPandas(fn, 'a string')\n"
        "df.write.parquet('wh/x')\n")})
    assert ("customers", "email", (("wh/x", "*"),)) in got                          # opaque: the column feeds any output


def test_risks_report_is_clear_and_actionable(shop_target, tmp_path, capsys):
    from intentflow import risks
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    d = _decl(tmp_path, [{"name": "spark_x", "reads": [{"dataset": "customers", "columns": ["email"]}], "writes": ["s3://x/ltv"]},
                         {"name": "pyspark_shell", "owner": "ds", "reads": [{"dataset": "nao_existe"}]}])
    st = Store(new_db(tmp_path))
    rep = risks.build(proj, res, st, declared=str(d), reads=external.load_declared(d, proj))
    areas = {i["area"] for i in rep["items"]}
    assert {"process", "external_jobs"} <= areas
    proc = next(i for i in rep["items"] if i["what"].startswith("no `--base`"))
    assert proc["severity"] == "high" and "PR gate" in proc["breaks"] and "--base" in proc["fix"]
    assert any("without an owner" in i["what"] for i in rep["items"] if i["area"] == "external_jobs")
    md = risks.to_markdown(rep)
    assert "breaks:" in md and "fix:" in md
    cli.main(["--store", new_db(tmp_path), "risks", "--target", str(shop_target / "target"), "--declared", str(d), "--fail-on-high"])
    assert "What can break" in capsys.readouterr().out


def test_python_analysis_is_bounded_on_adversarial_code(shop_target, tmp_path):
    import time
    proj = dbt_loader.load(shop_target / "target", shop_target)
    fan = ["def f0(df):\n    return df\n"] + [f"def f{i}(df):\n    return f{i-1}(f{i-1}(f{i-1}(f{i-1}(df))))\n" for i in range(1, 12)]
    src = ("df = spark.table('customers')\n" + "".join(fan) + "f11(df).write.parquet('wh/x')\n"
           "s = 'select 1'\n" + "".join("s = s + s\n" for _ in range(40)) + "spark.sql(s)\n")
    t = time.time()
    got = _py(tmp_path, proj, {"j.py": src})
    assert time.time() - t < 10
    assert any(ds == "customers" for ds, _, _ in got)                         # budget exceeded: still conservative, not empty
    huge = "x = [" + ",".join("1" for _ in range(300_000)) + "]\n"
    with pytest.raises(ValueError):
        _py(tmp_path, proj, {"j.py": huge})
