"""Dynamic oracle to measure column lineage on real projects (DuckDB), without a hand-made answer key.

For each source column (seed/source) the oracle perturbs the data (NULL and value shift), rebuilds the project models from
the compiled SQL, and sees which output columns changed. It compares that with the closure of the static lineage (SQLGlot):

  * TP  = the lineage claims the dependency and the data changed;
  * FN  = the data changed and the lineage does not claim it (recall drops);
  * FP? = the lineage claims it, the model kept its row count and the column did not change under any perturbation ("suspect": it can be a
          real dependency the perturbation does not reveal, e.g. a COALESCE that never takes the branch; hence an upper bound of false positives);
  * undetermined = the model's row count changed under both perturbations (filter/join): indirect dependency, left out of the count.

It is a measured approximation, not an answer key. See docs/BENCHMARKS.md for the method and the numbers.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path

NUMERIC = re.compile(r"(?i)^(tinyint|smallint|integer|int|bigint|hugeint|ubigint|float|double|real|decimal|numeric)")


def _q(n: str) -> str:
    return '"' + n.replace('"', '""') + '"'


def _rel(node) -> str:
    return ".".join(_q(p.strip('"`[]')) for p in node.relation.split("."))


def _order(project, models: list[str]) -> list[str]:
    deps = {u: [d for d in project.nodes[u].depends_on if d in set(models)] for u in models}
    out, seen = [], set()

    def visit(u):
        if u in seen:
            return
        seen.add(u)
        for d in deps[u]:
            visit(d)
        out.append(u)
    for u in models:
        visit(u)
    return out


def _sample_bases(con, project, bases: list[str], cap: int) -> dict[str, list[int]]:
    """Shrink each source above `cap` rows by key hash (the same key falls in the same bucket in every table, so joins
    survive). Key = the first column whose name appears in 2+ sources, otherwise the first column. Works on the temporary copy."""
    cols: dict[str, list[str]] = {}
    for b in bases:
        try:
            cols[b] = [r[0] for r in con.execute(f"describe select * from {_rel(project.nodes[b])}").fetchall()]
        except Exception:
            continue
    seen: dict[str, int] = defaultdict(int)
    for cs in cols.values():
        for c in set(x.lower() for x in cs):
            seen[c] += 1
    out: dict[str, list[int]] = {}
    for b, cs in cols.items():
        r = _rel(project.nodes[b])
        try:
            n = con.execute(f"select count(*) from {r}").fetchone()[0]
            if n <= cap or not cs:
                continue
            key = next((c for c in cs if seen[c.lower()] > 1), cs[0])
            con.execute(f"create or replace temp table _smp as select * from {r} where hash({_q(key)}) % 10000 < {max(1, int(cap / n * 10000))}")
            con.execute(f"delete from {r}")
            con.execute(f"insert into {r} select * from _smp")
            con.execute("drop table _smp")
            out[project.nodes[b].name] = [n, con.execute(f"select count(*) from {r}").fetchone()[0]]
        except Exception:
            continue
    return out


def static_closure(edges, start: tuple[str, str], indirect=(), model_columns: dict | None = None, include_indirect: bool = True) -> set[tuple[str, str]]:
    """Columns reached by direct lineage and, with `include_indirect`, by indirect dependency (affects every column of the model)."""
    by: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        by[(e.src, e.src_col.strip('"`[]').lower())].append((e.dst, e.dst_col.strip('"`[]').lower()))
    if include_indirect:
        for src, col, dst, _via in indirect:
            for c in (model_columns or {}).get(dst, []):
                by[(src, col.lower())].append((dst, c.lower()))
    seen, stack = set(), [start]
    while stack:
        for nxt in by.get(stack.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def run(project, lineage_result, duckdb_path: str, package: str | None = None, max_columns: int | None = None,
        only_models: list[str] | None = None, progress=None, only_used: bool = False,
        only_sources: list[str] | None = None, sample_rows: int | None = None) -> dict:
    import duckdb
    models = [u for u, n in project.nodes.items() if n.resource_type == "model" and n.compiled_code and n.relation
              and (not package or u.startswith(f"model.{package}."))]
    if only_models:
        models = [u for u in models if project.nodes[u].name in only_models]
    models = _order(project, models)
    bases_all = [u for u, n in project.nodes.items() if n.resource_type in ("seed", "source") and n.relation]
    same: dict[str, list[str]] = {}           # a seed and a source can be the same physical table: test once, add up both nodes' lineage
    for u in bases_all:
        same.setdefault(project.nodes[u].relation.lower(), []).append(u)
    bases = [us[0] for us in same.values()]
    tmp = Path(tempfile.mkdtemp()) / Path(duckdb_path).name      # the file name is the catalog name in the relations
    shutil.copy(duckdb_path, tmp)
    con = duckdb.connect(str(tmp))

    def tab_cols(u):
        try:
            return [r[0] for r in con.execute(f"describe select * from {_rel(project.nodes[u])}").fetchall()]
        except Exception:
            return []

    def rebuild(ms=None):
        for u in (models if ms is None else ms):
            kind = "view" if project.nodes[u].materialized == "view" else "table"       # keep the original object type
            con.execute(f"create or replace {kind} {_rel(project.nodes[u])} as {project.nodes[u].compiled_code}")

    keys: dict[str, str | None] = {}

    def pick_key(u):
        """First unique, non-null column of the model (aligns rows across runs); None if there is none."""
        n = project.nodes[u]
        for c in tab_cols(u):
            r, d, nn = con.execute(f"select count(*), count(distinct {_q(c)}), count({_q(c)}) from {_rel(n)}").fetchone()
            if r > 1 and r == d == nn:
                return c
        return None

    def signature(ms=None):
        sig = {}
        txt = lambda c: f"coalesce(cast({_q(c)} as varchar), '<null>')"
        for u in (models if ms is None else ms):
            n = project.nodes[u]
            cols = [c for c in tab_cols(u)]
            rows = con.execute(f"select count(*) from {_rel(n)}").fetchone()[0]
            if u not in keys:
                keys[u] = pick_key(u)
            k = keys[u]
            hs = {}
            for c in cols:
                # with a key: pair (key, value) and see changes in WHICH row gets which value (ROW_NUMBER/RANK), which a multiset hides
                pair = f"{txt(k)} || '#' || {txt(c)}" if k and c != k else txt(c)
                hs[c.lower()] = con.execute(f"select md5(string_agg({pair}, '|' order by {pair})) from {_rel(n)}").fetchone()[0]
            sig[u] = (rows, hs)
        return sig

    t0 = time.time()
    unbuildable: dict[str, str] = {}          # SQL invalid/incompatible with DuckDB, or depends on such a model: out of the test, but reported
    built = []
    def _exists(u):
        try:
            con.execute(f"select 1 from {_rel(project.nodes[u])} limit 0")
            return True
        except Exception:
            return False
    # a model without parents that already exists in the file: its data is the source; rebuilding it (e.g. empty `graph` outside dbt) would wipe it
    keep = {u for u in models if not any(d in project.nodes for d in project.nodes[u].depends_on) and _exists(u)}
    for u in models:
        if u in keep:
            built.append(u)
            continue
        parts = [x.strip('"`[]') for x in project.nodes[u].relation.split(".")]
        if len(parts) == 3:       # the model's target schema may not exist in the data file
            con.execute(f'create schema if not exists {_q(parts[0])}.{_q(parts[1])}')
        kind = "view" if project.nodes[u].materialized == "view" else "table"
        try:
            con.execute(f"create or replace {kind} {_rel(project.nodes[u])} as {project.nodes[u].compiled_code}")
            built.append(u)
        except Exception as ex:
            unbuildable[u] = str(ex).strip().splitlines()[0][:160]
    models = built
    # a model without any parent (data from literals/`graph`) is the "source" of that project: its table is perturbed
    roots = [u for u in models if not any(d in project.nodes for d in project.nodes[u].depends_on)]
    models = [u for u in models if u not in roots]
    for u in roots:
        r_ = _rel(project.nodes[u])      # the root becomes a physical table (a view does not accept UPDATE) and is no longer rebuilt
        con.execute(f"create or replace temp table _root_copy as select * from {r_}")
        for drop in ("view", "table"):
            try:
                con.execute(f"drop {drop} if exists {r_}")
            except Exception:
                pass
        con.execute(f"create table {r_} as select * from _root_copy")
        con.execute("drop table _root_copy")
        same.setdefault(project.nodes[u].relation.lower(), []).append(u)
        if project.nodes[u].relation.lower() not in {project.nodes[b].relation.lower() for b in bases}:
            bases.append(u)
    sampled: dict[str, list[int]] = {}
    if sample_rows:
        sampled = _sample_bases(con, project, bases, sample_rows)
    rebuild()
    base_sig = signature()
    rebuild()
    again = signature()          # two builds without perturbation: what changes on its own (now(), random()) cannot count as an effect
    unstable_rows = {u for u in models if again[u][0] != base_sig[u][0]}
    unstable = {(u, c) for u in models for c, h in base_sig[u][1].items() if again[u][1].get(c) != h}
    mcols = {u: list(base_sig[u][1]) for u in models}
    targets = []
    for b in bases:
        n = project.nodes[b]
        try:       # a seed/source missing from the given DuckDB is not testable: skip it (its lineage stays unverified)
            desc = con.execute(f"describe select * from {_rel(n)}").fetchall()
        except Exception:
            continue
        for col, typ in [(r[0], str(r[1])) for r in desc]:
            targets.append((b, col, typ))
    used = {(e.src, e.src_col.strip('"`[]').lower()) for e in lineage_result.edges} | {(s_, c_.lower()) for s_, c_, *_ in lineage_result.indirect}
    unused = [(b, c) for b, c, _ in targets if not any((t, c.lower()) in used for t in same[project.nodes[b].relation.lower()])]
    if only_used:      # faster, but hides false negatives: columns the lineage thinks nobody reads are not tested
        targets = [t for t in targets if any((x, t[1].lower()) in used for x in same[project.nodes[t[0]].relation.lower()])]
    if only_sources:      # `name` (all columns) or `name.column`
        wanted = set(only_sources)
        targets = [t for t in targets if project.nodes[t[0]].name in wanted or f"{project.nodes[t[0]].name}.{t[1]}" in wanted]
    if max_columns:
        targets = targets[:max_columns]

    # real values per column name (all sources): "swap for another valid value" passes through filters like `= 'model'`,
    # which `value || '_x'` never passes
    vals_by_col: dict[str, list[str]] = {}
    for b in bases:
        try:
            for cname, ctype, *_ in con.execute(f"describe select * from {_rel(project.nodes[b])}").fetchall():
                if str(ctype).upper().startswith("VARCHAR"):
                    got = [r[0] for r in con.execute(f"select {_q(cname)} from {_rel(project.nodes[b])} where {_q(cname)} is not null "
                                                     f"group by 1 order by count(*) desc limit 8").fetchall()]
                    for v in got:
                        if v not in vals_by_col.setdefault(cname.lower(), []) and len(vals_by_col[cname.lower()]) < 12:
                            vals_by_col[cname.lower()].append(v)
        except Exception:
            pass

    rel_of = {u: _rel(project.nodes[u]).lower() for u in models}
    code_of = {u: (project.nodes[u].compiled_code or "").lower() for u in models}
    affected_cache: dict[str, list[str]] = {}

    def affected_by(b: str) -> list[str]:
        """Models that can see a perturbation in `b` (in topological order); the others are not rebuilt."""
        key = project.nodes[b].relation.lower()
        if key not in affected_cache:
            rels = {_rel(project.nodes[t]).lower() for t in same[key]}
            uids = set(same[key])
            out = []
            for u in models:
                if u in uids:
                    continue
                if any(d in uids for d in project.nodes[u].depends_on) or any(r in code_of[u] for r in rels):
                    out.append(u)
                    uids.add(u)
                    rels.add(rel_of[u])
            affected_cache[key] = out
        return affected_cache[key]

    tp = fn = fp_susp = indet = explained_n = 0
    mtp = mfp = mfn = 0
    mset = set(models)
    rows_out, fn_list, fp_list, skipped, all_null = [], [], [], [], []
    for i, (b, col, typ) in enumerate(targets):
        node = project.nodes[b]
        rel, cq = _rel(node), _q(col)
        perturbs = {"null": f"update {rel} set {cq} = NULL"}
        tu = typ.upper()
        if NUMERIC.match(typ):
            perturbs["shift"] = f"update {rel} set {cq} = {cq} + 1000003"
            perturbs["reverse"] = f"update {rel} set {cq} = -{cq}"               # inverte a ordem relativa (revela ORDER BY / janelas)
        elif tu.startswith("DATE") or tu.startswith("TIMESTAMP"):
            perturbs["shift"] = f"update {rel} set {cq} = {cq} + INTERVAL 1 DAY"
            perturbs["reverse"] = f"update {rel} set {cq} = DATE '2100-01-01' - ({cq}::DATE - DATE '1970-01-01')"
        elif tu.startswith("BOOL"):
            perturbs["shift"] = f"update {rel} set {cq} = not {cq}"
        else:
            perturbs["shift"] = f"update {rel} set {cq} = {cq} || '_x'"
            perturbs["reverse"] = f"update {rel} set {cq} = reverse(cast({cq} as varchar))"
            if tu.startswith("VARCHAR"):
                own = {r[0] for r in con.execute(f"select distinct {cq} from {rel} limit 50").fetchall()}
                for i_, v in enumerate([v for v in vals_by_col.get(col.lower(), []) if v not in own][:3]):
                    perturbs[f"swap{i_}"] = "update " + rel + " set " + cq + " = '" + v.replace("'", "''") + "' where " + cq + " is not null"
        try:       # a column without any value: every perturbation is a no-op (NULL+1 = NULL), so it cannot be tested
            if con.execute(f"select count({cq}) from {rel}").fetchone()[0] == 0:
                all_null.append(f"{node.name}.{col}")
                continue
        except Exception:
            pass
        aff = affected_by(b)
        applied = 0
        row_changed: set[str] = set()
        changed: dict[tuple[str, str], bool] = {}
        stable_rows: dict[str, bool] = {}
        for pname, sql in perturbs.items():
            con.execute("begin")
            try:
                con.execute(sql)
            except Exception:          # perturbation not applicable (type/object): never counts as a "hit" or a "false positive"
                con.execute("rollback")
                continue
            applied += 1
            try:
                rebuild(aff)
                sig = signature(aff)
            except Exception:
                con.execute("rollback")
                continue
            con.execute("rollback")
            for u in models:
                if u not in sig:               # does not depend on the perturbed source: same output by construction
                    stable_rows[u] = True
                    continue
                brows, bh = base_sig[u]
                prows, ph = sig[u]
                if u in unstable_rows:
                    continue                                   # non-deterministic row count: no verdict
                if prows != brows:
                    row_changed.add(u)                         # rows changed: affected (indirect); out of the column-by-column metric
                    continue
                stable_rows[u] = True
                for c, h in ph.items():
                    if (u, c) not in unstable and bh.get(c) != h:
                        changed[(u, c)] = True
        if not applied:
            skipped.append(f"{node.name}.{col}")
            continue
        dyn = set(changed)
        twins = same[node.relation.lower()]
        stat_direct = {(u, c) for t in twins for (u, c) in static_closure(lineage_result.edges, (t, col.lower())) if u in mset}
        stat_all = {(u, c) for t in twins for (u, c) in static_closure(lineage_result.edges, (t, col.lower()), lineage_result.indirect, mcols) if u in mset}
        dyn_models, stat_models = {u for u, _ in dyn} | row_changed, {u for u, _ in stat_all}
        # column by column, direct lineage only
        d_tp = dyn & stat_direct
        d_susp = {(u, c) for (u, c) in stat_direct if stable_rows.get(u)} - dyn
        # what changed and direct lineage does not cover: explained by indirect dependency (model ∈ indirect closure) or truly lost
        d_miss = dyn - stat_direct
        explained = {k for k in d_miss if k[0] in stat_models}
        lost = d_miss - explained
        # model level (direct ∪ indirect)
        m_tp = dyn_models & stat_models
        m_susp = {u for u in stat_models if stable_rows.get(u)} - dyn_models
        m_miss = dyn_models - stat_models
        tp += len(d_tp); fp_susp += len(d_susp); fn += len(lost); explained_n += len(explained)
        mtp += len(m_tp); mfp += len(m_susp); mfn += len(m_miss)
        indet += len({(u, c) for (u, c) in stat_direct if not stable_rows.get(u)})
        name = lambda k: f"{project.nodes[k[0]].name}.{k[1]}"
        rows_out.append({"source": f"{node.name}.{col}", "static_direct": len(stat_direct), "static_with_indirect": len(stat_all), "dynamic": len(dyn),
                         "tp": len(d_tp), "fn_lost": len(lost), "explained_by_indirect": len(explained), "fp_suspect": len(d_susp),
                         "model_tp": len(m_tp), "model_fp_suspect": len(m_susp), "model_fn": len(m_miss)})
        fn_list += [f"{node.name}.{col} → {name(k)}" for k in sorted(lost)]
        fp_list += [f"{node.name}.{col} → {name(k)}" for k in sorted(d_susp)]
        if progress:
            progress(i + 1, len(targets), f"{node.name}.{col}")
    con.close()
    rate = lambda a, b: (a / (a + b)) if a + b else None
    prec, rec = rate(tp, fp_susp), rate(tp, fn + explained_n)       # recall direto: o que mudou e a direta cobre
    mprec, mrec = rate(mtp, mfp), rate(mtp, mfn)
    return {"models": len(models), "unbuildable": {project.nodes[u].name: r for u, r in unbuildable.items()}, "root_models_as_sources": [project.nodes[u].name for u in roots], "sampled_sources": sampled, "nondeterministic_columns": len(unstable), "perturbation_not_applicable": skipped, "untestable_all_null": all_null, "source_columns_tested": len(targets), "source_columns_unused": len(unused), "seconds": round(time.time() - t0, 1),
            "direct": {"tp": tp, "fp_suspect": fp_susp, "precision_lower_bound": prec, "recall_of_value_changes": rec,
                       "changed_but_only_indirect": explained_n, "lost": fn},
            "model_level": {"tp": mtp, "fp_suspect": mfp, "fn": mfn, "precision_lower_bound": mprec, "recall": mrec},
            "indeterminate": indet, "per_source": rows_out, "false_negatives": fn_list, "suspect_false_positives": fp_list}
