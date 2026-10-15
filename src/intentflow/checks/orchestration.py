"""Static lint of Airflow definitions (AST of DAG files) and Kestra definitions (flow YAML)."""
from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path

import yaml

from ..i18n import tr
from ..model import Finding
from . import Context, check


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _lit(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _date_of(node):
    if isinstance(node, ast.Call) and node.args and all(isinstance(a, ast.Constant) and isinstance(a.value, int) for a in node.args[:3]):
        try:
            return dt.date(*[a.value for a in node.args[:3]])
        except Exception:
            return None
    return None


def _airflow_file(path: Path):
    src = path.read_text(errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [Finding("AF000", "high", tr("{file}: syntax error", file=path.name), str(e), [str(path)], "pre-merge")]
    out = []
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    kws = {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords}
    dict_keys = {k.value for n in ast.walk(tree) if isinstance(n, ast.Dict) for k in n.keys if isinstance(k, ast.Constant)}
    has = lambda k: k in kws or k in dict_keys   # noqa: E731
    dag_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and (getattr(n.func, "id", "") in ("DAG", "dag") or getattr(n.func, "attr", "") in ("DAG", "dag"))]
    if not dag_calls and "DAG" not in names and "dag" not in names:
        return []
    ref = [str(path)]
    where = path.name
    if not has("retries"):
        out.append(Finding("AF001", "medium", tr("{file}: no retries", file=where), tr("A transient failure (network, throttling) brings the whole run down."), ref, "pre-merge",
                           {"recommendation": "default_args={'retries': 2, 'retry_delay': timedelta(minutes=5)}"}))
    if not (has("execution_timeout") or has("dagrun_timeout")):
        out.append(Finding("AF002", "medium", tr("{file}: no timeout", file=where),
                           tr("A stuck task holds a slot/worker forever and can exhaust memory and the pool."), ref, "pre-merge",
                           {"recommendation": tr("set execution_timeout on the tasks and dagrun_timeout on the DAG")}))
    for c in dag_calls:
        catchup = _kw(c, "catchup")
        start = _date_of(_kw(c, "start_date"))
        sched = _kw(c, "schedule") or _kw(c, "schedule_interval")
        explicit_false = isinstance(catchup, ast.Constant) and catchup.value is False
        if start and not explicit_false and sched is not None and (dt.date.today() - start).days > 30:
            out.append(Finding("AF003", "high", tr("{file}: catchup on with start_date {date}", file=where, date=start),
                               tr("When unpaused, Airflow creates one run for each interval since start_date (backfill storm; memory and cost peak)."),
                               ref, "pre-merge", {"start_date": str(start), "recommendation": tr("catchup=False or a recent start_date + max_active_runs")}))
            if _kw(c, "max_active_runs") is None:
                out.append(Finding("AF004", "medium", tr("{file}: catchup without max_active_runs", file=where),
                                   tr("Without a limit, the backfill runs execute in parallel."), ref, "pre-merge", {"recommendation": "max_active_runs=1..3"}))
        if _kw(c, "schedule_interval") is not None:
            out.append(Finding("AF006", "low", tr("{file}: `schedule_interval` is deprecated", file=where), tr("Removed in Airflow 3; use `schedule`."), ref, "pre-merge"))
    if not (has("on_failure_callback") or has("email_on_failure") or has("email")):
        out.append(Finding("AF005", "low", tr("{file}: no failure notification", file=where), tr("Failures are only seen when someone opens the UI."), ref, "continuous"))
    for imp in ast.walk(tree):
        mod = getattr(imp, "module", "") if isinstance(imp, ast.ImportFrom) else ""
        if mod in ("airflow.operators.bash_operator", "airflow.operators.python_operator", "airflow.operators.dummy_operator") \
                or (isinstance(imp, ast.ImportFrom) and any(a.name == "days_ago" for a in imp.names)):
            out.append(Finding("AF006", "low", tr("{file}: deprecated import ({module})", file=where, module=mod or "days_ago"),
                               tr("It will be removed in newer Airflow versions."), ref, "pre-merge"))
            break
    for stmt in tree.body:   # module-level code runs on every scheduler parse
        for n in ast.walk(stmt) if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else []:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
                    and getattr(n.func.value, "id", "") == "Variable":
                out.append(Finding("AF007", "medium", tr("{file}: Variable.get at module level", file=where),
                                   tr("Runs on every DAG parse (every few seconds), querying the metadata database and slowing the scheduler down."),
                                   ref, "pre-merge", {"recommendation": tr("use the Jinja template `{{ var.value.x }}` inside the tasks")}))
                return out
    return out


SECRET = re.compile(r"(?i)[\w-]*(password|passwd|secret|token|api[_-]?key|access[_-]?key)\w*['\"]?\s*[:=]\s*['\"]?(?!\{\{)([^\s'\"{}]{6,})")


def _kestra_file(path: Path):
    try:
        doc = yaml.safe_load(path.read_text())
    except Exception as e:
        return [Finding("KS000", "high", tr("{file}: invalid YAML", file=path.name), str(e)[:200], [str(path)], "pre-merge")]
    if not isinstance(doc, dict) or "tasks" not in doc:
        return []
    fid = f"{doc.get('namespace', '?')}.{doc.get('id', path.stem)}"
    ref = [fid]
    out = []
    tasks = doc.get("tasks") or []
    if not doc.get("errors") and not doc.get("afterExecution"):
        out.append(Finding("KS001", "medium", tr("{flow}: no error handling", flow=fid), tr("Without an `errors` block, failures raise no alert and do no cleanup."), ref, "pre-merge",
                           {"recommendation": tr("add `errors:` with a notification (Slack/e-mail)")}))
    risky = [t for t in tasks if any(k in str(t.get("type", "")).lower() for k in ("http", "jdbc", "dbt", "script", "shell", "bigquery", "snowflake", "sql"))]
    if risky and not any(t.get("retry") for t in risky):
        out.append(Finding("KS002", "low", tr("{flow}: I/O tasks without retry", flow=fid), tr("Transient network/warehouse failures bring the flow down."), ref, "pre-merge"))
    if tasks and not any(t.get("timeout") for t in tasks) and not doc.get("timeout"):
        out.append(Finding("KS003", "low", tr("{flow}: no timeout", flow=fid), tr("A stuck execution keeps a worker busy."), ref, "pre-merge"))
    scheduled = [t for t in (doc.get("triggers") or []) if "schedule" in str(t.get("type", "")).lower()]
    if scheduled and not doc.get("concurrency"):
        out.append(Finding("KS004", "medium", tr("{flow}: scheduled without a concurrency limit", flow=fid),
                           tr("If one execution runs late, the next ones pile up in parallel (memory and resource contention)."), ref, "pre-merge",
                           {"recommendation": "concurrency: {limit: 1, behavior: QUEUE|CANCEL}"}))
    for t in scheduled:
        bf = (t.get("backfill") or {}).get("start")
        if bf and str(bf)[:4].isdigit() and int(str(bf)[:4]) < dt.date.today().year - 1:
            out.append(Finding("KS006", "medium", tr("{flow}: backfill since {date}", flow=fid, date=bf), tr("Creates one execution per interval since the start date."), ref, "pre-merge"))
    raw = path.read_text()
    m = SECRET.search(raw)
    if m:
        out.append(Finding("KS005", "high", tr("{flow}: possible plain-text secret ({kind})", flow=fid, kind=m.group(1)),
                           tr("Credentials in the YAML leak through git and the UI."), ref, "pre-merge", {"recommendation": "use `{{ secret('NAME') }}`"}))
    if doc.get("disabled") is True:
        out.append(Finding("KS007", "info", tr("{flow}: flow disabled", flow=fid), "", ref, "continuous"))
    return out


@check("ORC001", "Lint of Airflow/Kestra definitions", stage="pre-merge",
       emits=tuple(f"AF00{i}" for i in range(8)) + tuple(f"KS00{i}" for i in range(8)))
def orchestration_lint(ctx: Context):
    out = []
    if ctx.airflow_dags_dir:
        for p in sorted(Path(ctx.airflow_dags_dir).rglob("*.py")):
            out.extend(_airflow_file(p))
    if ctx.kestra_flows_dir:
        for p in sorted(list(Path(ctx.kestra_flows_dir).rglob("*.yml")) + list(Path(ctx.kestra_flows_dir).rglob("*.yaml"))):
            out.extend(_kestra_file(p))
    return out
