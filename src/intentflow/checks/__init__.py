"""Pluggable check registry.

A check is a function `(ctx) -> list[Finding]` registered with `@check(id, title, stage, cost)`.
`cost`: static (metadata/code only) | probe (warehouse query) | statistical (history).
Titles are English; display them with `tr(check.title)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..i18n import tr
from ..model import Finding

REGISTRY: dict[str, "Check"] = {}


@dataclass
class Check:
    id: str
    title: str
    stage: str
    cost: str
    fn: Callable
    emits: tuple = ()      # secondary ids this check also produces (they must resolve on their own when the problem goes away)


def check(id: str, title: str, stage: str = "pre-merge", cost: str = "static", emits: tuple = ()):
    def deco(fn):
        REGISTRY[id] = Check(id, title, stage, cost, fn, tuple(emits))
        return fn
    return deco


@dataclass
class Context:
    project: object = None            # dbt_loader.Project
    lineage: object = None            # lineage.LineageResult
    base_project: object = None       # previous version (schema diff)
    base_lineage: object = None
    policies: dict = field(default_factory=dict)
    airflow_dags_dir: str | None = None
    kestra_flows_dir: str | None = None
    store: object = None
    warehouse: object = None
    runs: list = field(default_factory=list)       # orchestrators.Run
    samples: list = field(default_factory=list)    # monitoring.Sample
    dbt_results: dict | None = None                # dbt_results.load()
    dq_results: list = field(default_factory=list)  # dq_results.DQResult (GX/Soda)
    contracts: list = field(default_factory=list)   # contracts.ModelContract
    external: list = field(default_factory=list)    # external.ExtRead: jobs outside the SQL (OpenLineage + declared)
    completed_checks: set[str] = field(default_factory=set)  # only checks completed in this run
    parsed_sql: dict = field(default_factory=dict)  # ASTs shared only within one run


def load_all() -> None:
    from . import dbt_quality, schema_drift, sql_quality, orchestration, airflow3, dq, contracts, streaming, lint, elementary, lakehouse, catalog, probes, cluster, runtime_history, cost, dbt_run, deep, external  # noqa: F401


def run_checks(ctx: Context, only: list[str] | None = None, costs: set[str] | None = None, stages: set[str] | None = None) -> list[Finding]:
    load_all()
    out: list[Finding] = []
    ctx.completed_checks.clear()
    ctx.parsed_sql.clear()
    disabled = set(ctx.policies.get("disabled_checks", []) or [])
    for c in REGISTRY.values():
        if c.id in disabled:
            continue
        if only and c.id not in only and not any(c.id.startswith(o) for o in only):
            continue
        if costs and c.cost not in costs:
            continue
        if stages and c.stage not in stages:
            continue
        try:
            out.extend(c.fn(ctx) or [])
            ctx.completed_checks.update((c.id, *c.emits))
        except Exception as e:   # a broken check does not take the others down
            out.append(Finding(c.id, "info", tr("check {id} failed to run", id=c.id), f"{type(e).__name__}: {e}", stage=c.stage))
    return out
