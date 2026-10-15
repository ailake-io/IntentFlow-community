"""Cloud adapters (read-only). NOT exercised against real accounts in this repository:
the tests use fake connections. Credentials: environment variables; nothing is written to disk.

  Snowflake : SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, [SNOWFLAKE_WAREHOUSE, SNOWFLAKE_ROLE]
  BigQuery  : Google default credentials (GOOGLE_APPLICATION_CREDENTIALS), BIGQUERY_PROJECT
  Databricks: DATABRICKS_HOST, DATABRICKS_HTTP_PATH, DATABRICKS_TOKEN
"""
from __future__ import annotations

from ..i18n import tr

import json
import os
import re

from . import Warehouse


def _env(*names):
    miss = [n for n in names if not os.environ.get(n)]
    if miss:
        raise RuntimeError(tr("missing environment variables: {names}", names=", ".join(miss)))
    return [os.environ[n] for n in names]


class SnowflakeWarehouse(Warehouse):
    dialect, name = "snowflake", "snowflake"
    SPILL_SQL = (
        "select query_id, left(query_text, 200), bytes_spilled_to_local_storage, bytes_spilled_to_remote_storage "
        "from snowflake.account_usage.query_history "
        "where start_time > dateadd(hour, -{hours}, current_timestamp()) "
        "and (bytes_spilled_to_local_storage > 0 or bytes_spilled_to_remote_storage > 0) "
        "order by bytes_spilled_to_remote_storage desc, bytes_spilled_to_local_storage desc limit 50"
    )

    def __init__(self, connection=None):
        if connection is None:
            import snowflake.connector  # type: ignore
            acc, user, pw = _env("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")
            connection = snowflake.connector.connect(account=acc, user=user, password=pw,
                                                     warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"), role=os.environ.get("SNOWFLAKE_ROLE"))
        self.con = connection

    def rows(self, sql):
        cur = self.con.cursor()
        try:
            cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()

    def describe(self, rel):
        return [(r[0], str(r[1])) for r in self.rows(f"describe table {rel}")]

    def plan(self, sql):
        """`EXPLAIN USING JSON`: operations and estimated bytes (Snowflake does not estimate rows in the plan)."""
        raw = self.rows("EXPLAIN USING JSON " + sql)[0][0]
        doc = json.loads(raw) if isinstance(raw, str) else raw
        ops = [o.get("operation", "") for o in doc.get("Operations", [[]])[0] if isinstance(o, dict)]
        g = doc.get("GlobalStats") or {}
        return {"operators": ops, "max_rows": None, "cost": float(g["bytesAssigned"]) if g.get("bytesAssigned") is not None else None}

    def spill_report(self, hours=24):
        return [{"source": "snowflake", "scope": qid, "spilled_bytes": int(loc or 0) + int(rem or 0),
                 "detail": f"local={loc} remoto={rem}: {txt}"} for qid, txt, loc, rem in self.rows(self.SPILL_SQL.format(hours=int(hours)))]


class BigQueryWarehouse(Warehouse):
    dialect, name = "bigquery", "bigquery"
    str_type = "string"

    def _bucket(self, text):
        return f"abs(mod(farm_fingerprint({text}), 10000))"


    def quote(self, ident):
        return "`" + ident.replace("`", "") + "`"
    # BigQuery does not expose spill; the equivalent signal is resources exceeded
    SPILL_SQL = (
        "select job_id, left(query, 200), total_bytes_processed "
        "from `region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
        "where creation_time > timestamp_sub(current_timestamp(), interval {hours} hour) "
        "and error_result.reason = 'resourcesExceeded' limit 50"
    )

    def __init__(self, client=None):
        if client is None:
            from google.cloud import bigquery  # type: ignore
            client = bigquery.Client(project=os.environ.get("BIGQUERY_PROJECT"))
        self.client = client

    def rows(self, sql):
        return [tuple(r.values()) for r in self.client.query(sql).result()]

    def describe(self, rel):
        t = self.client.get_table(rel.replace("`", ""))
        return [(f.name, f.field_type) for f in t.schema]

    def plan(self, sql):
        """Dry run: BigQuery reports the bytes that would be read (the basis of cost); there are no operators or estimated rows."""
        from google.cloud import bigquery  # type: ignore
        job = self.client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        return {"operators": [], "max_rows": None, "cost": float(job.total_bytes_processed or 0)}

    def spill_report(self, hours=24):
        region = os.environ.get("BIGQUERY_REGION", "us").lower()      # `us`, `eu`, `southamerica-east1`, ...
        return [{"source": "bigquery", "scope": jid, "spilled_bytes": int(b or 0), "detail": f"resourcesExceeded: {q}"}
                for jid, q, b in self.rows(self.SPILL_SQL.format(hours=int(hours), region=region))]


class DatabricksWarehouse(Warehouse):
    dialect, name = "databricks", "databricks"
    str_type = "string"


    def quote(self, ident):
        return "`" + ident.replace("`", "") + "`"
    SPILL_SQL = (
        "select statement_id, left(statement_text, 200), spilled_local_bytes "
        "from system.query.history "
        "where start_time > current_timestamp() - interval {hours} hours and spilled_local_bytes > 0 "
        "order by spilled_local_bytes desc limit 50"
    )

    def __init__(self, connection=None):
        if connection is None:
            from databricks import sql  # type: ignore
            host, path, tok = _env("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN")
            connection = sql.connect(server_hostname=host, http_path=path, access_token=tok)
        self.con = connection

    def rows(self, sql):
        with self.con.cursor() as c:
            c.execute(sql)
            return [tuple(r) for r in c.fetchall()]

    def describe(self, rel):
        out = []
        for r in self.rows(f"describe table {rel}"):
            if not r[0] or str(r[0]).startswith("#"):       # end of the columns (partition/metadata block)
                break
            out.append((str(r[0]), str(r[1])))
        return out

    def plan(self, sql):
        """`EXPLAIN COST`: physical plan operators and `rowCount`/`sizeInBytes` from the statistics."""
        txt = "\n".join(str(r[0]) for r in self.rows("EXPLAIN COST " + sql))
        ops = re.findall(r"^[\s:+\-|]*(?:\+- |:- )?([A-Z][A-Za-z]+(?:Exec)?)\b", txt, re.M)
        rows = [int(float(m)) for m in re.findall(r"rowCount=(\d[\d.]*(?:E[+-]?\d+)?)", txt)]
        size = [self._bytes(m) for m in re.findall(r"sizeInBytes=([\d.]+ ?[KMGT]?i?B)", txt)]
        return {"operators": ops, "max_rows": max(rows) if rows else None, "cost": max(size) if size else None}

    @staticmethod
    def _bytes(s: str) -> float:
        m = re.match(r"([\d.]+)\s*([KMGT]?)i?B", s)
        return float(m.group(1)) * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2)] if m else 0.0

    def spill_report(self, hours=24):
        return [{"source": "databricks", "scope": sid, "spilled_bytes": int(b or 0), "detail": txt}
                for sid, txt, b in self.rows(self.SPILL_SQL.format(hours=int(hours)))]
