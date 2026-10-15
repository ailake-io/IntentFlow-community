"""Local review microbenchmarks; synthetic data and in-memory SQLite only.

Run: .venv/bin/python scripts/benchmark_review.py
Not representative of production load; compare on the same environment and versions.
"""
import json
import platform
import statistics
import time
from importlib.metadata import version
from unittest.mock import patch

from intentflow.checks import Context, run_checks, sql_quality
from intentflow.dbt_loader import DbtNode, Project
from intentflow.model import Finding
from intentflow.store import Store


def findings_benchmark():
    with Store() as store:
        store.sync_findings([Finding("BENCH", "low", str(i), "d", nodes=[str(i)]) for i in range(2000)], {"BENCH"})
        queries = []
        store.con.set_trace_callback(queries.append)
        started = time.perf_counter()
        rows = store.findings()
        elapsed = time.perf_counter() - started
    return {"rows": len(rows), "sql_statements": len(queries), "seconds": elapsed}


def sql_benchmark():
    project = Project({str(i): DbtNode(str(i), str(i), "model", compiled_code=
                       "select a.id, a.amount from a join b on a.id = b.id where a.amount > 0") for i in range(300)})
    context = Context(project=project)
    durations, counts = [], []
    for _ in range(3):
        with patch.object(sql_quality, "_parse", wraps=sql_quality._parse) as parse:
            started = time.perf_counter()
            run_checks(context, only=["SQL%03d" % i for i in range(1, 12)])
            durations.append(time.perf_counter() - started)
            counts.append(parse.call_count)
    return {"models": 300, "checks": 11, "parse_calls": counts,
            "median_seconds": statistics.median(durations), "samples": durations}


if __name__ == "__main__":
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                      "sqlglot": version("sqlglot"), "findings": findings_benchmark(),
                      "sql_checks": sql_benchmark()}, indent=2))
