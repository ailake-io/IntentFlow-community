from intentflow import dbt_loader, lineage


def build(shop):
    p = dbt_loader.load(shop / "target", shop)
    return p, lineage.build(p)


def uid(p, name):
    return next(u for u, n in p.nodes.items() if n.name == name and n.resource_type == "model")


def test_loads_nodes_tests_and_columns(shop_target):
    p, _ = build(shop_target)
    assert {"stg_customers", "customers", "daily_revenue"} <= {n.name for n in p.nodes.values()}
    assert p.nodes[uid(p, "customers")].columns["lifetime_value"].type
    assert any(t["name"] == "unique" and t["column"] == "customer_id" for t in p.nodes[uid(p, "customers")].tests)


def test_column_lineage_through_ctes_joins_and_star(shop_target):
    p, lin = build(shop_target)
    c = uid(p, "customers")
    ltv = {(p.nodes[e.src].name, e.src_col, e.kind) for e in lin.upstream_of(c, "lifetime_value")}
    assert ("stg_payments", "amount", "derived") in ltv          # sum(...) via CTE
    email = {(p.nodes[e.src].name, e.src_col, e.kind) for e in lin.upstream_of(c, "email")}
    assert email == {("stg_customers", "email", "direct")}
    # customer_contacts reads customers
    cc = uid(p, "customer_contacts")
    assert {(p.nodes[e.src].name, e.src_col) for e in lin.upstream_of(cc, "email")} == {("customers", "email")}


def test_everything_resolves_on_the_sample(shop_target):
    _, lin = build(shop_target)
    r = lin.precision_report
    assert r["unresolved"] == 0, lin.unresolved
    assert r["resolved_ratio"] == 1.0


def test_sql_quality_flags_planted_problems(shop_target):
    from intentflow.checks import Context, run_checks
    p, lin = build(shop_target)
    f = run_checks(Context(project=p, lineage=lin), only=["SQL"])
    ids = {(x.check, p.nodes[x.nodes[0]].name) for x in f}
    assert ("SQL001", "customers") in ids          # select * em CTEs
    assert ("SQL002", "customers") not in ids      # joins have ON
