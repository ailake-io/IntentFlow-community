import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from intentflow import business_rules, cli, dbt_loader, dbt_results, preflight, runner
from intentflow.checks import Context, run_checks
from intentflow.store import Store
from test_preflight import artifacts, edit, MODEL, TEST  # noqa: F401
from test_review_regressions import local_api  # noqa: F401


@pytest.fixture
def rule_project(artifacts):
    root, target, base = artifacts
    edit(target, "manifest.json", lambda d: d["nodes"][TEST].update(config={"tags": ["business_rule"], "meta": {"owner": "finance"}}, description="Order identifiers are unique"))
    return dbt_loader.load(target, root), dbt_results.load(target), root, target, base


def test_catalog_fresh_missing_stale_owner_and_cli(rule_project, tmp_path, capsys):
    project, results, root, target, _ = rule_project
    project.test_definitions[TEST]["meta"]["private_note"] = "not part of the public rule catalog"
    rule = business_rules.build(project, results)["rules"][0]
    assert "meta" not in rule
    assert rule["status"] == "pass" and rule["fresh"] and rule["owner"] == "finance"
    assert rule["models"] == ["orders"]
    assert business_rules.build(project, None)["rules"][0]["status"] == "not_run"
    assert business_rules.build(project, results, now=10**11)["rules"][0]["status"] == "stale"
    assert business_rules.build(project, {**results, "generated_at": "bad"})["rules"][0]["status"] == "stale"
    assert cli.main(["--store", str(tmp_path / "rules.db"), "rules", "--target", str(target), "--root", str(root), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["rules"][0]["status"] == "pass"


def test_history_deduplicates_invocations_and_is_bounded(rule_project):
    project, results, *_ = rule_project
    with Store() as store:
        for i in range(25):
            r = {**results, "invocation_id": f"build-{i}"}
            business_rules.record(store, project, r)
            business_rules.record(store, project, r)
        history = business_rules.build(project, results, store)["rules"][0]["history"]
        assert len(history) == 20 and history[0]["invocation_id"] == "build-5"
        assert history[-1]["invocation_id"] == "build-24"


def test_rule_visibility_gaps_alert_without_claiming_data_passed(rule_project):
    project, _, *_ = rule_project
    project.test_definitions[TEST]["meta"] = {}
    project.nodes[MODEL].meta = {}
    findings = run_checks(Context(project=project), only=["BIZ001"])
    assert {f.check for f in findings} == {"BIZ001", "BIZ002"}
    assert all(f.nodes == [MODEL] for f in findings)


def test_singular_and_unit_test_failures_keep_rule_owner_and_impact(rule_project):
    project, results, *_ = rule_project
    definition = project.test_definitions.pop(TEST)
    project.tests_by_id.clear()  # singular tests have no attached_node
    for uid in ("test.shop.paid_reconciles", "unit_test.shop.tax_calculation"):
        project.test_definitions = {uid: definition}
        row = {**results["results"][1], "unique_id": uid, "status": "error", "message": "rule failed"}
        findings = run_checks(Context(project=project, dbt_results={"results": [row]}), only=["DBTR001", "DBTR002"])
        assert len(findings) == 1 and findings[0].check == "DBTR001"
        assert findings[0].nodes == [MODEL]
        assert findings[0].evidence["business_rule"] and findings[0].evidence["owner"] == "finance"


def test_secondary_check_id_is_selectable():
    results = {"results": [{"unique_id": "test.shop.rule", "status": "fail"}, {"unique_id": MODEL, "status": "skipped"}]}
    findings = run_checks(Context(dbt_results=results), only=["DBTR003"])
    assert [f.check for f in findings] == ["DBTR003"]


def test_rules_api_requires_login_and_returns_project_results(rule_project, local_api):
    _, _, root, target, _ = rule_project
    path, call = local_api
    with Store(path) as store:
        store.set_setting("dbt.target", str(target))
        store.set_setting("dbt.root", str(root))
        token = store.add_user("reader", "viewer")
    assert call("GET", "/api/rules")[0] == 401
    status, body = call("GET", "/api/rules", token=token)
    assert status == 200 and body["rules"][0]["owner"] == "finance"


def test_real_dbt_business_rules_pass_then_detect_daily_data_errors(tmp_path):
    root = tmp_path / "business"
    for folder in ("models", "seeds", "tests"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text("name: business\nversion: '1.0'\nprofile: business\nmodels:\n  business:\n    +materialized: table\n    +meta:\n      owner: data-team\n")
    (root / "profiles.yml").write_text("business:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: warehouse.duckdb\n      threads: 2\n")
    (root / "seeds/order_input.csv").write_text("id,amount,state,customer_id,currency,event_date\n1,10,paid,1,GBP,2026-11-18\n2,-5,cancelled,1,GBP,2026-11-18\n3,0,pending,2,USD,2026-11-18\n")
    (root / "seeds/customer_input.csv").write_text("id\n1\n2\n")
    (root / "seeds/payment_input.csv").write_text("order_id,amount\n1,10\n")
    (root / "models/orders.sql").write_text("""select id,amount,state,customer_id,currency,cast(event_date as date) as event_date from {{ ref('order_input') }}
{% if var('bad', false) %}
union all select 4,11,'paid',1,'GBP',date '2026-11-21'
union all select 5,-2,'paid',null,'BTC',date '2026-11-18'
union all select 6,10,'unknown',999,'GBP',date '2026-11-18'
{% endif %}
""")
    (root / "models/daily_sales.sql").write_text("select event_date,sum(amount) as amount from {{ ref('orders') }} group by event_date\n")
    rules = {
        "paid_payment_reconciles": "select o.id from {{ ref('orders') }} o left join {{ ref('payment_input') }} p on o.id=p.order_id where o.state='paid' and abs(o.amount-coalesce(p.amount,0))>0.001",
        "known_state": "select id from {{ ref('orders') }} where state not in ('paid','cancelled','pending')",
        "known_currency": "select id from {{ ref('orders') }} where currency not in ('GBP','EUR','USD')",
        "paid_positive": "select id from {{ ref('orders') }} where state='paid' and amount<=0",
        "customer_present": "select id from {{ ref('orders') }} where customer_id is null",
        "customer_exists": "select o.id from {{ ref('orders') }} o left join {{ ref('customer_input') }} c on o.customer_id=c.id where o.customer_id is not null and c.id is null",
        "event_not_future": "select id from {{ ref('orders') }} where event_date > date '2026-11-19'",
    }
    for name, sql in rules.items():
        (root / f"tests/{name}.sql").write_text("{{ config(tags=['business_rule'],meta={'owner':'finance'},severity='error') }}\n"+sql+"\n")
    dbt = str(Path(sys.executable).parent / "dbt")
    def build(bad):
        variables = json.dumps({"bad": bad})
        built = subprocess.run([dbt, "build", "--full-refresh", "--profiles-dir", ".", "--vars", variables], cwd=root, capture_output=True, text=True, timeout=120)
        assert built.returncode == int(bad), built.stdout + built.stderr
        artifact = (root / "target/run_results.json").read_bytes()
        docs = subprocess.run([dbt, "docs", "generate", "--profiles-dir", ".", "--vars", variables], cwd=root, capture_output=True, text=True, timeout=120)
        assert docs.returncode == 0, docs.stdout + docs.stderr
        (root / "target/run_results.json").write_bytes(artifact)
    build(False)
    base = tmp_path / "base"
    shutil.copytree(root / "target", base)
    with Store() as store:
        options = {"target": str(root / "target"), "base": str(base), "root": str(root)}
        result = preflight.run(store, options)
        assert result["ok"], result
        p = dbt_loader.load(root / "target", root)
        assert len(business_rules.build(p, dbt_results.load(root / "target"), store)["rules"]) == 7
        build(True)
        result = preflight.run(store, options)
        assert not result["ok"] and any(e["code"] == "build_unsuccessful" for e in result["errors"])
        checked = runner.run_check(store, options, only=["DBTR001"])
        failed = [f for f in checked["findings"] if f.check == "DBTR001"]
        assert len(failed) == 7 and all(f.evidence["owner"] == "finance" and f.evidence["downstream"] >= 1 for f in failed)
        p = dbt_loader.load(root / "target", root)
        catalog = business_rules.build(p, dbt_results.load(root / "target"), store)
        assert all(r["status"] == "fail" and len(r["history"]) == 2 and "daily_sales" in r["downstream"] for r in catalog["rules"])
