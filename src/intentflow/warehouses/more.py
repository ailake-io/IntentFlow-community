"""Trino, ClickHouse and Redshift (read-only, the driver is imported only when used).

  Trino      : TRINO_HOST, [TRINO_PORT=8080, TRINO_USER=intentflow, TRINO_CATALOG, TRINO_SCHEMA, TRINO_PASSWORD → https]
  ClickHouse : CLICKHOUSE_HOST, [CLICKHOUSE_PORT=8123, CLICKHOUSE_USER=default, CLICKHOUSE_PASSWORD, CLICKHOUSE_DATABASE, CLICKHOUSE_SECURE]
  Redshift   : REDSHIFT_URL (postgresql://…, Postgres protocol)
Verification: Trino and ClickHouse in real containers; Redshift only with a fake connection (there is no emulator).
"""
from __future__ import annotations

from ..i18n import tr

import json
import os
import re

from . import PostgresWarehouse, Warehouse


def _num(x):
    try:
        v = float(x)
        return v if v == v else None          # NaN → None
    except (TypeError, ValueError):
        return None


class TrinoWarehouse(Warehouse):
    dialect, name = "trino", "trino"
    def _bucket(self, text):
        return f"abs(mod(from_big_endian_64(xxhash64(to_utf8({text}))), 10000))"

    MEM_ERRORS = ("EXCEEDED_LOCAL_MEMORY_LIMIT", "EXCEEDED_GLOBAL_MEMORY_LIMIT", "EXCEEDED_SPILL_LIMIT", "CLUSTER_OUT_OF_MEMORY")

    def __init__(self, connection=None):
        if connection is None:
            import trino  # type: ignore
            host = os.environ.get("TRINO_HOST")
            if not host:
                raise RuntimeError(tr("variable {name} is not set", name="TRINO_HOST"))
            pw = os.environ.get("TRINO_PASSWORD")
            kw = dict(host=host, port=int(os.environ.get("TRINO_PORT", "8080")), user=os.environ.get("TRINO_USER", "intentflow"),
                      catalog=os.environ.get("TRINO_CATALOG"), schema=os.environ.get("TRINO_SCHEMA"))
            if pw:
                kw.update(http_scheme="https", auth=trino.auth.BasicAuthentication(kw["user"], pw))
            connection = trino.dbapi.connect(**kw)
        self.con = connection

    def rows(self, sql):
        cur = self.con.cursor()
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]

    def describe(self, rel):
        return [(r[0], str(r[1])) for r in self.rows(f"describe {rel}")]

    def plan(self, sql):
        raw = self.rows("EXPLAIN (TYPE DISTRIBUTED, FORMAT JSON) " + sql)[0][0]
        frags = json.loads(raw) if isinstance(raw, str) else raw
        ops, rows, cost = [], [], []

        def walk(n):
            ops.append(n.get("name", ""))
            for e in n.get("estimates") or []:
                if _num(e.get("outputRowCount")) is not None:
                    rows.append(int(_num(e["outputRowCount"])))
                if _num(e.get("cpuCost")):
                    cost.append(_num(e["cpuCost"]))
            for c in n.get("children", []):
                walk(c)
        for f in (frags.values() if isinstance(frags, dict) and "name" not in frags else [frags]):
            walk(f)
        return {"operators": ops, "max_rows": max(rows) if rows else None, "cost": max(cost) if cost else None}

    def memory_failures(self, hours: int = 24) -> list[dict]:
        """Queries that died from the memory limit (Trino does not expose spill bytes in `runtime.queries`)."""
        inl = ", ".join(f"'{e}'" for e in self.MEM_ERRORS)
        try:
            r = self.rows(f"select query_id, substr(query, 1, 200), error_code from system.runtime.queries "
                          f"where error_code in ({inl}) and \"end\" > current_timestamp - interval '{int(hours)}' hour order by \"end\" desc limit 50")
        except Exception:
            return []
        return [{"source": "trino", "scope": qid, "detail": f"{code}: {txt}"} for qid, txt, code in r]


class RedshiftWarehouse(PostgresWarehouse):
    """Postgres protocol, but without EXPLAIN JSON: the plan comes from the text (`cost=a..b rows=N`)."""
    dialect, name = "redshift", "redshift"

    def _bucket(self, text):
        return f"abs(mod(fnv_hash({text}), 10000))"

    SPILL_SQL = ("select query, sum(bytes) from svl_query_summary where is_diskbased = 't' and starttime > dateadd(hour, -{hours}, getdate()) "
                 "group by query order by 2 desc limit 50")

    def __init__(self, url_env: str = "REDSHIFT_URL", connection=None):
        if connection is not None:
            self.con = connection
        else:
            super().__init__(url_env)

    def plan(self, sql):
        txt = "\n".join(str(r[0]) for r in self.rows("EXPLAIN " + sql))
        ops = [m.group(1).strip() for m in re.finditer(r"^\s*(?:->\s*)?(XN [\w ]+?)\s+\(cost", txt, re.M)]
        rows = [int(m) for m in re.findall(r"rows=(\d+)", txt)]
        cost = [float(m) for m in re.findall(r"cost=[\d.]+\.\.([\d.]+)", txt)]
        return {"operators": ops, "max_rows": max(rows) if rows else None, "cost": max(cost) if cost else None}

    def describe(self, rel):
        with self.con.cursor() as c:
            c.execute(f"select * from {rel} where 1=0")
            return [(d.name, str(d.type_code)) for d in c.description]

    def spill_report(self, hours=24):
        try:
            r = self.rows(self.SPILL_SQL.format(hours=int(hours)))
        except Exception:
            return []
        return [{"source": "redshift", "scope": str(q), "spilled_bytes": int(b or 0), "detail": "svl_query_summary is_diskbased"} for q, b in r]


class ClickHouseWarehouse(Warehouse):
    dialect, name = "clickhouse", "clickhouse"
    def _bucket(self, text):
        return f"(cityHash64({text}) % 10000)"


    def __init__(self, client=None):
        if client is None:
            import clickhouse_connect  # type: ignore
            host = os.environ.get("CLICKHOUSE_HOST")
            if not host:
                raise RuntimeError(tr("variable {name} is not set", name="CLICKHOUSE_HOST"))
            secure = os.environ.get("CLICKHOUSE_SECURE", "").lower() in ("1", "true", "yes")
            client = clickhouse_connect.get_client(
                host=host, port=int(os.environ.get("CLICKHOUSE_PORT", "8443" if secure else "8123")), username=os.environ.get("CLICKHOUSE_USER", "default"),
                password=os.environ.get("CLICKHOUSE_PASSWORD", ""), database=os.environ.get("CLICKHOUSE_DATABASE", "default"), secure=secure,
                settings={"readonly": 2, "join_use_nulls": 1})   # readonly=2: no writes, but settings can be adjusted; join_use_nulls: a correct anti-join
        self.con = client

    def rows(self, sql):
        return [tuple(r) for r in self.con.query(sql).result_rows]

    def differs(self, a, b):                      # 24.x: IS [NOT] DISTINCT FROM only exists in JOIN ON
        return f"ifNull({a} != {b}, isNull({a}) != isNull({b}))"

    def describe(self, rel):
        return [(r[0], str(r[1])) for r in self.rows(f"describe table {rel}")]

    def plan(self, sql):
        ops: list[str] = []

        def walk(n):
            ops.append(n.get("Node Type", ""))
            for c in n.get("Plans", []):
                walk(c)
        raw = self.rows("EXPLAIN json = 1 " + sql)[0][0]
        for item in (json.loads(raw) if isinstance(raw, str) else raw):
            walk(item.get("Plan", {}))
        rows = None
        try:                                      # EXPLAIN ESTIMATE: (database, table, parts, rows, marks)
            rows = sum(int(r[3]) for r in self.rows("EXPLAIN ESTIMATE " + sql)) or None
        except Exception:
            pass
        return {"operators": ops, "max_rows": rows, "cost": None}

    def spill_report(self, hours=24):
        try:
            r = self.rows(
                "select query_id, substring(query, 1, 200), "
                "ProfileEvents['ExternalProcessingCompressedBytesTotal'] + ProfileEvents['ExternalSortCompressedBytes'] + ProfileEvents['ExternalAggregationCompressedBytes'] as b "
                f"from system.query_log where type = 'QueryFinish' and event_time > now() - interval {int(hours)} hour and b > 0 order by b desc limit 50")
        except Exception:
            return []
        return [{"source": "clickhouse", "scope": qid, "spilled_bytes": int(b), "detail": txt} for qid, txt, b in r]

    def memory_failures(self, hours=24):
        try:                                      # 241 = MEMORY_LIMIT_EXCEEDED
            r = self.rows("select query_id, substring(query, 1, 200) from system.query_log "
                          f"where type = 'ExceptionWhileProcessing' and exception_code = 241 and event_time > now() - interval {int(hours)} hour limit 50")
        except Exception:
            return []
        return [{"source": "clickhouse", "scope": qid, "detail": f"MEMORY_LIMIT_EXCEEDED: {txt}"} for qid, txt in r]


class MySQLWarehouse(Warehouse):
    """MySQL/MariaDB (read-only; `READ ONLY` session). Env: MYSQL_HOST, [MYSQL_PORT=3306, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE]."""
    dialect, name = "mysql", "mysql"
    str_type = "char"

    def _bucket(self, text):
        return f"(crc32({text}) % 10000)"


    def __init__(self, connection=None):
        if connection is None:
            import pymysql  # type: ignore
            host = os.environ.get("MYSQL_HOST")
            if not host:
                raise RuntimeError(tr("variable {name} is not set", name="MYSQL_HOST"))
            connection = pymysql.connect(host=host, port=int(os.environ.get("MYSQL_PORT", "3306")), user=os.environ.get("MYSQL_USER", "root"),
                                         password=os.environ.get("MYSQL_PASSWORD", ""), database=os.environ.get("MYSQL_DATABASE"), autocommit=True,
                                         connect_timeout=10, read_timeout=300)
            with connection.cursor() as c:
                c.execute("SET SESSION TRANSACTION READ ONLY")
        self.con = connection

    def quote(self, ident):
        return "`" + ident.replace("`", "") + "`"

    def differs(self, a, b):                      # MySQL has no IS DISTINCT FROM; `<=>` is the NULL-safe equality
        return f"not ({a} <=> {b})"

    def rows(self, sql):
        with self.con.cursor() as c:
            c.execute(sql)
            return [tuple(r) for r in c.fetchall()] if c.description else []

    def describe(self, rel):
        return [(r[0], str(r[1])) for r in self.rows(f"describe {rel}")]

    def plan(self, sql):
        raw = self.rows("EXPLAIN FORMAT=JSON " + sql)[0][0]
        doc = json.loads(raw) if isinstance(raw, str) else raw
        ops, rows, cost = [], [], []

        def walk(n):
            if isinstance(n, dict):
                if "access_type" in n:
                    ops.append(f"{n.get('access_type')}:{n.get('table_name', '')}")
                    for k in ("rows_examined_per_scan", "rows_produced_per_join"):
                        if n.get(k) is not None:
                            rows.append(int(n[k]))
                if "query_cost" in (n.get("cost_info") or {}):
                    cost.append(float(n["cost_info"]["query_cost"]))
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(doc)
        return {"operators": ops, "max_rows": max(rows) if rows else None, "cost": max(cost) if cost else None}


class SQLServerWarehouse(Warehouse):
    """SQL Server / Azure SQL through `pymssql` (read-only). Env: MSSQL_HOST, [MSSQL_PORT=1433, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE].
    Verification: fake connection only (no SQL Server container verified here)."""
    dialect, name = "tsql", "sqlserver"
    def _bucket(self, text):
        return f"abs(checksum({text}) % 10000)"


    def __init__(self, connection=None):
        if connection is None:
            import pymssql  # type: ignore
            host = os.environ.get("MSSQL_HOST")
            if not host:
                raise RuntimeError(tr("variable {name} is not set", name="MSSQL_HOST"))
            connection = pymssql.connect(server=host, port=int(os.environ.get("MSSQL_PORT", "1433")), user=os.environ.get("MSSQL_USER", ""),
                                         password=os.environ.get("MSSQL_PASSWORD", ""), database=os.environ.get("MSSQL_DATABASE", ""), autocommit=True,
                                         login_timeout=10, read_only=True)
        self.con = connection

    def quote(self, ident):
        return "[" + ident.replace("]", "]]") + "]"

    def differs(self, a, b):                      # works on any version (IS DISTINCT FROM only in 2022+)
        return f"not exists (select {a} intersect select {b})"

    def rows(self, sql):
        cur = self.con.cursor()
        try:
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()] if cur.description else []
        finally:
            cur.close()

    def describe(self, rel):
        parts = [p.strip("[]\"") for p in rel.split(".")]
        tbl, sch = parts[-1], (parts[-2] if len(parts) > 1 else "dbo")
        return [(r[0], str(r[1])) for r in self.rows(
            "select column_name, data_type from information_schema.columns "
            f"where table_name = '{tbl.replace(chr(39), '')}' and table_schema = '{sch.replace(chr(39), '')}' order by ordinal_position")]


class AthenaWarehouse(TrinoWarehouse):
    """Amazon Athena (motor Trino): consultas via API (`StartQueryExecution`), esquema pelo Glue Data Catalog.
    Env: `ATHENA_OUTPUT_LOCATION` (s3://bucket/prefixo/), opcionais `ATHENA_WORKGROUP`, `ATHENA_DATABASE`, `AWS_REGION`."""
    dialect, name = "athena", "athena"

    def __init__(self, client=None, workgroup: str | None = None, database: str | None = None, output: str | None = None, poll=0.5, timeout=300):
        if client is None:
            from .. import orchestrators  # noqa: F401
            from ..orchestrators.aws import client as _client
            client = _client("athena")
        self.c, self.poll, self.timeout = client, poll, timeout
        self.workgroup = workgroup or os.environ.get("ATHENA_WORKGROUP", "primary")
        self.database = database or os.environ.get("ATHENA_DATABASE")
        self.output = output or os.environ.get("ATHENA_OUTPUT_LOCATION")

    def rows(self, sql):
        import time
        kw = {"QueryString": sql, "WorkGroup": self.workgroup}
        if self.database:
            kw["QueryExecutionContext"] = {"Database": self.database}
        if self.output:
            kw["ResultConfiguration"] = {"OutputLocation": self.output}
        qid = self.c.start_query_execution(**kw)["QueryExecutionId"]
        t0 = time.time()
        while True:
            st = self.c.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
            if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.time() - t0 > self.timeout:
                raise RuntimeError(tr("Athena: query {id} exceeded {s}s", id=qid, s=self.timeout))
            time.sleep(self.poll)
        if st["State"] != "SUCCEEDED":
            raise RuntimeError(f"Athena: {st['State']}: {st.get('StateChangeReason', '')[:200]}")
        out, first, token = [], True, None
        while True:
            r = self.c.get_query_results(QueryExecutionId=qid, **({"NextToken": token} if token else {}))
            for row in r["ResultSet"]["Rows"]:
                vals = tuple(d.get("VarCharValue") for d in row["Data"])
                if first:                  # the first row of the first page is the header (data queries)
                    first = False
                    if not sql.lstrip().lower().startswith(("explain", "show", "describe")):
                        continue
                out.append(vals)
            token = r.get("NextToken")
            if not token:
                return out

    def describe(self, rel):
        parts = [p.strip('`"') for p in rel.split(".")]
        db, tbl = (parts[-2], parts[-1]) if len(parts) > 1 else (self.database, parts[-1])
        md = self.c.get_table_metadata(CatalogName="AwsDataCatalog", DatabaseName=db, TableName=tbl)["TableMetadata"]
        return [(c["Name"], c.get("Type", "")) for c in md.get("Columns", [])]

    def memory_failures(self, hours=24):
        return []

    def spill_report(self, hours=24):
        return []
