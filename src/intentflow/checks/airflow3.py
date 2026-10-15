"""Airflow 2 → 3 migration: finds in DAG files what breaks when moving to Airflow 3 and rewrites what is mechanical.

The rules (AF3xx) come from the documented Airflow 3 breaking changes and were checked against a real Airflow 3.1
(operator imports moved to the `standard` provider, `airflow.sdk`, removal of `execution_date`, `Table` → `Asset`...).
Only exact module swaps are rewritten; everything else becomes a finding with the recommendation.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from ..i18n import N_, cat, tr
from ..model import Finding
from . import Context, check

# old module → new module (exact swap, imported names do not change)
MODULE_MOVES = {
    "airflow.operators.bash": "airflow.providers.standard.operators.bash",
    "airflow.operators.bash_operator": "airflow.providers.standard.operators.bash",
    "airflow.operators.python": "airflow.providers.standard.operators.python",
    "airflow.operators.python_operator": "airflow.providers.standard.operators.python",
    "airflow.operators.empty": "airflow.providers.standard.operators.empty",
    "airflow.operators.trigger_dagrun": "airflow.providers.standard.operators.trigger_dagrun",
    "airflow.operators.latest_only": "airflow.providers.standard.operators.latest_only",
    "airflow.operators.datetime": "airflow.providers.standard.operators.datetime",
    "airflow.sensors.filesystem": "airflow.providers.standard.sensors.filesystem",
    "airflow.sensors.time_sensor": "airflow.providers.standard.sensors.time",
    "airflow.sensors.date_time": "airflow.providers.standard.sensors.date_time",
    "airflow.sensors.external_task": "airflow.providers.standard.sensors.external_task",
}
# `from <mod> import names` → `from airflow.sdk import names` when ALL names exist in the SDK
SDK_MODULES = {"airflow": {"DAG"}, "airflow.models": {"DAG", "Variable", "Connection", "BaseOperator", "XComArg", "Param"},
               "airflow.decorators": {"dag", "task", "task_group", "setup", "teardown"},
               "airflow.models.baseoperator": {"BaseOperator", "chain", "cross_downstream"},
               "airflow.models.dag": {"DAG", "dag"}}
RENAMED = {"DummyOperator": "EmptyOperator", "Dataset": "Asset"}
REMOVED_IMPORTS = {"days_ago": N_("use pendulum.today('UTC').add(days=-N) or datetime(...)"),
                   "SubDagOperator": N_("replace it with a TaskGroup"),
                   "provide_session": N_("workers do not access the metadata database; use the API/Task SDK"),
                   "create_session": N_("workers do not access the metadata database; use the API/Task SDK")}
REMOVED_ARGS = {"provide_context": ("AF304", N_("removed: the context is always passed; declare **context or the kwargs you use")),
                "schedule_interval": ("AF302", N_("removed in Airflow 3: use `schedule`")),
                "timetable": ("AF302", N_("removed as a DAG argument: use `schedule=`"))}
TEMPLATE_VARS = {"execution_date": "logical_date", "next_execution_date": "data_interval_end", "prev_execution_date": N_("(no direct equivalent; use data_interval_start)"),
                 "tomorrow_ds": "macros/pendulum", "yesterday_ds": "macros/pendulum", "next_ds": "data_interval_end | ds",
                 "prev_ds": "data_interval_start | ds", "conf": "params / dag_run.conf"}


def scan_source(src: str) -> list[dict]:
    """Return problems: {rule, severity, line, message, recommendation, fixable}."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[dict] = []

    def add(rule, sev, line, msg, rec, fixable=False):
        out.append({"rule": rule, "severity": sev, "line": line, "message": msg, "recommendation": rec, "fixable": fixable})

    dag_calls = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            names = {a.name for a in n.names}
            if n.module in MODULE_MOVES:
                add("AF301", "high", n.lineno, tr("`{module}` left the core in Airflow 3", module=n.module),
                    tr("`from {module} import …` (package apache-airflow-providers-standard)", module=MODULE_MOVES[n.module]), True)
            elif n.module in SDK_MODULES and names <= SDK_MODULES[n.module]:
                add("AF301", "high", n.lineno, tr("`from {module} import {names}` moved to the SDK", module=n.module, names=", ".join(sorted(names))),
                    "`from airflow.sdk import …`", True)
            if n.module in ("airflow.datasets", "airflow.dataset") or "Dataset" in names:
                add("AF310", "medium", n.lineno, tr("`Dataset` became `Asset` (and `airflow.datasets` was removed)"),
                    tr("`from airflow.sdk import Asset`; `inlets`/`outlets`/`schedule` use Asset"))
            if n.module == "airflow.lineage.entities":
                add("AF310", "medium", n.lineno, tr("`airflow.lineage.entities.Table/File` was removed (checked on 3.1)"),
                    tr("use `Asset(uri)` (e.g. `Asset('postgres://host:5432/db/schema/table')`)"))
            if n.module == "airflow.settings" and "Session" in names:
                add("AF308", "high", n.lineno, tr("direct access to the metadata database does not work in tasks in Airflow 3"), tr(REMOVED_IMPORTS["provide_session"]))
            for nm in names & set(REMOVED_IMPORTS):
                add("AF306" if nm == "days_ago" else ("AF305" if nm == "SubDagOperator" else "AF308"), "high", n.lineno,
                    tr("`{name}` was removed in Airflow 3", name=nm), tr(REMOVED_IMPORTS[nm]))
            for nm in names & set(RENAMED):
                if nm == "DummyOperator":
                    add("AF301", "medium", n.lineno, tr("`DummyOperator` was removed"), tr("use `EmptyOperator` (airflow.providers.standard.operators.empty)"))
        if isinstance(n, ast.Call):
            fn = getattr(n.func, "id", getattr(n.func, "attr", ""))
            if fn in ("DAG", "dag"):
                dag_calls.append(n)
            for k in n.keywords:
                if k.arg in REMOVED_ARGS and (fn in ("DAG", "dag") or k.arg == "provide_context"):
                    rule, msg = REMOVED_ARGS[k.arg]
                    add(rule, "high", n.lineno, cat(f"`{k.arg}`: ", tr(msg)), tr(msg))
            if fn == "SubDagOperator":
                add("AF305", "high", n.lineno, tr("`SubDagOperator` was removed"), tr(REMOVED_IMPORTS["SubDagOperator"]))
        if isinstance(n, ast.Attribute) and n.attr == "NONE_FAILED_OR_SKIPPED":
            add("AF309", "medium", n.lineno, tr("`TriggerRule.NONE_FAILED_OR_SKIPPED` was removed"), "`TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS`")
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value in ("execution_date", "next_execution_date", "prev_execution_date"):
            add("AF303", "high", n.lineno, tr("context[`{key}`] was removed", key=n.slice.value),
                tr("use `logical_date`/`data_interval_*` ({hint})", hint=tr(TEMPLATE_VARS[n.slice.value])))
    for c in dag_calls:
        kw = {k.arg for k in c.keywords}
        has_sched = ("schedule" in kw or "schedule_interval" in kw) and not any(
            k.arg in ("schedule", "schedule_interval") and isinstance(k.value, ast.Constant) and k.value.value is None for k in c.keywords)
        if has_sched and "catchup" not in kw:
            add("AF307", "medium", c.lineno, tr("`catchup` not declared: the default changed from True (2.x) to False (3.x)"),
                tr("declare `catchup=` explicitly so the behavior does not change silently"))
    for i, line in enumerate(src.splitlines(), 1):
        if "{{" in line:
            for m in re.finditer(r"\{\{[^}]*\b(execution_date|next_execution_date|prev_execution_date|tomorrow_ds|yesterday_ds|next_ds|prev_ds)\b", line):
                v = m.group(1)
                add("AF303", "high", i, tr("template variable `{name}` was removed", name=v),
                    tr("use `{replacement}` (e.g. `{example}`)", replacement=tr(TEMPLATE_VARS[v]), example="{{ logical_date }}"))
        if re.search(r"/api/v1/", line) and "airflow" in src.lower():
            add("AF311", "medium", i, tr("call to the REST `/api/v1`, removed in Airflow 3"), tr("use `/api/v2` with a JWT token (`POST /auth/token`)"))
    seen, uniq = set(), []
    for p in out:
        k = (p["rule"], p["line"], p["message"])
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return sorted(uniq, key=lambda p: (p["line"], p["rule"]))


def rewrite_source(src: str) -> tuple[str, int]:
    """Apply only exact import swaps. Return (new_text, count)."""
    n = 0

    def sub(m):
        nonlocal n
        indent, mod, rest = m.group(1), m.group(2), m.group(3)
        names = {x.strip().split(" as ")[0].strip("() ") for x in rest.split(",") if x.strip().strip("()")}
        if mod in MODULE_MOVES:
            n += 1
            new = MODULE_MOVES[mod]
        elif mod in SDK_MODULES and names and names <= SDK_MODULES[mod]:
            n += 1
            new = "airflow.sdk"
        else:
            return m.group(0)
        return f"{indent}from {new} import {rest}"
    return re.sub(r"^([ \t]*)from\s+([\w.]+)\s+import\s+(.+)$", sub, src, flags=re.M), n


def migrate_dir(path: str | Path, write: bool = False) -> list[dict]:
    """Scan a DAG folder. With write=True rewrite the mechanical imports (keeps a .af2.bak next to each file)."""
    res = []
    for p in sorted(Path(path).rglob("*.py")):
        src = p.read_text(errors="replace")
        if "airflow" not in src:
            continue
        issues = scan_source(src)
        new, n = rewrite_source(src)
        if write and n and new != src:
            p.with_suffix(p.suffix + ".af2.bak").write_text(src)
            p.write_text(new)
        if issues or n:
            remaining = scan_source(new) if write and n else issues
            res.append({"file": str(p), "issues": issues, "rewritable": n, "remaining": remaining if write else None, "written": bool(write and n)})
    return res


@check("AF3", "Airflow 3 compatibility (2 → 3 migration)", stage="pre-merge")
def airflow3_compat(ctx: Context):
    # opt-in: teams still on Airflow 2 must not see hundreds of "high" findings. `intentflow policy airflow-target 3` turns it on.
    if not ctx.airflow_dags_dir or int(ctx.policies.get("airflow_target_major", 0) or 0) < 3:
        return []
    out = []
    for r in migrate_dir(ctx.airflow_dags_dir):
        name = Path(r["file"]).name
        for p in r["issues"]:
            out.append(Finding(p["rule"], p["severity"], cat(f"{name}:{p['line']} ", p["message"]),
                               tr("Breaks when migrating to Airflow 3 (checked against Airflow 3.1)."), [r["file"]], "pre-merge",
                               {"line": p["line"], "recommendation": p["recommendation"], "auto_fixable": p["fixable"]}))
    return out
