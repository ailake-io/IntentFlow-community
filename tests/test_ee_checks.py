import shutil, subprocess, sys
from pathlib import Path

import pytest

from intentflow import dbt_loader, lineage
from intentflow.enterprise import proposals
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


@pytest.mark.usefixtures("sandbox_image")
def test_governed_fix_validates_in_sandbox_and_is_reversible(drift, tmp_path):
    base, head = drift
    work = tmp_path / "proj"
    shutil.copytree(head, work, ignore=shutil.ignore_patterns("logs"))
    wp = dbt_loader.load(work / "target", work)
    bp = dbt_loader.load(base / "target", base)
    ctx = Context(project=wp, lineage=lineage.build(wp), base_project=bp, base_lineage=lineage.build(bp))
    fnd = next(x for x in run_checks(ctx, only=["SCH001"]) if "stg_customers.email" in x.title)
    st = Store()
    st.sync_findings([fnd], {"SCH001"})
    prop = proposals.create(st, wp, st.findings(None)[0])
    diff = prop["changes"][0]["diff"]
    assert "c.email_address as email" in diff                       # output name kept
    with pytest.raises(ValueError):
        proposals.apply(st, prop["id"])                             # no apply without approval
    proposals.approve(st, prop["id"], "thiago")
    with pytest.raises(ValueError):
        proposals.apply(st, prop["id"])                             # no apply without validation
    v = proposals.validate_and_store(st, prop["id"], "build")["validation"]
    assert v["ok"], v["output"]
    original = (work / "models/marts/customers.sql").read_text()
    proposals.apply(st, prop["id"])
    assert "c.email_address as email" in (work / "models/marts/customers.sql").read_text()
    proposals.rollback(st, prop["id"])
    assert (work / "models/marts/customers.sql").read_text() == original
    events = [a["event"] for a in st.audit_log()]
    assert events == ["proposal_created", "approved", "validated", "applied", "rolled_back"]
