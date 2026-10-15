import json
from pathlib import Path

from intentflow import dbt_loader, lineage, lineage_api

ROOT = Path(__file__).resolve().parent.parent


def _with_exposure(tmp_path, src):
    src = Path(src) if (Path(src) / "manifest.json").exists() else Path(src) / "target"
    man = json.loads((src / "manifest.json").read_text())
    models = [u for u, n in man["nodes"].items() if n["resource_type"] == "model"]
    leaf = next(u for u in models if u.endswith("customer_ltv")) if any(u.endswith("customer_ltv") for u in models) else models[-1]
    man["exposures"] = {"exposure.shop.revenue_dashboard": {
        "unique_id": "exposure.shop.revenue_dashboard", "name": "revenue_dashboard", "resource_type": "exposure", "type": "dashboard",
        "url": "https://bi.example.com/d/1", "maturity": "high", "owner": {"name": "finance", "email": "f@x.com"},
        "depends_on": {"nodes": [leaf]}, "original_file_path": "models/exposures.yml"}}
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    if (src / "catalog.json").exists():
        (tmp_path / "catalog.json").write_text((src / "catalog.json").read_text())
    return leaf


def test_exposures_become_nodes_and_receive_impact(tmp_path, shop_target):
    leaf = _with_exposure(tmp_path, shop_target)
    p = dbt_loader.load(tmp_path)
    x = p.nodes["exposure.shop.revenue_dashboard"]
    assert x.resource_type == "exposure" and x.meta["owner"] == "finance" and x.depends_on == [leaf]
    res = lineage.build(p)                                       # exposures do not break the lineage
    root = next(u for u, n in p.nodes.items() if n.resource_type in ("seed", "source", "model") and leaf in p.downstream(u))
    imp = lineage_api.impact(p, res, p.nodes[root].name)
    assert any(r["name"] == "revenue_dashboard" for r in imp["affected"])
    assert "finance" in imp["owners"]


def test_metrics_and_semantic_models_from_real_dbt_parse(tmp_path):
    """Real `dbt parse` from dbt 1.12 over the sample project + semantic model + metric + time spine."""
    import shutil, subprocess, sys
    proj = tmp_path / "shop"
    shutil.copytree(ROOT / "examples/dbt_shop", proj, ignore=shutil.ignore_patterns("target", "logs"))
    (proj / "models/metricflow_time_spine.sql").write_text(
        "{{ config(materialized='table') }}\nselect cast(d as date) as date_day from generate_series(date '2020-01-01', date '2030-01-01', interval 1 day) as t(d)\n")
    (proj / "models/semantic.yml").write_text("""version: 2
models:
  - name: metricflow_time_spine
    time_spine: {standard_granularity_column: date_day}
    columns: [{name: date_day, granularity: day}]
semantic_models:
  - name: orders_semantic
    model: ref('orders')
    defaults: {agg_time_dimension: order_date}
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions: [{name: order_date, type: time, type_params: {time_granularity: day}}]
    measures: [{name: order_count, agg: count, expr: order_id}]
metrics:
  - name: total_orders
    label: Total de pedidos
    type: simple
    type_params: {measure: order_count}
    meta: {owner: financas@exemplo.com}
""")
    r = subprocess.run([str(Path(sys.executable).parent / "dbt"), "parse", "--profiles-dir", "."], cwd=proj, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-800:]
    p = dbt_loader.load(proj / "target")
    m = p.nodes["metric.shop.total_orders"]
    assert m.resource_type == "metric" and m.meta["owner"] == "financas@exemplo.com" and m.meta["label"] == "Total de pedidos"
    assert m.depends_on == ["semantic_model.shop.orders_semantic"] and p.nodes["semantic_model.shop.orders_semantic"].depends_on == ["model.shop.orders"]
    assert "metric.shop.total_orders" in p.downstream("model.shop.stg_orders")                  # reached from a staging model
    res = lineage.build(p)
    imp = lineage_api.impact(p, res, "stg_orders", "order_id")
    kinds = {r["name"]: r.get("kind") for r in imp["affected"]}
    assert kinds.get("total_orders") == "metric" and kinds.get("orders_semantic") == "semantic_model"
    assert "financas@exemplo.com" in imp["owners"]


def test_package_models_are_not_scanned_by_default(shop_target, tmp_path):
    """Models from installed packages are not actionable by the team: left out unless `scan.include_packages` is on."""
    import json, shutil
    from intentflow import runner
    from intentflow.store import Store
    src = Path(shop_target) if (Path(shop_target) / "manifest.json").exists() else Path(shop_target) / "target"
    d = tmp_path / "t"
    shutil.copytree(src, d)
    man = json.loads((d / "manifest.json").read_text())
    pkg = {}
    for uid, n in man["nodes"].items():
        if n["resource_type"] == "model" and n["name"].startswith("stg_"):
            new = uid.replace("model.shop.", "model.some_package.")
            n["unique_id"], n["package_name"] = new, "some_package"
            pkg[uid] = new
    man["nodes"] = {pkg.get(u, u): n for u, n in man["nodes"].items()}
    for n in man["nodes"].values():
        n["depends_on"]["nodes"] = [pkg.get(x, x) for x in n.get("depends_on", {}).get("nodes", [])]
    (d / "manifest.json").write_text(json.dumps(man))
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("dbt.target", str(d))
    p = dbt_loader.load(d)
    assert p.root_package == "shop" and not p.in_root_package(next(iter(pkg.values()))) and p.in_root_package("model.shop.orders")
    res = runner.run_check(st)
    known = lambda f: [n for n in f.nodes if n in p.nodes]
    assert res["findings"] and all(not known(f) or any(p.in_root_package(n) for n in known(f)) for f in res["findings"])
    assert not any(known(f) and not any(p.in_root_package(n) for n in known(f)) for f in res["findings"])
    st.set_setting("scan.include_packages", True)
    res2 = runner.run_check(st)
    assert len(res2["findings"]) > len(res["findings"])
    assert any(known(f) and not any(p.in_root_package(n) for n in known(f)) for f in res2["findings"])     # aparecem quando ligado
