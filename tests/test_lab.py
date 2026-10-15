"""Lab: lineage answer key (precision/recall), every SQL rule firing, positive probes,
type change and optimizer rewrite equivalence."""
import shutil, subprocess, sys
from pathlib import Path

import pytest

from intentflow import dbt_loader, lineage
from intentflow.checks import Context, run_checks
from intentflow.store import Store
from intentflow.warehouses import DuckDBWarehouse

# answer key: model → {(source, source_column, target_column)}
TRUTH = {
    "lin_window": {("events", "amount", "amount"), ("events", "user_id", "user_id"), ("events", "amount", "running_amount"),
                   ("events", "user_id", "running_amount"), ("events", "ts", "running_amount"), ("events", "user_id", "rn"), ("events", "ts", "rn")},
    "lin_unnest": {("events", "id", "id"), ("events", "tags", "tag")},
    "lin_union_all": {("users", "id", "entity_id"), ("dup_keys", "id", "entity_id"), ("users", "name", "label"), ("dup_keys", "label", "label")},
    "lin_exclude": {("events", c, c) for c in ("id", "user_id", "kind", "ts", "amount")},
    "lin_self_join": {("events", "id", "first_id"), ("events", "id", "next_id")},
    "lin_pivot_case": {("events", "user_id", "user_id"), ("events", "amount", "click_amount"), ("events", "kind", "click_amount"),
                       ("events", "amount", "buy_amount"), ("events", "kind", "buy_amount")},
    "lin_macro": {("events", "id", "id"), ("events", "amount", "amount_units")},
    "lin_subquery_from": {("events", "user_id", "user_id")},          # n = count(*) has no source column
    "lin_constant": {("users", "id", "id")},                          # literais/current_date sem origem
    "lin_nested_ref": {("lin_window", "user_id", "user_id"), ("lin_window", "running_amount", "running_amount"), ("users", "name", "name")},
}


def load(d):
    p = dbt_loader.load(d / "target", d)
    return p, lineage.build(p)


def test_lineage_precision_and_recall_against_ground_truth(lab_target):
    p, lin = load(lab_target)
    nm = lambda u: p.nodes[u].name   # noqa: E731
    tp = fp = fn = 0
    for model, truth in TRUTH.items():
        uid = next(u for u in p.nodes if nm(u) == model)
        got = {(nm(e.src), e.src_col, e.dst_col) for e in lin.edges if e.dst == uid}
        tp += len(got & truth)
        fp += len(got - truth)
        fn += len(truth - got)
        assert got == truth, (model, "extra:", got - truth, "faltando:", truth - got)
    precision, recall = tp / (tp + fp), tp / (tp + fn)
    assert precision == 1.0 and recall == 1.0
    assert lin.precision_report["resolved_ratio"] == 1.0


def test_every_sql_rule_fires_on_its_planted_model(lab_target):
    p, lin = load(lab_target)
    f = run_checks(Context(project=p, lineage=lin), only=["SQL"])
    got = {(x.check, p.nodes[x.nodes[0]].name) for x in f}
    expected = {("SQL001", "lin_exclude"), ("SQL002", "bad_cross"), ("SQL002", "bad_comma_join"), ("SQL003", "bad_union"),
                ("SQL004", "bad_order_cte"), ("SQL005", "bad_nonsargable"), ("SQL006", "bad_distinct_join"),
                ("SQL007", "bad_correlated"), ("SQL008", "bad_like"), ("SQL009", "bad_not_in"), ("SQL010", "bad_big_in"),
                ("SQL011", "bad_or_join"), ("SQL012", "opt_cte")}
    assert expected <= got, expected - got
    # no false positive on clean models
    clean = {"lin_window", "lin_union_all", "lin_macro", "lin_subquery_from", "lin_pivot_case", "lin_nested_ref"}
    assert not [x for x in got if x[1] in clean and x[0] != "SQL012"], [x for x in got if x[1] in clean]


def test_optimizer_rewrite_is_verified_by_execution(lab_target):
    p, lin = load(lab_target)
    wh = DuckDBWarehouse(str(lab_target / "target" / "lab.duckdb"))
    f = run_checks(Context(project=p, lineage=lin, warehouse=wh), only=["SQL012"])
    hit = next(x for x in f if p.nodes[x.nodes[0]].name == "opt_cte")
    assert hit.evidence["verified_equivalent"] is True


def test_equivalence_detects_a_wrong_rewrite(lab_target):
    from intentflow.checks.sql_quality import equivalent
    wh = DuckDBWarehouse(str(lab_target / "target" / "lab.duckdb"))
    assert equivalent(wh, "select id from main.users", "select id from main.users where id > 1") is False
    assert equivalent(wh, "select id from main.users", "select id from main.users order by id") is True


def test_duplicate_key_probe_positive_case(lab_target):
    p, lin = load(lab_target)
    wh = DuckDBWarehouse(str(lab_target / "target" / "lab.duckdb"))
    f = run_checks(Context(project=p, lineage=lin, warehouse=wh, store=Store()), only=["PRB001"])
    hit = [x for x in f if "dup_keys.id" in x.title]
    assert hit and hit[0].evidence["duplicates"] == 1
    assert hit[0].severity in ("critical", "high")


def test_type_change_is_reported_with_consumers(lab_target, tmp_path):
    head = tmp_path / "head"
    shutil.copytree(lab_target, head, ignore=shutil.ignore_patterns("logs"))
    f = head / "models/lin_window.sql"
    f.write_text(f.read_text().replace("sum(amount) over (partition by user_id order by ts) as running_amount",
                                       "cast(sum(amount) over (partition by user_id order by ts) as varchar) as running_amount"))
    dbt = str(Path(sys.executable).parent / "dbt")
    for cmd in (["run", "--select", "lin_window"], ["docs", "generate", "--select", "lin_window"]):
        r = subprocess.run([dbt, *cmd, "--profiles-dir", "."], cwd=head, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-800:]
    bp, bl = load(lab_target)
    hp, hl = load(head)
    r = run_checks(Context(project=hp, lineage=hl, base_project=bp, base_lineage=bl), only=["SCH"])
    t = next(x for x in r if x.check == "SCH002")
    assert "running_amount" in t.title and t.severity == "high"
    assert any(hp.nodes[u].name == "lin_nested_ref" for u in t.nodes)
