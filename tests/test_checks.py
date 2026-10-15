import shutil, subprocess, sys
from pathlib import Path

import pytest

from intentflow import dbt_loader, lineage
from intentflow.checks import Context, run_checks
from intentflow.store import Store

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"


def ctx_for(shop, **kw):
    p = dbt_loader.load(shop / "target", shop)
    return Context(project=p, lineage=lineage.build(p), **kw)


def by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f.check, []).append(f)
    return out


def test_dbt_quality_checks(shop_target):
    c = ctx_for(shop_target, policies={})
    r = by_check(run_checks(c, only=["DBT"]))
    names = lambda k: {c.project.nodes[x.nodes[0]].name for x in r.get(k, [])}   # noqa: E731
    assert "stg_payments" in names("DBT001")             # consumida e sem teste unique
    assert "customers" not in names("DBT001")
    assert names("DBT002") == {"daily_revenue"}          # incremental sem unique_key
    assert {"daily_revenue", "customer_contacts"} <= names("DBT003")   # sem owner
    pii = {(c.project.nodes[x.nodes[0]].name, x.title.split(":")[0].split(".")[1]) for x in r["DBT004"]}
    assert ("customers", "email") in pii and ("customer_contacts", "email") in pii


def test_pii_policy_allows_models(shop_target):
    c = ctx_for(shop_target, policies={"pii_allowed_models": ["customers", "customer_contacts"]})
    assert "DBT004" not in by_check(run_checks(c, only=["DBT004"]))


def test_sql_quality_on_sample(shop_target):
    c = ctx_for(shop_target)
    r = by_check(run_checks(c, only=["SQL"]))
    assert any(c.project.nodes[x.nodes[0]].name == "customers" for x in r["SQL001"])


def test_orchestration_lint():
    c = Context(airflow_dags_dir=str(EX / "airflow_dags"), kestra_flows_dir=str(EX / "kestra_flows"))
    r = run_checks(c, only=["ORC"])
    ids = {(f.check, f.nodes[0].split("/")[-1]) for f in r}
    for want in [("AF001", "nightly_shop.py"), ("AF002", "nightly_shop.py"), ("AF003", "nightly_shop.py"), ("AF004", "nightly_shop.py"),
                 ("AF006", "nightly_shop.py"), ("AF007", "nightly_shop.py"), ("KS001", "company.shop.shop_daily"),
                 ("KS004", "company.shop.shop_daily"), ("KS005", "company.shop.shop_daily"), ("KS006", "company.shop.shop_daily")]:
        assert want in ids, (want, sorted(ids))
    assert not [f for f in r if f.nodes[0].endswith("healthy.py") and f.severity in ("high", "critical", "medium")]


def test_orchestration_lint_http_flow_without_retry_and_timeout():
    r = run_checks(Context(kestra_flows_dir=str(EX / "kestra_flows")), only=["ORC"])
    ids = {(f.check, f.nodes[0]) for f in r}
    assert ("KS002", "company.shop.http_ingest") in ids and ("KS003", "company.shop.http_ingest") in ids
    assert ("KS001", "company.shop.http_ingest") not in ids         # tem bloco errors


def test_memory_high_thresholds():
    from intentflow.monitoring import Sample
    s = [Sample(1.0, "docker", "a", "memory_bytes", 7.5e9, 10e9), Sample(1.0, "docker", "b", "memory_bytes", 9.5e9, 10e9),
         Sample(1.0, "docker", "c", "memory_bytes", 5e9, 10e9)]
    r = {x.nodes[0]: x.severity for x in run_checks(Context(samples=s), only=["CLU001"])}
    assert r == {"docker:a": "medium", "docker:b": "critical"}


def test_probes_duplicates_and_volume(shop_target):
    from intentflow.warehouses import DuckDBWarehouse
    import duckdb
    db = shop_target / "target" / "shop.duckdb"
    con = duckdb.connect(str(db))
    con.execute("insert into main.stg_customers_dup select 1") if False else None
    con.close()
    wh = DuckDBWarehouse(str(db))
    st = Store()
    c = ctx_for(shop_target, warehouse=wh, store=st)
    assert run_checks(c, only=["PRB001"]) == []                 # unique keys
    # volume history: 3 observations at ~100 rows; the table now has 4 → sharp drop
    uid = next(u for u, n in c.project.nodes.items() if n.name == "raw_orders")
    for i in range(3):
        st.add_history(f"rows:{uid}", 100 + i, 100)
    f = run_checks(ctx_for(shop_target, warehouse=wh, store=st), only=["PRB002"])
    hit = [x for x in f if "raw_orders" in x.title]
    assert hit and hit[0].severity == "high"


def test_cluster_checks_and_exhaustion_forecast():
    from intentflow.monitoring import Sample
    base = 1_000_000
    s = [Sample(base + i * 600, "docker", "airflow-worker", "memory_bytes", 2e9 + i * 3e8, 8e9) for i in range(6)]    # sobe 3e8/10min
    s += [Sample(base, "docker", "db", "oom_killed", 1.0), Sample(base, "warehouse", "snowflake:01a", "spilled_bytes", 12e9),
          Sample(base, "airflow", "default_pool", "queued", 3, 8), Sample(base + 60, "airflow", "default_pool", "queued", 4, 8),
          Sample(base + 120, "airflow", "default_pool", "queued", 2, 8)]
    r = by_check(run_checks(Context(samples=s), only=["CLU"]))
    assert "CLU002" in r and "runs out of memory" in r["CLU002"][0].title
    assert r["CLU003"][0].severity == "critical"
    assert r["CLU004"][0].severity == "high"
    assert "CLU005" in r


def test_run_history_checks():
    from intentflow.orchestrators import Run
    runs = [Run("kestra", "ns.f", str(i), "failed", float(i), float(i) + 5, 5.0) for i in range(3)]
    runs += [Run("airflow", "d", str(i), "success", float(i), float(i) + 10, 10.0) for i in range(8)]
    runs.append(Run("airflow", "d", "9", "success", 20.0, 80.0, 60.0))
    r = by_check(run_checks(Context(runs=runs), only=["RUN"]))
    assert "RUN001" in r and "RUN002" in r


@pytest.fixture(scope="module")
def drift(tmp_path_factory, shop_target):
    """base = sample project; head = column stg_customers.email renamed to email_address."""
    d = tmp_path_factory.mktemp("drift")
    head = d / "head"
    shutil.copytree(shop_target, head, ignore=shutil.ignore_patterns("logs"))
    f = head / "models/staging/stg_customers.sql"
    f.write_text(f.read_text().replace("    email,", "    email as email_address,"))
    dbt = str(Path(sys.executable).parent / "dbt")
    # head cannot build everything (consumers break): only what the catalog needs
    for cmd in (["run", "--select", "stg_customers"], ["docs", "generate", "--select", "stg_customers"]):
        subprocess.run([dbt, *cmd, "--profiles-dir", "."], cwd=head, capture_output=True, text=True)
    return shop_target, head


def test_schema_drift_finds_impact_and_rename(drift):
    base, head = drift
    bp, hp = dbt_loader.load(base / "target", base), dbt_loader.load(head / "target", head)
    ctx = Context(project=hp, lineage=lineage.build(hp), base_project=bp, base_lineage=lineage.build(bp))
    r = run_checks(ctx, only=["SCH"])
    f = next(x for x in r if x.check == "SCH001" and "stg_customers.email" in x.title)
    assert f.severity == "critical" and f.evidence["rename_candidate"] == "email_address"
    assert "customers" in {c[0] for c in f.evidence["consumers"]}
    assert f.fix["targets"] == ["customers"]
    assert {"customers", "customer_contacts"} <= set(f.evidence["downstream_total"])      # cascade beyond the direct consumer
    assert "customer_contacts" in f.detail


def test_lineage_cli_impact_is_transitive(shop_target, capsys):
    from intentflow import cli
    cli.main(["lineage", "--target", str(shop_target / "target"), "--column", "stg_customers.email", "--impact"])
    assert "customer_contacts" in capsys.readouterr().out


def test_store_finding_lifecycle():
    from intentflow.model import Finding
    st = Store()
    f = Finding("DBT001", "high", "x", nodes=["a"])
    assert st.sync_findings([f], {"DBT001"})["new"] == 1
    assert st.sync_findings([f], {"DBT001"})["new"] == 0
    st.ack(f.id, "me", "ok", until=1.0)                      # ack expirado
    st.sync_findings([f], {"DBT001"})
    assert st.findings("open")[0]["id"] == f.id
    assert st.sync_findings([], {"DBT001"})["resolved"] == 1


def test_every_emitted_finding_id_belongs_to_a_registered_check():
    """Regression: AF*/KS*/SCH002/DBTR003 never resolved because the emitted id matched no registered check."""
    import ast
    from pathlib import Path
    from intentflow.checks import REGISTRY, load_all
    load_all()
    known = set(REGISTRY) | {i for c in REGISTRY.values() for i in c.emits}
    emitted = set()
    for p in (Path(__file__).resolve().parent.parent / "src/intentflow/checks").glob("*.py"):
        for c in ast.walk(ast.parse(p.read_text())):
            if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "Finding" and c.args and isinstance(c.args[0], ast.Constant):
                emitted.add(c.args[0].value)
    orphans = {i for i in emitted if not any(i == k or i.startswith(k) for k in known)}
    assert orphans == set(), orphans


def test_secondary_finding_resolves_when_the_problem_disappears(tmp_path):
    from intentflow.model import Finding
    from intentflow.store import Store
    from intentflow.checks import REGISTRY, load_all
    load_all()
    st = Store(str(tmp_path / "s.db"))
    f = Finding("DBTR003", "medium", "12 modelos não rodaram", nodes=["x"])
    st.sync_findings([f], {"DBTR002"} | set(REGISTRY["DBTR002"].emits))
    assert len(st.findings("open")) == 1
    r = st.sync_findings([], {"DBTR002"} | set(REGISTRY["DBTR002"].emits))
    assert r["resolved"] == 1 and st.findings("open") == []


def test_schema_drift_warns_when_head_has_no_catalog(drift, tmp_path):
    base, head = drift
    nocat = tmp_path / "nocat"
    shutil.copytree(head, nocat, ignore=shutil.ignore_patterns("logs"))
    (nocat / "target/catalog.json").unlink()
    bp, hp = dbt_loader.load(base / "target", base), dbt_loader.load(nocat / "target", nocat)
    ctx = Context(project=hp, lineage=lineage.build(hp), base_project=bp, base_lineage=lineage.build(bp))
    r = [x for x in run_checks(ctx, only=["SCH"]) if x.check == "SCH003"]
    assert len(r) == 1 and r[0].evidence["missing_catalog"] == ["head"]


def test_ui_refuses_unreadable_tls_files_with_a_clear_message(tmp_path):
    from intentflow.web import build_server
    bad = tmp_path / "missing.pem"
    with pytest.raises(SystemExit, match="TLS certificate/key"):
        build_server(str(tmp_path / "s.db"), 0, "127.0.0.1", str(bad), str(bad))
