"""Read access to warehouses: probe queries, EXPLAIN and spill reports.

Local: DuckDB, PostgreSQL. Cloud: Snowflake, BigQuery, Databricks (cloud.py; credentials only through
environment variables, read-only, lazy driver import).
"""
from __future__ import annotations

from ..i18n import tr

import os


class Warehouse:
    dialect = "duckdb"
    name = "warehouse"

    def rows(self, sql: str) -> list[tuple]:
        raise NotImplementedError

    def close(self) -> None:
        """Close the connection, if any (idempotent; driver errors are ignored)."""
        for attr in ("con", "client"):
            c = getattr(self, attr, None)
            if c is not None and hasattr(c, "close"):
                try:
                    c.close()
                except Exception:
                    pass

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def differs(self, a: str, b: str) -> str:
        """Inequality comparison that treats NULL as a value."""
        return f"{a} is distinct from {b}"

    # deterministic key hash per engine: base and new always fall in the SAME bucket, so the sample is consistent between them
    str_type = "varchar"

    def _bucket(self, text: str) -> str:
        """Integer expression in [0, 10000) derived from `text` (already a string)."""
        return f"abs(mod(hash({text}), 10000))"

    def sample_pred(self, exprs: list[str], pct: float) -> str:
        """Predicate true for ~pct % of the keys (key hash, not random rows)."""
        text = f"cast({exprs[0]} as {self.str_type})"
        for e in exprs[1:]:
            text = f"concat(concat({text}, '|'), cast({e} as {self.str_type}))"
        return f"{self._bucket(text)} < {max(1, round(pct * 100))}"

    def scalar(self, sql: str):
        r = self.rows(sql)
        return r[0][0] if r else None

    def explain(self, sql: str) -> str:
        return "\n".join(str(r[-1]) for r in self.rows("EXPLAIN " + sql))

    def spill_report(self, hours: int = 24) -> list[dict]:
        """Queries that spilled memory to disk (a sign of memory pressure)."""
        return []

    def memory_failures(self, hours: int = 24) -> list[dict]:
        """Queries killed for lack of memory: [{source, scope, detail}] (empty if the engine does not expose them)."""
        return []

    def describe(self, rel: str) -> list[tuple[str, str]]:
        """(column, type) of a relation."""
        raise NotImplementedError

    def plan(self, sql: str) -> dict:
        """Plano estimado, sem executar: {operators: [...], max_rows: int|None, cost: float|None}."""
        return {"operators": [], "max_rows": None, "cost": None}


class DuckDBWarehouse(Warehouse):
    dialect, name = "duckdb", "duckdb"

    def __init__(self, path: str, read_only: bool = True):
        import duckdb
        self.con = duckdb.connect(path, read_only=read_only)

    def rows(self, sql):
        return self.con.execute(sql).fetchall()

    def describe(self, rel):
        return [(r[0], str(r[1])) for r in self.rows(f"describe select * from {rel}")]

    def plan(self, sql):
        import json
        raw = self.rows("EXPLAIN (FORMAT json) " + sql)[0][1]
        ops, rows = [], []

        def walk(n):
            ops.append(n.get("name", ""))
            ec = (n.get("extra_info") or {}).get("Estimated Cardinality")
            if ec is not None:
                try:
                    rows.append(int(str(ec).lstrip("~")))
                except ValueError:
                    pass
            for c in n.get("children", []):
                walk(c)
        for n in json.loads(raw):
            walk(n)
        return {"operators": ops, "max_rows": max(rows) if rows else None, "cost": None}

    def memory(self) -> list[dict]:
        return [dict(zip(("tag", "memory_bytes", "temp_bytes"), r[:3])) for r in self.rows(
            "select tag, memory_usage_bytes, temporary_storage_bytes from duckdb_memory()")]


class PostgresWarehouse(Warehouse):
    dialect, name = "postgres", "postgres"
    def _bucket(self, text):
        return f"abs(mod(hashtext({text}), 10000))"


    def __init__(self, url_env: str = "INTENTFLOW_PG_URL"):
        import psycopg2
        url = os.environ.get(url_env)
        if not url:
            raise RuntimeError(tr("variable {name} is not set", name=url_env))
        self.con = psycopg2.connect(url)
        self.con.set_session(readonly=True, autocommit=True)

    def rows(self, sql):
        with self.con.cursor() as c:
            c.execute(sql)
            return c.fetchall() if c.description else []

    def describe(self, rel):
        return [(r[0], r[1]) for r in self.rows(f"select * from (select * from {rel} limit 0) q") ] if False else self._describe(rel)

    def _describe(self, rel):
        with self.con.cursor() as c:
            c.execute(f"select * from {rel} limit 0")
            import psycopg2.extensions as ext  # tipos pelo OID
            return [(d.name, str(d.type_code)) for d in c.description]

    def plan(self, sql):
        import json
        raw = self.rows("EXPLAIN (FORMAT JSON) " + sql)[0][0]
        raw = raw if isinstance(raw, list) else json.loads(raw)
        ops, rows, cost = [], [], []

        def walk(n):
            ops.append(n.get("Node Type", ""))
            rows.append(int(n.get("Plan Rows", 0)))
            cost.append(float(n.get("Total Cost", 0)))
            for c in n.get("Plans", []):
                walk(c)
        walk(raw[0]["Plan"])
        return {"operators": ops, "max_rows": max(rows) if rows else None, "cost": max(cost) if cost else None}

    def spill_report(self, hours=24):
        # pg_stat_database.temp_bytes: bytes written to temporary files (insufficient work_mem)
        try:
            r = self.rows("select datname, temp_files, temp_bytes from pg_stat_database where temp_bytes > 0 order by temp_bytes desc limit 20")
        except Exception:
            return []
        return [{"source": "postgres", "scope": d, "spilled_bytes": int(b), "detail": tr("{n} temporary files", n=f)} for d, f, b in r]


TYPES = ("duckdb", "postgres", "redshift", "trino", "clickhouse", "mysql", "sqlserver", "athena", "snowflake", "bigquery", "databricks")


def connect(get) -> Warehouse | None:
    """The configured warehouse. `get` reads settings (e.g. `store.get_setting`).
    `warehouse.type` picks the engine; without it the old behavior applies (`warehouse.duckdb` or `warehouse.postgres_env`)."""
    t = (get("warehouse.type") or "").lower()
    duck, pg = get("warehouse.duckdb"), get("warehouse.postgres_env")
    if not t:
        t = "duckdb" if duck else "postgres" if pg else ""
    if not t:
        return None
    if t == "duckdb":
        if not duck:
            raise RuntimeError(tr("warehouse.duckdb is not configured"))
        return DuckDBWarehouse(duck)
    if t == "postgres":
        return PostgresWarehouse(pg or "INTENTFLOW_PG_URL")
    if t in ("redshift", "trino", "clickhouse", "mysql", "sqlserver", "athena"):
        from . import more
        return {"redshift": lambda: more.RedshiftWarehouse(pg or "REDSHIFT_URL"), "trino": more.TrinoWarehouse,
                "clickhouse": more.ClickHouseWarehouse, "mysql": more.MySQLWarehouse, "sqlserver": more.SQLServerWarehouse, "athena": more.AthenaWarehouse}[t]()
    if t in ("snowflake", "bigquery", "databricks"):
        from . import cloud
        return {"snowflake": cloud.SnowflakeWarehouse, "bigquery": cloud.BigQueryWarehouse, "databricks": cloud.DatabricksWarehouse}[t]()
    raise RuntimeError(tr("unknown warehouse.type: {type} (use one of {types})", type=t, types=", ".join(TYPES)))
