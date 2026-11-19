"""Visibility and accountability for business rules declared in dbt."""
from .. import business_rules
from ..i18n import tr
from ..model import Finding
from . import check


@check("BIZ001", "Business rule result unavailable", stage="continuous", emits=("BIZ002",))
def business_rule_coverage(ctx):
    if ctx.project is None:
        return []
    catalog = business_rules.build(ctx.project, ctx.dbt_results, ctx.store,
                                   max_age_hours=float(ctx.policies.get("max_rule_age_hours", 24)))
    out = []
    for rule in catalog["rules"]:
        evidence = {"test_id": rule["id"], "business_rule": True, "file": rule["file"], "status": rule["status"]}
        if rule["owner"]:
            evidence["owner"] = rule["owner"]
        if not rule["fresh"] or rule["status"] in ("skipped", "not_run"):
            out.append(Finding("BIZ001", "high", tr("{rule}: business rule has no current execution", rule=rule["name"]),
                               tr("Run the declared dbt test and preserve run_results; an absent, skipped or outdated result is not a pass."),
                               rule["model_ids"], "continuous", evidence))
        if not rule["owner"]:
            out.append(Finding("BIZ002", "medium", tr("{rule}: business rule has no owner", rule=rule["name"]),
                               tr("Set meta.owner on the dbt test or one unambiguous owner on its input models."),
                               rule["model_ids"], "continuous", evidence))
    return out
