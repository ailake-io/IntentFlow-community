"""Consumers outside the project SQL (Spark, Python, notebooks, BI tools): reads of columns/datasets.

Two sources, same shape: OpenLineage events already ingested (`ol_jobs`/`ol_edges`) and an optional hand-written
`intentflow.lineage.json` for jobs that do not emit OpenLineage:

    {"jobs": [{"name": "ltv_spark", "owner": "team-ds",
               "reads": [{"dataset": "customers", "columns": ["email", "id"]}, {"dataset": "orders"}],
               "writes": ["warehouse.ltv"]}]}

Missing `columns` = the whole dataset (any removed/changed column affects it).
`aliases` (optional, at the top of the file) links an external name to a model when suffix matching fails or is ambiguous:
`{"aliases": {"file:/lake/raw/customers": "stg_customers"}, "jobs": [...]}` (also applies to datasets coming from OpenLineage).
A SQL job (Spark SQL, notebook with `spark.sql`) can carry the text instead of `reads`:
`{"name": "ltv", "sql_file": "jobs/ltv.sql", "dialect": "spark", "writes": ["wh.ltv"]}` (or `"sql": "select ..."`; path relative to the
declaration file). Column lineage (reads and `feeds`) is computed by the same engine as the project, without declaring it by hand.
`params` replaces `${v}`, `$v`, `{v}`, `{{ v }}`; `{{ ref('m') }}`/`{{ source('s','t') }}` become the name; without a value the placeholder becomes
`__param_v__` (a table name still matches by suffix). Scripts with several statements (`INSERT ... SELECT`, `CREATE TABLE AS`) are accepted.
Python job (`"py_file"`): static analysis of the code (PySpark/pandas, see `pyflow`). It is heuristic (the code is never run); what it does not
understand stays conservative.
Datasets written by one job and read by another form a chain: the change propagates through the outputs. With `feeds` (per read) or
OpenLineage `columnLineage` the propagation is per column; without them it is conservative (any column of what the job writes):
`{"dataset": "customers", "columns": ["email"], "feeds": [{"dataset": "wh.ltv", "columns": ["score"]}]}`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .i18n import N_, tr

DECLARED_FILE = "intentflow.lineage.json"
REASONS = {"ok": N_("ok"), "ambiguous": N_("ambiguous"), "missing": N_("missing")}


@dataclass(frozen=True)
class ExtRead:
    job: str
    dataset: str          # name as the source wrote it (`ns:db.schema.table`, `table`, `schema.table`)
    column: str           # "*" = whole dataset
    source: str           # declared | openlineage
    owner: str | None = None
    writes: tuple = ()
    feeds: tuple = ()     # ((dataset, column), ...) this read feeds; empty = unknown (propagates to everything the job writes)


def _read_file(path) -> dict:
    p = Path(path) if path else None
    if p is None or not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list) or not isinstance(data.get("aliases", {}), dict):
            raise TypeError('expected {"jobs": [...], "aliases": {...}}')
        return data
    except (ValueError, TypeError) as e:
        raise ValueError(tr("{file}: invalid declared lineage file ({error})", file=p, error=f"{type(e).__name__}: {e}")) from None


def load_aliases(path) -> dict[str, str]:
    return {str(k): str(v) for k, v in _read_file(path).get("aliases", {}).items()}


def _safe_file(base: Path, ref: str, exts: tuple, name: str, what: str) -> str:
    """The declaration file may come from a PR: only read inside its directory, never an absolute path or `..`."""
    root = base.resolve()
    target = (root / str(ref)).resolve()
    if not target.is_relative_to(root) or target.suffix.lower() not in exts or not target.is_file():
        raise ValueError(tr("job '{job}': {what} must be a {exts} file inside {dir}/", job=name, what=what, exts="/".join(exts), dir=root.name))
    if target.stat().st_size > 1_000_000:
        raise ValueError(tr("job '{job}': {what} is over 1 MB", job=name, what=what))
    return target.read_text()


# Bounded quantifiers and non-overlapping classes: linear time in the text size (the SQL may come from a PR)
_PARAM = re.compile(r"\$\{([^{}$]{1,200})\}|\{\{([^{}]{1,200})\}\}|(?<![\w$])\$([A-Za-z_]\w{0,100})|\{([A-Za-z_]\w{0,100})\}")


def _strip_blocks(text: str, opener: str, closer: str) -> str:
    out, i = [], 0
    while True:
        j = text.find(opener, i)
        k = text.find(closer, j + len(opener)) if j >= 0 else -1
        if j < 0 or k < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        i = k + len(closer)


def render_sql(text: str, params: dict | None = None) -> tuple[str, list[str]]:
    """Resolve macros/variables of parameterized SQL: `${v}`, `$v`, `{v}`, `{{ v }}`, `{{ ref('m') }}`, `{{ source('s','t') }}`; Jinja tags are dropped.

    An unknown value becomes the identifier `__param_v__` (valid as a table, column or literal) and is returned in `unresolved`.
    """
    params = {str(k): v for k, v in (params or {}).items()}
    unresolved: set[str] = set()
    if len(text) > 1_000_000:
        raise ValueError(tr("SQL over 1 MB"))
    text = _strip_blocks(_strip_blocks(text, "{%", "%}"), "{#", "#}")

    def repl(m):
        key = next(g for g in m.groups() if g).strip()
        r = re.fullmatch(r"ref\(\s*['\"]([\w.]+)['\"]\s*\)", key)
        if r:
            return r.group(1)
        r = re.fullmatch(r"source\(\s*['\"]([\w.]+)['\"]\s*,\s*['\"]([\w.]+)['\"]\s*\)", key)
        if r:
            return f"{r.group(1)}.{r.group(2)}"
        if key in params:
            return str(params[key])
        unresolved.add(key)
        return "__param_" + re.sub(r"\W", "_", key) + "__"
    return _PARAM.sub(repl, text), sorted(unresolved)


def _dataset_name(t) -> str:
    return ".".join(x for x in (t.catalog, t.db, t.name) if x)


def sql_reads(name: str, owner, writes: tuple, text: str, dialect: str, project) -> list[ExtRead]:
    """Reads (column by column) and `feeds` of a SQL script with one or more statements (SELECT, INSERT ... SELECT, CREATE TABLE AS)."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.lineage import lineage as sg_lineage
    from sqlglot.optimizer.qualify import qualify
    try:
        stmts = [t for t in sqlglot.parse(text, read=dialect) if t is not None]
    except sqlglot.errors.SqlglotError:
        raise ValueError(tr("job '{job}': invalid SQL for the '{dialect}' dialect", job=name, dialect=dialect)) from None       # never echo the content
    schema = project.sqlglot_schema() if project is not None else {}
    out: list[ExtRead] = []
    seen: set = set()
    for st in stmts:
        target = None
        if isinstance(st, (exp.Create, exp.Insert)) and isinstance(st.this, (exp.Table, exp.Schema)):
            tt = st.this if isinstance(st.this, exp.Table) else st.this.this
            target = _dataset_name(tt) if isinstance(tt, exp.Table) else None
        sel = st if isinstance(st, exp.Select) else st.find(exp.Select)
        if sel is None:
            continue
        dsts = writes or ((target,) if target else ())
        dst = dsts[0] if dsts else ""
        ctes = {c.alias_or_name.lower() for c in sel.find_all(exp.CTE)}
        try:
            q = qualify(sel.copy(), schema=schema, dialect=dialect, validate_qualify_columns=False, identify=False)
        except Exception:
            q = sel.copy()
        got = False
        for col in q.named_selects:
            try:
                root = sg_lineage(col, q, schema=schema, dialect=dialect, trim_selects=False)
            except Exception:
                continue
            stack = [root]
            while stack:
                n = stack.pop()
                stack.extend(n.downstream)
                if n.downstream or not isinstance(n.expression, exp.Table):
                    continue
                ds = _dataset_name(n.expression)
                src_col = n.name.split(".")[-1].strip('"`[]').lower()
                if ds.lower() in ctes or (ds, src_col, col, dst) in seen:
                    continue
                seen.add((ds, src_col, col, dst))
                got = True
                out.append(ExtRead(name, ds, src_col, "declared", owner, dsts, ((dst, col.lower()),) if dst else ()))
        # columns used only in JOIN/WHERE/GROUP BY/window: not in the SELECT, but changing them changes the whole result
        tabs = {}
        for t in q.find_all(exp.Table):
            tabs[(t.alias_or_name or "").lower()] = _dataset_name(t)
        only = next(iter(tabs.values())) if len(tabs) == 1 else None
        scopes = [q.args.get(k) for k in ("where", "group", "having", "order", "qualify")] + list(q.args.get("joins") or []) \
            + list(q.find_all(exp.Window))
        cols = [c for sc in scopes if sc is not None for c in sc.find_all(exp.Column)]
        for c in cols:                   # only filter/join/grouping/ordering/window; the projection is already direct lineage
            ds = tabs.get((c.table or "").lower()) if c.table else only
            if not ds or ds.lower() in ctes:
                continue
            col = c.name.lower()
            if (ds, col, "*", dst) in seen:
                continue
            seen.add((ds, col, "*", dst))
            got = True
            out.append(ExtRead(name, ds, col, "declared", owner, dsts, ((dst, "*"),) if dst else ()))
        if not got:        # column lineage not resolved (e.g. `select *` without a schema): conservative read of each table
            for t in sel.find_all(exp.Table):
                ds = _dataset_name(t)
                if ds.lower() not in ctes and (ds, "*", "*", dst) not in seen:
                    seen.add((ds, "*", "*", dst))
                    out.append(ExtRead(name, ds, "*", "declared", owner, dsts))
    return out


def _sql_reads(job: dict, base: Path, project) -> list[ExtRead]:
    name = str(job["name"])
    text = str(job["sql"]) if job.get("sql") else _safe_file(base, job["sql_file"], (".sql", ".hql", ".txt"), name, "sql_file")
    text, _ = render_sql(text, job.get("params"))
    return sql_reads(name, job.get("owner"), tuple(str(w) for w in job.get("writes", [])), text, job.get("dialect") or "spark", project)


def _py_reads(job: dict, base: Path, project) -> list[ExtRead]:
    from . import pyflow
    name = str(job["name"])
    src = _safe_file(base, job["py_file"], (".py",), name, "py_file")
    return pyflow.reads(name, job.get("owner"), src, tuple(str(w) for w in job.get("writes", [])), job.get("dialect") or "spark",
                        project, job.get("params"), base=base)


def load_declared(path, project=None) -> list[ExtRead]:
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = _read_file(p)
        jobs = data.get("jobs", [])
        out = []
        for j in jobs:
            writes = tuple(str(w) for w in j.get("writes", []))
            if "sql" in j or "sql_file" in j:
                out += _sql_reads(j, p.parent, project)
                continue
            if "py_file" in j:
                out += _py_reads(j, p.parent, project)
                continue
            for r in j["reads"]:
                cols = [c.lower() for c in r.get("columns") or []] or ["*"]
                feeds = tuple((str(f["dataset"]), (c2 or "*").lower()) for f in r.get("feeds", []) for c2 in (f.get("columns") or [None]))
                out += [ExtRead(str(j["name"]), str(r["dataset"]), c, "declared", j.get("owner"), writes, feeds) for c in cols]
        return out
    except ValueError:
        raise
    except (KeyError, TypeError, AttributeError, OSError) as e:
        raise ValueError(tr("{file}: invalid declared lineage file ({error})", file=p.name, error=type(e).__name__)) from None


def load_openlineage(store) -> list[ExtRead]:
    jobs = {j["job"]: j for j in store.ol_jobs()}
    out, with_cols, feeds = [], set(), {}
    for e in store.ol_edges():
        feeds.setdefault((e["job"], e["src_ds"], str(e["src_col"]).lower()), []).append((e["dst_ds"], str(e["dst_col"]).lower()))
    for (job, ds, col), f in feeds.items():
        out.append(ExtRead(job, ds, col, "openlineage", None, tuple(jobs.get(job, {}).get("outputs", [])), tuple(f)))
        with_cols.add((job, ds))
    for name, j in jobs.items():          # input without a column edge: read of the whole dataset
        out += [ExtRead(name, ds, "*", "openlineage", None, tuple(j["outputs"])) for ds in j["inputs"] if (name, ds) not in with_cols]
    return out


def load(store=None, declared=None, project=None) -> list[ExtRead]:
    from dataclasses import replace
    al = load_aliases(declared) if declared else {}
    reads = (load_declared(declared, project) if declared else []) + (load_openlineage(store) if store is not None else [])
    ref = lambda d: al.get(d, d)
    return [replace(r, dataset=ref(r.dataset), writes=tuple(ref(w) for w in r.writes), feeds=tuple((ref(d), c) for d, c in r.feeds))
            for r in reads] if al else reads


_EXT = (".parquet", ".csv", ".json", ".orc", ".avro", ".delta", ".tsv", ".txt")


def _candidates(ref: str) -> list[list[str]]:
    """Lists of name parts, from the most specific to the least: `db.schema.tab`, or the last 3/2/1 segments of a path."""
    name = ref.rsplit(":", 1)[-1]
    clean = lambda x: x.strip('"`[]').lower()
    if "/" in name:        # file dataset (Spark/Parquet): the last path segments are schema/table
        segs = [clean(x) for x in name.split("/") if x.strip()]
        if segs and segs[-1].endswith(_EXT):
            segs[-1] = segs[-1].rsplit(".", 1)[0]
        return [segs[-k:] for k in (3, 2, 1) if len(segs) >= k]
    parts = [clean(p) for p in name.split(".") if p.strip()]
    firm = [p for p in parts if not re.fullmatch(r"__param_\w+__", p)]       # an unresolved variable (`${db}.table`) names nothing
    return [x for x in (parts, firm) if x] if firm != parts else ([parts] if parts else [])


def match(project, ref: str) -> tuple[str | None, str]:
    """(uid, reason): `ok`, `missing` (no node) or `ambiguous` (the most specific matching suffix points to more than one node)."""
    rels = {uid: [p.strip('"`[]').lower() for p in n.relation.split(".")] if n.relation else [] for uid, n in project.nodes.items()}
    for parts in _candidates(ref):
        hits = {uid for uid, n in project.nodes.items()
                if (rels[uid] and len(parts) <= len(rels[uid]) and rels[uid][-len(parts):] == parts)
                or (len(parts) == 1 and n.name.lower() == parts[0])}
        if len(hits) == 1:
            return next(iter(hits)), "ok"
        if hits:
            return None, "ambiguous"
    return None, "missing"


def match_dataset(project, ref: str) -> str | None:
    return match(project, ref)[0]


def unmatched(project, reads: list[ExtRead]) -> dict[str, dict]:
    """{dataset: {"reason", "jobs"}} of the read datasets that did not match. `ambiguous` is the dangerous one: the impact silently disappears."""
    out: dict[str, dict] = {}
    for r in reads:
        uid, why = match(project, r.dataset)
        if not uid:
            out.setdefault(r.dataset, {"reason": why, "jobs": set()})["jobs"].add(r.job)
    return {k: {"reason": v["reason"], "jobs": sorted(v["jobs"])} for k, v in out.items()}


def resolve(project, reads: list[ExtRead]) -> list[tuple[str, str, ExtRead]]:
    """[(uid, column, read)] of the reads that match a project node (the others are unknown to it)."""
    cache: dict[str, str | None] = {}
    out = []
    for r in reads:
        if r.dataset not in cache:
            cache[r.dataset] = match_dataset(project, r.dataset)
        if cache[r.dataset]:
            out.append((cache[r.dataset], r.column, r))
    return out


def _same(project, a: str, b: str) -> bool:
    """Same dataset: identical name (ignoring trailing slash and case) or both match the same project node."""
    if a.rstrip("/").lower() == b.rstrip("/").lower():
        return True
    ua, ub = match_dataset(project, a), match_dataset(project, b)
    return ua is not None and ua == ub


def readers(project, reads: list[ExtRead], touched: set[tuple[str, str]], models: set[str]) -> dict[str, dict]:
    """Affected external jobs: they read a column in `touched`, the whole dataset of a model in `models`, or an output of an affected job.

    {job: {"reads": [ExtRead], "via": previous job | None, "depth": 1..}}; propagation through outputs without feeds is conservative (whole dataset).
    """
    hit: dict[str, dict] = {}
    for uid, col, r in resolve(project, reads):
        if (uid, col) in touched or (col == "*" and uid in models):
            hit.setdefault(r.job, {"reads": [], "via": None, "depth": 1})["reads"].append(r)
    frontier = list(hit)
    while frontier:
        nxt = []
        for j in frontier:
            outs = {f for r in hit[j]["reads"] for f in (r.feeds or tuple((w, "*") for w in r.writes))}
            if not outs:
                continue
            for r in reads:
                if r.job in hit or r.job == j:
                    continue
                if any(_same(project, r.dataset, d) and "*" in (r.column, c) or (_same(project, r.dataset, d) and r.column == c) for d, c in outs):
                    hit.setdefault(r.job, {"reads": [], "via": j, "depth": hit[j]["depth"] + 1})["reads"].append(r)
                    if r.job not in nxt:
                        nxt.append(r.job)
        frontier = nxt
    return hit


def suggest(project, ref: str, n: int = 3) -> list[str]:
    """Model names similar to the dataset (to write the alias)."""
    import difflib
    last = _candidates(ref)[-1:] or [[ref]]
    want = last[0][-1] if last and last[0] else ref
    names = {x.name for x in project.nodes.values()}
    return difflib.get_close_matches(want.lower(), [x.lower() for x in names], n=n, cutoff=0.6)


def coverage(reads: list[ExtRead]) -> list[dict]:
    """Jobs with reads without `feeds`: propagation through their outputs is conservative (any written column)."""
    by: dict[str, set] = {}
    for r in reads:
        if r.writes and not r.feeds:
            by.setdefault(r.job, set()).add(f"{r.dataset}.{r.column}")
    return [{"job": j, "reads_without_feeds": sorted(v)} for j, v in sorted(by.items())]


def add_alias(path, dataset: str, model: str) -> None:
    """Write `aliases[dataset] = model` into intentflow.lineage.json (creates the file; keeps the rest)."""
    p = Path(path)
    data = _read_file(p) if p.is_file() else {}
    data.setdefault("jobs", [])
    data.setdefault("aliases", {})[dataset] = model
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
