"""Static analysis of a Python job (PySpark/pandas): what it reads, what it writes and how each output column derives from the inputs.

Interprets the AST without running the code. A DataFrame is a `Frame`: known columns (`cols`: output → {(dataset, column)}),
datasets read whole (`whole`: columns not named yet, passed along), columns used only in filters/joins (`ind`) and
`opaque` (an operation it does not understand: the output becomes conservative). It follows variables, chained calls, functions of the
same file and of sibling modules imported (inside the declaration directory), SQL built by concatenation/f-string/`format`/`%`,
`spark.sql(...)`, `filter/join/groupBy.agg/withColumn/select/rename/drop`, and in pandas `df["c"] = ...`, `merge`, `rename`, `drop`.
Best-effort heuristic: what it does not understand is marked `opaque` and never silently disappears.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .external import ExtRead, render_sql, sql_reads
from .i18n import tr

_READ_FMT = {"parquet", "csv", "json", "orc", "load", "table", "avro", "text", "delta"}
_PD_READ = {"read_csv", "read_parquet", "read_json", "read_orc", "read_table", "read_sql_table", "read_feather", "read_excel"}
_WRITE_FMT = {"parquet", "csv", "json", "orc", "save", "avro", "text", "saveAsTable", "insertInto", "to_parquet", "to_csv", "to_json",
              "to_orc", "to_feather", "to_sql", "to_excel"}
_PASS = {"orderBy", "sort", "sort_values", "limit", "distinct", "drop_duplicates", "dropDuplicates", "repartition", "coalesce", "cache",
         "persist", "alias", "copy", "reset_index", "head", "tail", "sample", "na", "fillna", "dropna", "toDF", "checkpoint", "hint",
         "withWatermark", "toPandas", "to_pandas", "astype"}
_MAX_DEPTH = 6
_MAX_STEPS = 20_000        # evaluations per job: the file may come from a PR; functions calling each other several times grow exponentially
_MAX_NODES = 200_000       # AST nodes summed (file + imported modules)
_MAX_MODULES = 20
_MAX_STR = 1_000_000


class _Budget(Exception):
    pass


@dataclass
class Frame:
    cols: dict = field(default_factory=dict)         # output column → {(dataset, column)}
    whole: set = field(default_factory=set)          # datasets whose columns pass along unnamed
    ind: set = field(default_factory=set)            # {(dataset, column)} used only in filter/join/grouping
    opaque: bool = False

    def copy(self) -> "Frame":
        return Frame({k: set(v) for k, v in self.cols.items()}, set(self.whole), set(self.ind), self.opaque)

    def resolve(self, name: str) -> set:
        name = name.lower()
        if name in self.cols:
            return set(self.cols[name])
        return {(d, name) for d in self.whole}


@dataclass
class _Mark:                 # Spark session / reader / writer / grouping
    kind: str
    frame: Frame | None = None
    keys: set = field(default_factory=set)


class _Run:
    def __init__(self, base: Path | None, params, dialect, project):
        self.base, self.params, self.dialect, self.project = base, params, dialect, project
        self.outputs: list[tuple[str, Frame]] = []
        self.reads: list[str] = []
        self.funcs: dict[str, tuple[ast.FunctionDef, dict]] = {}
        self.mods: dict[str, Path] = {}
        self.loaded: set[str] = set()
        self.depth = 0
        self.unknown_sql = False
        self.steps = 0
        self.nodes = 0
        self.truncated = False

    def tick(self, n: int = 1):
        self.steps += n
        if self.steps > _MAX_STEPS:
            raise _Budget()

    # ---- sibling modules (only inside the declaration directory) ----
    def _load_module(self, dotted: str):
        if self.base is None or dotted in self.loaded:
            return
        if len(self.loaded) >= _MAX_MODULES:
            return
        self.loaded.add(dotted)
        root = self.base.resolve()
        path = (root / (dotted.replace(".", "/") + ".py")).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 1_000_000:
            return
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, ValueError, RecursionError):
            return
        self.nodes += sum(1 for _ in ast.walk(tree))
        if self.nodes > _MAX_NODES:
            return
        for n in tree.body:
            if isinstance(n, ast.FunctionDef):
                self.funcs.setdefault(f"{dotted}.{n.name}", (n, {}))
                self.funcs.setdefault(n.name, (n, {})) if False else None

    # ---- strings (built SQL) ----
    def string(self, n, env) -> str | None:
        r = self._string(n, env)
        return r if r is None or len(r) <= _MAX_STR else r[:_MAX_STR]

    def _string(self, n, env) -> str | None:
        self.tick()
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value
        if isinstance(n, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else self._hole(v.value, env) for v in n.values)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            a, b = self.string(n.left, env), self.string(n.right, env)
            return None if a is None and b is None else (a or self._hole(n.left, env)) + (b or self._hole(n.right, env))
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
            a = self.string(n.left, env)
            return None if a is None else __import__("re").sub(r"%[sdrfi]", lambda m: "{arg}", a)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "format":
                a = self.string(n.func.value, env)
                return None if a is None else __import__("re").sub(r"\{[^{}]*\}", lambda m: "{arg}", a)
            if n.func.attr == "join" and n.args:
                return self.string(n.args[0], env)
            if n.func.attr in ("strip", "lstrip", "rstrip", "replace", "lower", "upper", "dedent"):
                return self.string(n.func.value, env)
        if isinstance(n, ast.Name) and isinstance(env.get(n.id), str):
            return env[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("dedent", "str", "textwrap") and n.args:
            return self.string(n.args[0], env)
        return None

    def _hole(self, n, env) -> str:
        s = self.string(n, env) if not isinstance(n, str) else n
        if s is not None:
            return s
        return "{" + (n.id if isinstance(n, ast.Name) else "arg") + "}"

    # ---- column expressions ----
    def refs(self, expr, frame: Frame) -> set:
        out = set()
        for x in ast.walk(expr):
            self.tick()
            if isinstance(x, ast.Constant) and isinstance(x.value, str) and x.value.isidentifier():
                out |= frame.resolve(x.value)
            elif isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id in ("df", "data", "frame", "d"):
                out |= frame.resolve(x.attr)
        return out

    def out_name(self, expr) -> str | None:
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr in ("alias", "name") and expr.args:
            s = expr.args[0]
            return s.value.lower() if isinstance(s, ast.Constant) and isinstance(s.value, str) else None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str) and expr.value.isidentifier():
            return expr.value.lower()
        if isinstance(expr, ast.Call) and isinstance(expr.func, (ast.Attribute, ast.Name)):
            f = expr.func.attr if isinstance(expr.func, ast.Attribute) else expr.func.id
            if f == "col" and expr.args and isinstance(expr.args[0], ast.Constant):
                return str(expr.args[0].value).lower()
        if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str):
            return expr.slice.value.lower()
        return None

    # ---- evaluation ----
    def ev(self, n, env):
        self.tick()
        if isinstance(n, ast.Name):
            if env.get(n.id) is not None:
                return env[n.id]
            return _Mark("session") if n.id in ("spark", "sqlContext", "sc", "session", "SparkSession", "SQLContext", "HiveContext") else None
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, (ast.JoinedStr, ast.BinOp)):
            s = self.string(n, env)
            return s
        if isinstance(n, ast.Attribute):
            b = self.ev(n.value, env)
            if isinstance(b, _Mark) and b.kind == "session" and n.attr == "read":
                return _Mark("reader")
            if isinstance(b, _Mark) and b.kind == "session" and n.attr == "catalog":
                return _Mark("session")
            if isinstance(b, Frame) and n.attr == "write":
                return _Mark("writer", b)
            if isinstance(b, Frame) and n.attr in ("columns", "na", "T"):
                return b
            return b if isinstance(b, (Frame, _Mark)) else None
        if isinstance(n, ast.Subscript):
            b = self.ev(n.value, env)
            if isinstance(b, Frame):          # df[["a","b"]] = selection; df["a"] = column (assignment is handled in `stmt`)
                if isinstance(n.slice, (ast.List, ast.Tuple)):
                    names = [self.out_name(e) for e in n.slice.elts]
                    f = b.copy()
                    f.cols = {c: b.resolve(c) for c in names if c}
                    f.whole = set()
                    return f
                return b
            return None
        if isinstance(n, ast.Call):
            return self.call(n, env)
        return None

    def read(self, ds, kind="read") -> Frame:
        self.reads.append(ds)
        return Frame(whole={ds})

    def call(self, n: ast.Call, env):
        fn = n.func
        args = [self.ev(a, env) for a in n.args]
        if isinstance(fn, ast.Name):
            return self.call_user(fn.id, n, env)
        if not isinstance(fn, ast.Attribute):
            return None
        m = fn.attr
        base = self.ev(fn.value, env)
        s0 = self.string(n.args[0], env) if n.args else None
        if isinstance(fn.value, ast.Name) and fn.value.id in ("pd", "pandas", "dd", "pl", "polars") and m in _PD_READ and s0:
            return self.read(s0)
        if isinstance(fn.value, ast.Name) and env.get(fn.value.id) is None and not isinstance(base, _Mark):       # module.function
            u = self.call_user(f"{fn.value.id}.{m}", n, env)
            if u is not None:
                return u
        if isinstance(base, _Mark):
            k = base.kind
            if k == "session":
                if m == "table" and s0:
                    return self.read(s0)
                if m == "sql" and s0 is not None:
                    return self.sql(s0)
                if m in ("builder", "appName", "master", "config", "getOrCreate", "enableHiveSupport"):
                    return base
                if m in ("createDataFrame", "range"):
                    return Frame()
                return base
            if k == "reader":
                if m in _READ_FMT and s0 and m != "load" or (m == "load" and s0):
                    return self.read(s0)
                if m == "table" and s0:
                    return self.read(s0)
                return base                       # format/option/schema/...
            if k == "writer":
                if m in _WRITE_FMT and s0:
                    self.outputs.append((s0, base.frame))
                    return None
                return base                       # mode/option/format/partitionBy...
            if k == "grouped":
                f = base.frame.copy()
                if m == "agg":
                    f.cols = {c: base.frame.resolve(c) for c in base.keys}
                    for a in n.args:
                        nm = self.out_name(a)
                        if nm:
                            f.cols[nm] = self.refs(a, base.frame)
                    for kw in n.keywords:                          # pandas: agg(total=("amount", "sum"))
                        if kw.arg:
                            f.cols[kw.arg.lower()] = self.refs(kw.value, base.frame)
                else:                                              # count/sum/avg/max/min(col...)
                    f.cols = {c: base.frame.resolve(c) for c in base.keys}
                    for a in n.args:
                        s = self.string(a, env)
                        if s:
                            f.cols[f"{m}({s.lower()})"] = base.frame.resolve(s)
                            f.cols[s.lower()] = base.frame.resolve(s)
                    f.cols.setdefault("count", set())
                f.whole = set()
                f.ind |= {x for c in base.keys for x in base.frame.resolve(c)}
                return f
            return None
        if isinstance(base, Frame):
            return self.frame_method(base, m, n, env, s0)
        # DataFrame.write on an unknown object, `to_*` on an unknown frame: no information
        return None

    def frame_method(self, b: Frame, m: str, n: ast.Call, env, s0):
        f = b.copy()
        if m in ("select", "selectExpr"):
            cols: dict = {}
            for a in n.args:
                if isinstance(a, ast.Constant) and a.value == "*":
                    cols.update(f.cols)
                    continue
                if m == "selectExpr" and isinstance(a, ast.Constant) and isinstance(a.value, str):
                    nm = a.value.rsplit(" as ", 1)[-1].strip().lower() if " as " in a.value.lower() else a.value.strip().lower()
                    cols[nm.split(".")[-1]] = {r for w in __import__("re").findall(r"[A-Za-z_]\w*", a.value) for r in f.resolve(w)}
                    continue
                if isinstance(a, (ast.List, ast.Tuple)):
                    for e in a.elts:
                        nm = self.out_name(e)
                        if nm:
                            cols[nm] = self.refs(e, f) or f.resolve(nm)
                    continue
                nm = self.out_name(a)
                if nm:
                    cols[nm] = self.refs(a, f) or f.resolve(nm)
                else:
                    f.opaque = True
            keep_whole = any(isinstance(a, ast.Constant) and a.value == "*" for a in n.args)
            f.cols = {**({} if not keep_whole else f.cols), **cols}
            if not keep_whole:
                f.whole = set()
            return f
        if m == "withColumn" and len(n.args) == 2:
            nm = self.string(n.args[0], env)
            if nm:
                f.cols[nm.lower()] = self.refs(n.args[1], f)
                return f
            f.opaque = True
            return f
        if m == "withColumnRenamed" and len(n.args) == 2:
            a, c = self.string(n.args[0], env), self.string(n.args[1], env)
            if a and c:
                f.cols[c.lower()] = f.cols.pop(a.lower(), None) or f.resolve(a)
                return f
            f.opaque = True
            return f
        if m == "rename":
            for kw in n.keywords:
                if kw.arg == "columns" and isinstance(kw.value, ast.Dict):
                    for k, v in zip(kw.value.keys, kw.value.values):
                        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                            f.cols[str(v.value).lower()] = f.cols.pop(str(k.value).lower(), None) or f.resolve(str(k.value))
            return f
        if m in ("drop", "drop_duplicates") and m == "drop":
            names = [self.string(a, env) for a in n.args] + [self.string(e, env) for kw in n.keywords if kw.arg == "columns"
                                                              for e in (kw.value.elts if isinstance(kw.value, (ast.List, ast.Tuple)) else [kw.value])]
            for x in names:
                if x:
                    f.cols.pop(x.lower(), None)
            return f
        if m in ("filter", "where", "query", "loc", "isin"):
            for a in n.args:
                f.ind |= self.refs(a, f) if not isinstance(a, ast.Constant) or not isinstance(a.value, str) else \
                    {r for w in __import__("re").findall(r"[A-Za-z_]\w*", a.value) for r in f.resolve(w)}
            return f
        if m in ("groupBy", "groupby", "group_by", "rollup", "cube"):
            keys = set()
            for a in n.args:
                for e in (a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a]):
                    s = self.string(e, env) or self.out_name(e)
                    if s:
                        keys.add(s.lower())
            return _Mark("grouped", f, keys)
        if m in ("join", "merge", "crossJoin"):
            other = self.ev(n.args[0], env) if n.args else None
            if isinstance(other, Frame):
                for a in n.args[1:] + [kw.value for kw in n.keywords if kw.arg in ("on", "left_on", "right_on")]:
                    f.ind |= self.refs(a, f) | self.refs(a, other)
                f.cols = {**{c: set(v) for c, v in other.cols.items()}, **f.cols}
                f.whole |= other.whole
                f.ind |= other.ind
                f.opaque = f.opaque or other.opaque
                return f
            f.opaque = True
            return f
        if m in ("union", "unionByName", "unionAll", "concat", "append"):
            other = self.ev(n.args[0], env) if n.args else None
            if isinstance(other, Frame):
                for c, v in other.cols.items():
                    f.cols.setdefault(c, set()).update(v)
                f.whole |= other.whole
                f.ind |= other.ind
                return f
            f.opaque = True
            return f
        if m in _WRITE_FMT and (s0 or m.startswith("to_")):
            if s0:
                self.outputs.append((s0, f))
            return None
        if m in ("write",):
            return _Mark("writer", f)
        if m == "sql" and s0 is not None:
            return self.sql(s0)
        if m in _PASS:
            return f
        f.opaque = True            # mapInPandas, applyInPandas, rdd.map, apply, transform(func): the output is unknown
        return f

    def sql(self, text: str) -> Frame:
        txt, _ = render_sql(text, self.params)
        try:
            rs = sql_reads("job", None, ("__out__",), txt, self.dialect, self.project)
        except ValueError:
            self.unknown_sql = True
            return Frame(opaque=True)
        f = Frame()
        for r in rs:
            self.reads.append(r.dataset)
            if r.column == "*":
                f.whole.add(r.dataset)
            for d, c in r.feeds:
                if c == "*":
                    f.ind.add((r.dataset, r.column))
                else:
                    f.cols.setdefault(c, set()).add((r.dataset, r.column))
            if not r.feeds and r.column != "*":
                f.ind.add((r.dataset, r.column))
        target = _sql_target(txt, self.dialect)
        if target:               # INSERT/CREATE ... AS inside spark.sql: it is a write
            self.outputs.append((target, f))
        return f

    def call_user(self, name: str, n: ast.Call, env):
        entry = self.funcs.get(name)
        if entry is None or self.depth >= _MAX_DEPTH:
            return None
        fd = entry[0]
        local = {}
        names = [a.arg for a in fd.args.args]
        for i, a in enumerate(n.args):
            if i < len(names):
                local[names[i]] = self.ev(a, env)
        for kw in n.keywords:
            if kw.arg:
                local[kw.arg] = self.ev(kw.value, env)
        self.depth += 1
        try:
            return self.block(fd.body, local, ret=True)
        finally:
            self.depth -= 1

    def stmt(self, s, env):
        self.tick()
        if isinstance(s, ast.Assign):
            v = self.ev(s.value, env)
            for t in s.targets:
                if isinstance(t, ast.Name):
                    env[t.id] = v
                elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and isinstance(env.get(t.value.id), Frame):
                    nm = self.string(t.slice, env) if isinstance(t.slice, ast.Constant) else None
                    fr = env[t.value.id]
                    if nm:
                        fr.cols[nm.lower()] = self.refs(s.value, fr)
                    else:
                        fr.opaque = True
                elif isinstance(t, ast.Tuple) and isinstance(s.value, ast.Tuple):
                    for tt, vv in zip(t.elts, s.value.elts):
                        if isinstance(tt, ast.Name):
                            env[tt.id] = self.ev(vv, env)
        elif isinstance(s, ast.AugAssign) and isinstance(s.target, ast.Name):
            a, b = self.string(s.target, env), self.string(s.value, env)
            if isinstance(s.op, ast.Add) and a is not None and b is not None:
                env[s.target.id] = a + b
        elif isinstance(s, ast.Expr):
            self.ev(s.value, env)
        elif isinstance(s, ast.FunctionDef):
            self.funcs.setdefault(s.name, (s, {}))
        elif isinstance(s, ast.ClassDef):
            for x in s.body:
                if isinstance(x, ast.FunctionDef):
                    self.funcs.setdefault(x.name, (x, {}))
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            if isinstance(s, ast.ImportFrom) and s.module:
                self._load_module(s.module)
                for a in s.names:
                    if f"{s.module}.{a.name}" in self.funcs:
                        self.funcs.setdefault(a.asname or a.name, self.funcs[f"{s.module}.{a.name}"])
            elif isinstance(s, ast.Import):
                for a in s.names:
                    self._load_module(a.name)
                    for k, v in list(self.funcs.items()):
                        if k.startswith(a.name + ".") and a.asname:
                            self.funcs.setdefault(a.asname + k[len(a.name):], v)
        elif isinstance(s, ast.Return):
            return self.ev(s.value, env) if s.value is not None else None
        elif isinstance(s, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            for part in ("body", "orelse", "finalbody"):
                r = self.block(getattr(s, part, []) or [], env, ret=True)
                if r is not None:
                    return r
            for h in getattr(s, "handlers", []):
                self.block(h.body, env)
        return None

    def block(self, body, env, ret=False):
        # functions first: `main()` may be called before its `def` appears in the file (module execution order is by call)
        for s in body:
            if isinstance(s, ast.FunctionDef):
                self.funcs.setdefault(s.name, (s, {}))
        out = None
        for s in body:
            r = self.stmt(s, env)
            if r is not None and ret:
                out = r
        return out


def _sql_target(text: str, dialect: str) -> str | None:
    import sqlglot
    from sqlglot import exp
    try:
        for st in sqlglot.parse(text, read=dialect):
            if isinstance(st, (exp.Create, exp.Insert)):
                t = st.this if isinstance(st.this, exp.Table) else getattr(st.this, "this", None)
                if isinstance(t, exp.Table):
                    return ".".join(x for x in (t.catalog, t.db, t.name) if x)
    except sqlglot.errors.SqlglotError:
        return None
    return None


def reads(name: str, owner, src: str, writes: tuple, dialect: str, project, params: dict | None = None, base: Path | None = None) -> list[ExtRead]:
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        raise ValueError(tr("job '{job}': py_file is not valid Python", job=name)) from None
    run = _Run(base, params, dialect, project)
    run.nodes = sum(1 for _ in ast.walk(tree))
    if run.nodes > _MAX_NODES:
        raise ValueError(tr("job '{job}': py_file too large to analyze (> {n} nodes)", job=name, n=_MAX_NODES))
    env: dict = {}
    try:
        run.block(tree.body, env, ret=True)
        if not run.outputs and not run.reads:
            # nothing recognized: run the file's functions as entry points (e.g. `def main(spark)` called by a scheduler)
            for fn, (fd, _) in list(run.funcs.items()):
                local = {a.arg: (_Mark("session") if a.arg in ("spark", "session") else None) for a in fd.args.args}
                run.block(fd.body, local, ret=True)
    except (_Budget, RecursionError):
        run.truncated = True            # budget exceeded: what was seen counts, and the output becomes conservative
    w = tuple(dict.fromkeys(writes or [d for d, _ in run.outputs]))
    out: list[ExtRead] = []
    seen: set = set()

    def add(ds, col, feeds):
        k = (ds, col, feeds)
        if k not in seen:
            seen.add(k)
            out.append(ExtRead(name, ds, col, "declared", owner, w, feeds))

    used: set = set()
    targets = list(run.outputs) or ([(w[0], Frame(whole=set(run.reads), opaque=True))] if w else [])
    if run.truncated:
        targets = [(d, Frame(cols=fr.cols, whole=fr.whole | set(run.reads), ind=fr.ind, opaque=True)) for d, fr in targets] or \
                  ([(w[0], Frame(whole=set(run.reads), opaque=True))] if w else [])
    for ds, fr in targets:
        for c, srcs in fr.cols.items():
            for sds, scol in srcs:
                add(sds, scol, ((ds, c),))
                used.add(sds)
        for sds in fr.whole:
            add(sds, "*", ((ds, "*"),))
            used.add(sds)
        for sds, scol in fr.ind:
            add(sds, scol, ((ds, "*"),))
            used.add(sds)
        if fr.opaque:       # operation not understood: every input may affect any output column
            for srcs in fr.cols.values():
                for sds, scol in srcs:
                    add(sds, scol, ((ds, "*"),))
    for ds in dict.fromkeys(run.reads):
        if ds not in used:
            add(ds, "*", ())              # read, but without an identifiable destination: conservative
    return out
