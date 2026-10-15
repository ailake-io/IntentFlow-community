"""Data diff between two relations (base version × changed version), run in the warehouse itself.

No data leaves the warehouse: only counts, statistics and a small sample of changed rows.
"""
from __future__ import annotations

import math

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from .warehouses import Warehouse
from .i18n import tr


def q(c: str) -> str:      # ANSI default; `diff_relations` uses the engine's quote when it differs
    return '"' + c.replace('"', '""') + '"'


def _relation(value: str, dialect: str) -> str:
    """Accept a table identifier, never an arbitrary FROM clause or extra statement."""
    try:
        trees = sqlglot.parse("select * from " + value, read=dialect)
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        raise ValueError(tr("invalid relation: pass only the qualified table name")) from None
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise ValueError(tr("invalid relation: SQL statements are not allowed"))
    tree = trees[0]
    if any(v for k, v in tree.args.items() if k not in ("expressions", "from_", "from")):
        raise ValueError(tr("invalid relation: SQL clauses are not allowed"))
    frm = tree.args.get("from_") or tree.args.get("from")
    table = frm.this if frm is not None else None
    if (not isinstance(table, exp.Table)
            or any(v for k, v in table.args.items() if k not in ("this", "db", "catalog"))
            or not table.parts or not all(isinstance(p, exp.Identifier) for p in table.parts)):
        raise ValueError(tr("invalid relation: functions, aliases and subqueries are not allowed"))
    table = normalize_identifiers(table, dialect=dialect)
    clean = exp.Table(**{k: exp.Identifier(this=v.name, quoted=True)
                         for k, v in table.args.items() if k in ("this", "db", "catalog") and v is not None})
    return clean.sql(dialect=dialect)


def diff_relations(wh: Warehouse, base: str, head: str, key: list[str], sample: int = 5, columns: list[str] | None = None,
                   sample_pct: float | None = None) -> dict:
    """`sample_pct` (0 < p < 100): compare only ~p % of the keys (hash of the key, the same in both relations). For huge tables;
    the numbers become the sample's and `out["sampled"]` carries the estimate for the total."""
    if not key or len(set(key)) != len(key) or not all(isinstance(k, str) and k for k in key):
        raise ValueError(tr("pass a non-empty key without repeated columns"))
    if not isinstance(sample, int) or not 0 <= sample <= 1000:
        raise ValueError(tr("sample must be between 0 and 1000"))
    if sample_pct is not None and (not math.isfinite(sample_pct) or not 0 < sample_pct <= 100):
        raise ValueError(tr("sample_pct must be greater than 0 and at most 100"))
    base, head = _relation(base, wh.dialect), _relation(head, wh.dialect)
    q = wh.quote                                   # noqa: F811 — aspas/crases e IS DISTINCT FROM variam por motor
    bcols, hcols = dict(wh.describe(base)), dict(wh.describe(head))
    shared = [c for c in (columns or list(bcols)) if c in hcols and c not in key]
    for k in key:
        if k not in bcols or k not in hcols:
            raise ValueError(tr("key '{key}' does not exist in both relations", key=k))
    on = " and ".join(f"b.{q(k)} = h.{q(k)}" for k in key)
    out: dict = {"base": base, "head": head, "key": key}
    pred = None
    if sample_pct is not None and 0 < sample_pct < 100:
        pred = wh.sample_pred([q(k) for k in key], sample_pct)
        out["sampled"] = {"pct": sample_pct}

    def S(rel: str, alias: str) -> str:      # the relation, filtered by the key hash when sampling
        return f"(select * from {rel} where {pred}) {alias}" if pred else f"{rel} {alias}"
    out["schema"] = {"added": sorted(set(hcols) - set(bcols)), "removed": sorted(set(bcols) - set(hcols)),
                     "type_changed": [(c, bcols[c], hcols[c]) for c in bcols if c in hcols and str(bcols[c]) != str(hcols[c])]}
    kexpr = ", ".join(q(k) for k in key)
    # Scans (independent of the number of columns): 1 per relation for count+statistics (columns in batches), 1 per relation
    # for duplicated keys, 1 aggregated join per batch of columns, 2 anti-joins. Before: ~11 + 3 per column.
    batch = 40
    chunks = [shared[i:i + batch] for i in range(0, len(shared), batch)] or [[]]
    rows_n, col_stats = {}, {}
    for side, rel in (("base", base), ("head", head)):
        n_rows = None
        for ch in chunks:
            sel = "count(*)" + "".join(f", count({q(c)}), min({q(c)}), max({q(c)})" for c in ch)
            try:
                r = wh.rows(f"select {sel} from {S(rel, 't')}")[0]
            except Exception:           # type without min/max (JSON, array...): fall back to column by column, skipping the unsupported ones
                r = None
                one = {}
                for c in ch:
                    try:
                        x = wh.rows(f"select count(*), count({q(c)}), min({q(c)}), max({q(c)}) from {S(rel, 't')}")[0]
                        n_rows = int(x[0]) if n_rows is None else n_rows
                        one[c] = (int(x[0]) - int(x[1]), x[2], x[3])
                    except Exception:
                        continue
                col_stats.setdefault(side, {}).update(one)
                if n_rows is None:
                    n_rows = int(wh.scalar(f"select count(*) from {S(rel, 't')}"))
                continue
            n_rows = int(r[0]) if n_rows is None else n_rows
            for i, c in enumerate(ch):
                cnt, mn, mx = r[1 + 3 * i: 4 + 3 * i]
                col_stats.setdefault(side, {})[c] = (int(r[0]) - int(cnt), mn, mx)
        rows_n[side] = n_rows or 0
    out["rows"] = rows_n
    out["duplicate_keys"] = {
        "base": int(wh.scalar(f"select count(*) from (select {kexpr} from {S(base, 's')} group by {kexpr} having count(*) > 1) t")),
        "head": int(wh.scalar(f"select count(*) from (select {kexpr} from {S(head, 's')} group by {kexpr} having count(*) > 1) t")),
    }
    k0 = key[0]      # anti-join: portable (ClickHouse does not accept a correlated NOT EXISTS)
    out["only_in_base"] = int(wh.scalar(f"select count(*) from {S(base, 'b')} left join {S(head, 'h')} on {on} where h.{q(k0)} is null"))
    out["only_in_head"] = int(wh.scalar(f"select count(*) from {S(head, 'h')} left join {S(base, 'b')} on {on} where b.{q(k0)} is null"))
    changed, total_changed_rows = {}, 0
    flag = lambda c: f"sum(case when {wh.differs(f'b.{q(c)}', f'h.{q(c)}')} then 1 else 0 end)"
    for i, ch in enumerate(chunks):
        if not ch:
            continue
        extra = ""
        if i == 0:       # rows with any difference: a single expression, in the same scan as the first batch
            any_diff = " or ".join(wh.differs(f"b.{q(c)}", f"h.{q(c)}") for c in shared)
            extra = f", sum(case when {any_diff} then 1 else 0 end)"
        r = wh.rows(f"select {', '.join(flag(c) for c in ch)}{extra} from {S(base, 'b')} join {S(head, 'h')} on {on}")[0]
        for c, n in zip(ch, r):
            if n:
                changed[c] = int(n)
        if i == 0:
            total_changed_rows = int(r[len(ch)] or 0)
    out["changed_rows"] = total_changed_rows
    out["changed_by_column"] = changed
    samples = []
    if changed and sample:
        cols = list(changed)[:4]
        sel = ", ".join([f"b.{q(k)}" for k in key] + [f"b.{q(c)} as {q('base_' + c)}, h.{q(c)} as {q('head_' + c)}" for c in cols])
        cond = " or ".join(wh.differs(f"b.{q(c)}", f"h.{q(c)}") for c in cols)
        names = key + [x for c in cols for x in (f"base_{c}", f"head_{c}")]
        for r in wh.rows(f"select {sel} from {S(base, 'b')} join {S(head, 'h')} on {on} where {cond} order by 1 limit {int(sample)}"):
            samples.append(dict(zip(names, r)))
    out["samples"] = samples
    stats = {}
    for c in shared:
        sb, sh = col_stats.get("base", {}).get(c), col_stats.get("head", {}).get(c)
        if sb is None or sh is None:
            continue
        if tuple(sb) != tuple(sh):
            stats[c] = {"base": {"nulls": sb[0], "min": str(sb[1]), "max": str(sb[2])}, "head": {"nulls": sh[0], "min": str(sh[1]), "max": str(sh[2])}}
    out["column_stats_changed"] = stats
    if pred:
        f = 100.0 / sample_pct
        out["sampled"].update({k: int(out[k] * f) for k in ("only_in_base", "only_in_head", "changed_rows")} | {"rows": {k: int(v * f) for k, v in rows_n.items()}})
    out["identical"] = not (out["schema"]["added"] or out["schema"]["removed"] or out["schema"]["type_changed"] or out["only_in_base"] or out["only_in_head"] or total_changed_rows
                            or out["rows"]["base"] != out["rows"]["head"])
    return out


def to_markdown(d: dict) -> str:
    L = ["### " + tr("Data diff: `{base}` → `{head}`", base=d["base"], head=d["head"]), ""]
    if d["identical"]:
        return "\n".join(L + [tr("No differences in the sample ({pct} % of the keys).", pct=d["sampled"]["pct"]) if d.get("sampled") else tr("No differences.")])
    if d.get("sampled"):
        sm = d["sampled"]
        L.append("- " + tr("**Sample of {pct} % of the keys** (hash); estimate for the total: changed ≈ {changed}, only in base ≈ {base}, only in new ≈ {head}",
                           pct=sm["pct"], changed=sm["changed_rows"], base=sm["only_in_base"], head=sm["only_in_head"]))
    L.append("- " + tr("Rows: {base} → {head}  |  only in base: {only_base}  |  only in new: {only_head}", base=d["rows"]["base"], head=d["rows"]["head"],
                       only_base=d["only_in_base"], only_head=d["only_in_head"]))
    L.append("- " + tr("Changed rows: {n}  |  duplicated keys (base/new): {base}/{head}", n=d["changed_rows"], base=d["duplicate_keys"]["base"],
                       head=d["duplicate_keys"]["head"]))
    s = d["schema"]
    if s["added"] or s["removed"] or s["type_changed"]:
        L.append("- " + tr("Schema: +{added} −{removed} type: {changed}", added=s["added"], removed=s["removed"], changed=[(c, a, b) for c, a, b in s["type_changed"]]))
    if d["changed_by_column"]:
        L += ["", tr("| column | changed rows |"), "|---|---:|"] + [f"| {c} | {n} |" for c, n in d["changed_by_column"].items()]
    for smp in d["samples"]:
        L.append("  - " + ", ".join(f"{k}={v}" for k, v in smp.items()))
    return "\n".join(L)
