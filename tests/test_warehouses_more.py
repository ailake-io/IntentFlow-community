"""Real Trino (INTENTFLOW_TRINO_HOST) and ClickHouse (INTENTFLOW_CLICKHOUSE_HOST) in containers; Redshift only with a fake connection."""
import os

import pytest

from intentflow import datadiff, monitoring, warehouses
from intentflow.warehouses.more import ClickHouseWarehouse, RedshiftWarehouse, TrinoWarehouse

TRINO = os.environ.get("INTENTFLOW_TRINO_HOST")
CH = os.environ.get("INTENTFLOW_CLICKHOUSE_HOST")


def _seed(wh, base, head, ddl):
    for stmt in ddl:
        wh.con.cursor().execute(stmt) if hasattr(wh.con, "cursor") else wh.con.command(stmt)


@pytest.mark.skipif(not TRINO, reason="real Trino not configured")
def test_trino_real_describe_plan_and_data_diff(monkeypatch):
    host, port = TRINO.split(":")
    for k, v in {"TRINO_HOST": host, "TRINO_PORT": port, "TRINO_CATALOG": "memory", "TRINO_SCHEMA": "shop"}.items():
        monkeypatch.setenv(k, v)
    wh = TrinoWarehouse()
    for stmt in ("create schema if not exists memory.shop", "drop table if exists memory.shop.o_base", "drop table if exists memory.shop.o_head",
                 "create table memory.shop.o_base as select * from (values (1, 10.5, 'paid'), (2, cast(null as decimal(5,1)), 'paid'), (3, 7.0, 'x')) t(id, amount, status)",
                 "create table memory.shop.o_head as select * from (values (1, 10.5, 'paid'), (2, 5.0, 'paid'), (4, 1.0, 'y')) t(id, amount, status)"):
        wh.rows(stmt)
    assert [c for c, _ in wh.describe("memory.shop.o_base")] == ["id", "amount", "status"]
    p = wh.plan("select status, sum(amount) from memory.shop.o_base group by status")
    assert "Aggregate" in p["operators"] and "Output" in p["operators"]
    d = datadiff.diff_relations(wh, "memory.shop.o_base", "memory.shop.o_head", ["id"])
    assert d["only_in_base"] == 1 and d["only_in_head"] == 1 and d["changed_by_column"] == {"amount": 1}
    assert isinstance(wh.memory_failures(), list) and isinstance(wh.spill_report(), list)


@pytest.mark.skipif(not CH, reason="real ClickHouse not configured")
def test_clickhouse_real_describe_plan_and_data_diff(monkeypatch):
    host, port = CH.split(":")
    for k, v in {"CLICKHOUSE_HOST": host, "CLICKHOUSE_PORT": port, "CLICKHOUSE_PASSWORD": os.environ.get("CLICKHOUSE_PASSWORD", ""),
                 "CLICKHOUSE_DATABASE": "shop"}.items():
        monkeypatch.setenv(k, v)
    import clickhouse_connect
    adm = clickhouse_connect.get_client(host=host, port=int(port), username="default", password=os.environ.get("CLICKHOUSE_PASSWORD", ""), database="shop")
    for stmt in ("drop table if exists o_base", "drop table if exists o_head",
                 "create table o_base (id UInt32, amount Nullable(Float64), status String) engine=MergeTree order by id",
                 "create table o_head (id UInt32, amount Nullable(Float64), status String) engine=MergeTree order by id",
                 "insert into o_base values (1,10.5,'paid'),(2,NULL,'paid'),(3,7,'x')",
                 "insert into o_head values (1,10.5,'paid'),(2,5,'paid'),(4,1,'y')"):
        adm.command(stmt)
    wh = ClickHouseWarehouse()
    assert [c for c, _ in wh.describe("o_base")] == ["id", "amount", "status"]
    p = wh.plan("select status, sum(amount) from o_base group by status")
    assert "Aggregating" in p["operators"] and p["max_rows"] == 3
    d = datadiff.diff_relations(wh, "o_base", "o_head", ["id"])
    assert d["only_in_base"] == 1 and d["only_in_head"] == 1
    assert d["changed_rows"] == 1 and d["changed_by_column"] == {"amount": 1}                               # NULL → 5 is a change
    assert isinstance(wh.memory_failures(), list)
    with pytest.raises(Exception):                                         # readonly=1: the server refuses writes
        wh.rows("drop table o_base")


class FakeCur:
    def __init__(self, table):
        self.table, self.description = table, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql):
        self.res = next((v for k, v in self.table.items() if k in sql), [])
        self.description = [("c",)] if self.res else None

    def fetchall(self):
        return self.res


class FakeCon:
    def __init__(self, table):
        self.table = table

    def cursor(self):
        return FakeCur(self.table)


def test_redshift_plan_text_and_spill_with_fake_connection():
    plan = [("XN HashAggregate  (cost=1000.00..1250.50 rows=500 width=40)",), ("  ->  XN Seq Scan on orders  (cost=0.00..100.00 rows=10000 width=40)",)]
    wh = RedshiftWarehouse(connection=FakeCon({"EXPLAIN": plan, "svl_query_summary": [(77, 5_000_000_000)]}))
    p = wh.plan("select 1")
    assert p["max_rows"] == 10000 and p["cost"] == 1250.5 and p["operators"] == ["XN HashAggregate", "XN Seq Scan on orders"]
    sp = wh.spill_report()
    assert sp[0]["scope"] == "77" and sp[0]["spilled_bytes"] == 5_000_000_000
    assert monitoring.warehouse_spill(wh)[0].metric == "spilled_bytes"


def test_factory_picks_engine_and_keeps_legacy_behaviour(tmp_path):
    assert warehouses.connect(lambda k: None) is None
    db = tmp_path / "w.duckdb"
    import duckdb
    duckdb.connect(str(db)).close()
    assert warehouses.connect({"warehouse.duckdb": str(db)}.get).name == "duckdb"
    with pytest.raises(RuntimeError, match="unknown"):
        warehouses.connect({"warehouse.type": "oracle"}.get)
    with pytest.raises(RuntimeError, match="TRINO_HOST"):
        os.environ.pop("TRINO_HOST", None)
        warehouses.connect({"warehouse.type": "trino"}.get)
    assert warehouses.DuckDBWarehouse.quote is not None


def test_cloud_engines_quote_with_backticks_and_clickhouse_uses_not_distinct():
    from intentflow.warehouses.cloud import BigQueryWarehouse
    bq = BigQueryWarehouse.__new__(BigQueryWarehouse)
    assert bq.quote("a") == "`a`"
    ch = ClickHouseWarehouse.__new__(ClickHouseWarehouse)
    assert ch.differs("a", "b") == "ifNull(a != b, isNull(a) != isNull(b))"


BQ = os.environ.get("INTENTFLOW_BIGQUERY_EMULATOR")


@pytest.mark.skipif(not BQ, reason="BigQuery emulator not configured")
def test_bigquery_emulator_describe_plan_and_data_diff():
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery
    from intentflow.warehouses.cloud import BigQueryWarehouse
    c = bigquery.Client(project="intentflow", credentials=AnonymousCredentials(), client_options=ClientOptions(api_endpoint=BQ))
    c.query("create or replace table shop.o_base as select * from unnest([struct(1 as id, 10.5 as amount, 'paid' as status), "
            "struct(2, cast(null as float64), 'paid'), struct(3, 7.0, 'x')])").result()
    c.query("create or replace table shop.o_head as select * from unnest([struct(1 as id, 10.5 as amount, 'paid' as status), "
            "struct(2, 5.0, 'paid'), struct(4, 1.0, 'y')])").result()
    wh = BigQueryWarehouse(client=c)
    assert [n for n, _ in wh.describe("intentflow.shop.o_base")] == ["id", "amount", "status"]
    assert wh.plan("select * from shop.o_base")["cost"] is not None
    d = datadiff.diff_relations(wh, "`intentflow.shop.o_base`", "`intentflow.shop.o_head`", ["id"])      # backticks, not double quotes
    assert (d["only_in_base"], d["only_in_head"], d["changed_by_column"]) == (1, 1, {"amount": 1})


def test_snowflake_and_databricks_describe_and_plan_with_fake_connections():
    from intentflow.warehouses.cloud import DatabricksWarehouse, SnowflakeWarehouse

    class Cur:
        def __init__(self, table):
            self.t, self.res = table, []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql):
            self.res = next((v for k, v in self.t.items() if k in sql), [])

        def fetchall(self):
            return self.res

        def close(self):
            pass

    class Con:
        def __init__(self, t):
            self.t = t

        def cursor(self):
            return Cur(self.t)
    sf = SnowflakeWarehouse(Con({"describe table": [("ID", "NUMBER(38,0)", "COLUMN"), ("AMOUNT", "FLOAT", "COLUMN")],
                                 "EXPLAIN USING JSON": [(json.dumps({"GlobalStats": {"bytesAssigned": 4096},
                                                                     "Operations": [[{"operation": "TableScan"}, {"operation": "Aggregate"}]]}),)]}))
    assert sf.describe("db.s.t") == [("ID", "NUMBER(38,0)"), ("AMOUNT", "FLOAT")]
    assert sf.plan("select 1") == {"operators": ["TableScan", "Aggregate"], "max_rows": None, "cost": 4096.0}
    dbx = DatabricksWarehouse(Con({"describe table": [("id", "int", None), ("amount", "double", None), ("# Partition Information", "", ""), ("part", "string", "")],
                                   "EXPLAIN COST": [("== Optimized Logical Plan ==\nAggregate [..], Statistics(sizeInBytes=1024.0 B, rowCount=10)\n"
                                                     "+- Relation x[id#1] parquet, Statistics(sizeInBytes=2.0 MiB, rowCount=2.00E+4)",)]}))
    assert dbx.describe("t") == [("id", "int"), ("amount", "double")]
    p = dbx.plan("select 1")
    assert p["max_rows"] == 20000 and p["cost"] == 2 * 1024**2
import json  # noqa: E402  (usado pelos fakes acima)


MYSQL = os.environ.get("INTENTFLOW_MYSQL_HOST")


@pytest.mark.skipif(not MYSQL, reason="real MySQL not configured")
def test_mysql_real_describe_plan_and_data_diff(monkeypatch):
    import pymysql
    from intentflow.warehouses.more import MySQLWarehouse
    host, port = MYSQL.split(":")
    for k, v in {"MYSQL_HOST": host, "MYSQL_PORT": port, "MYSQL_USER": "root", "MYSQL_PASSWORD": os.environ.get("MYSQL_PASSWORD", ""), "MYSQL_DATABASE": "shop"}.items():
        monkeypatch.setenv(k, v)
    adm = pymysql.connect(host=host, port=int(port), user="root", password=os.environ.get("MYSQL_PASSWORD", ""), database="shop", autocommit=True)
    with adm.cursor() as c:
        for s in ("drop table if exists o_base", "drop table if exists o_head",
                  "create table o_base (id int primary key, amount decimal(5,1) null, status varchar(10))",
                  "create table o_head (id int primary key, amount decimal(5,1) null, status varchar(10))",
                  "insert into o_base values (1,10.5,'paid'),(2,NULL,'paid'),(3,7,'x')",
                  "insert into o_head values (1,10.5,'paid'),(2,5,'paid'),(4,1,'y')"):
            c.execute(s)
    wh = MySQLWarehouse()
    assert [n for n, _ in wh.describe("o_base")] == ["id", "amount", "status"]
    p = wh.plan("select status, sum(amount) from o_base group by status")
    assert p["max_rows"] == 3 and p["cost"] is not None and any("ALL" in o or "index" in o.lower() for o in p["operators"])
    d = datadiff.diff_relations(wh, "o_base", "o_head", ["id"])
    assert (d["only_in_base"], d["only_in_head"], d["changed_by_column"]) == (1, 1, {"amount": 1})
    with pytest.raises(Exception):                                                   # READ ONLY session
        wh.rows("drop table o_base")
    wh.close()
