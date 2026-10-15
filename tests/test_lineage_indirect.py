from intentflow import lineage, lineage_api
from intentflow.checks import Context, run_checks
from intentflow.dbt_loader import Col, DbtNode, Project


def _proj(extra_cols=True):
    cols = lambda *names: {n: Col(n, "int") for n in names}
    return Project(adapter="duckdb", nodes={
        "a": DbtNode("a", "customers", "seed", relation='"d"."s"."customers"', columns=cols("id", "name")),
        "b": DbtNode("b", "orders", "seed", relation='"d"."s"."orders"', columns=cols("id", "customer_id", "amount", "status")),
        "m": DbtNode("m", "customer_totals", "model", relation='"d"."s"."customer_totals"', depends_on=["a", "b"], columns=cols("name", "total"),
                     compiled_code='select c.name, sum(o.amount) as total from d.s.customers c join d.s.orders o on o.customer_id = c.id '
                                   "where o.status <> 'cancelled' group by c.name"),
        "x": DbtNode("x", "report", "model", relation='"d"."s"."report"', depends_on=["m"], columns=cols("name"),
                     compiled_code="select name from d.s.customer_totals"),
    })


def test_join_filter_and_group_columns_are_indirect_dependencies():
    r = lineage.build(_proj())
    ind = {(s, c, d, v) for s, c, d, v in r.indirect}
    assert ("a", "id", "m", "join") in ind and ("b", "customer_id", "m", "join") in ind
    assert ("b", "status", "m", "filter") in ind and ("a", "name", "m", "group") in ind
    assert not any(s == "b" and c == "amount" for s, c, *_ in ind)                 # `amount` is only read as a value (direct)
    assert r.indirect_of("b", "status") == [("m", "filter")]


def test_impact_reaches_models_through_join_key_only_and_propagates():
    p = _proj()
    imp = lineage_api.impact(p, lineage.build(p), "orders", "status")
    by = {x["name"]: x for x in imp["affected"]}
    assert by["customer_totals"]["indirect_via"] == ["filter"] and "report" in by         # and what reads the affected model
    assert lineage_api.impact(p, lineage.build(p), "orders", "amount")["affected"][0]["name"] == "customer_totals"


def test_removing_a_join_key_is_critical_even_though_it_is_never_projected():
    base = _proj()
    head = _proj()
    del head.nodes["a"].columns["id"]
    head.nodes["a"].columns["customer_id"] = Col("customer_id", "int")
    ctx = Context(project=head, lineage=lineage.build(head), base_project=base, base_lineage=lineage.build(base))
    f = [x for x in run_checks(ctx, only=["SCH001"]) if x.evidence["column"] == "id"]
    assert f and f[0].severity == "critical" and "customer_totals" in [base.nodes[u].name for u in f[0].nodes if u in base.nodes]
    assert f[0].evidence["indirect_consumers"] == [("customer_totals", "join")]


def test_output_columns_are_inferred_without_yaml_or_catalog():
    p = _proj()
    p.nodes["m"].columns = {}                       # nothing declared: the columns come from the compiled SELECT
    r = lineage.build(p)
    assert any(e.dst == "m" and e.dst_col == "total" and e.src == "b" and e.src_col == "amount" for e in r.edges)


def test_parent_without_column_edges_falls_back_to_model_dependency():
    p = _proj()
    p.nodes["x"].compiled_code = "select 1 as name"   # generated SQL that never touches the parent: it still depends on it
    r = lineage.build(p)
    assert ("m", "name", "x", "opaque") not in r.indirect and ("m", "*", "x", "opaque") in r.indirect
    assert ("x", "opaque") in r.indirect_of("m", "total")


def test_filter_on_union_cte_column_is_an_indirect_dependency():
    # regression: sqlglot 30 has no `union_scopes`; provenance through UNION ALL was silently empty
    p = _proj()
    p.nodes["x"].compiled_code = ("with u as (select name from d.s.customer_totals union all select name from d.s.customer_totals), "
                                  "f as (select name from u where name <> 'x') select name from f")
    r = lineage.build(p)
    assert ("m", "name", "x", "filter") in r.indirect


def test_parallel_build_matches_serial_and_uses_cache():
    p = _proj()
    for i in range(12):        # enough models to trigger the pool (>= 2 x workers)
        p.nodes[f"g{i}"] = DbtNode(f"g{i}", f"g{i}", "model", relation=f'"d"."s"."g{i}"', depends_on=["m"], columns={"name": Col("name", "int")},
                                   compiled_code="select name from d.s.customer_totals")
    ser, par = lineage.build(p, workers=1), lineage.build(p, workers=4)
    key = lambda r: (sorted(map(repr, r.edges)), sorted(map(repr, r.indirect)), sorted(map(repr, r.unresolved)), r.columns_total)
    assert key(ser) == key(par)

    class C(dict):
        def set(self, k, v):
            self[k] = v
    c = C()
    lineage.build(p, cache=c, workers=4)
    assert lineage.build(p, cache=c, workers=4).cache_hits == 14       # the 14 models, all from the cache


def test_only_changed_models_are_recomputed_between_base_and_head():
    class C(dict):
        def set(self, k, v):
            self[k] = v
    c = C()
    base = _proj()
    assert lineage.build(base, cache=c).cache_hits == 0
    head = _proj()
    head.nodes["x"].compiled_code = "select name as nm from d.s.customer_totals"          # only the `report` model changes
    r = lineage.build(head, cache=c)
    assert r.cache_hits == 1 and any(e.dst == "x" and e.dst_col == "nm" for e in r.edges)  # `customer_totals` veio do cache; `report` foi recalculado


def test_unguarded_script_does_not_cascade_processes(tmp_path):
    """A script without `if __name__ == "__main__"`: spawn re-imports the module; the child must not open another pool (cascade)."""
    import subprocess
    import sys
    script = tmp_path / "unguarded.py"
    script.write_text(
        "from intentflow import lineage\n"
        "from intentflow.dbt_loader import Col, DbtNode, Project\n"
        "n = {f'm{i}': DbtNode(f'm{i}', f'm{i}', 'model', relation=f'\"d\".\"s\".\"m{i}\"', columns={'a': Col('a', 'int')},\n"
        "     compiled_code='select a from d.s.t') for i in range(20)}\n"
        "n['t'] = DbtNode('t', 't', 'seed', relation='\"d\".\"s\".\"t\"', columns={'a': Col('a', 'int')})\n"
        "r = lineage.build(Project(adapter='duckdb', nodes=n), workers=2)\n"
        "print('OK', len(r.edges))\n")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and 1 <= out.stdout.count("OK") <= 3, out.stderr[-400:]      # parent + 2 workers, never more
