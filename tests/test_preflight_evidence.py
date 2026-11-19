import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intentflow import dq_results, preflight
from intentflow.store import Store
from test_preflight import artifacts, edit  # noqa: F401

SOURCE = "source.shop.raw.orders"


def source_evidence(artifacts):
    now = datetime.now(timezone.utc)
    edit(artifacts[1], "manifest.json", lambda d: d.update(sources={SOURCE: {"resource_type": "source", "config": {"freshness": {"error_after": {"count": 1, "period": "hour"}}}}}))
    doc = {"metadata": {"generated_at": now.isoformat()}, "results": [{"unique_id": SOURCE, "status": "pass", "max_loaded_at": (now-timedelta(minutes=15)).isoformat(), "snapshotted_at": now.isoformat()}]}
    (artifacts[1]/"sources.json").write_text(json.dumps(doc))


@pytest.mark.parametrize("mode,code", [("missing", "artifact_unavailable"), ("partial", "source_freshness_incomplete"), ("error", "source_freshness_failed"), ("sla_missing", "source_sla_missing"), ("old_data", "source_stale"), ("future_data", "source_freshness_invalid"), ("invalid", "source_freshness_invalid")])
def test_source_freshness_cannot_pass_without_current_complete_slas(artifacts, mode, code):
    source_evidence(artifacts)
    root, target, base = artifacts
    assert not preflight.inspect_artifacts(target, base, root, require_freshness=True)["errors"]
    if mode == "missing":
        (target/"sources.json").unlink()
    elif mode == "sla_missing":
        edit(target,"manifest.json",lambda d:d["sources"][SOURCE].update(config={}))
    elif mode == "partial":
        edit(target,"sources.json",lambda d:d.update(results=[]))
    elif mode == "invalid":
        edit(target,"sources.json",lambda d:d["results"][0].update(max_loaded_at="bad"))
    elif mode in ("old_data", "future_data"):
        stamp=datetime.now(timezone.utc)+timedelta(hours=-2 if mode=="old_data" else 2)
        edit(target,"sources.json",lambda d:d["results"][0].update(max_loaded_at=stamp.isoformat()))
    else:
        edit(target,"sources.json",lambda d:d["results"][0].update(status="runtime error"))
    report=preflight.inspect_artifacts(target, base, root, require_freshness=True)
    assert code in {e["code"] for e in report["errors"]},report


def test_required_quality_checks_need_recognized_current_mapped_results(artifacts, tmp_path):
    root,target,base=artifacts
    options={"target":target,"base":base,"root":root}
    quality=tmp_path/'soda.json'
    doc={"hasFailures":False,"hasErrors":False,"scanStartTimestamp":datetime.now(timezone.utc).isoformat(),"checks":[{"name":"has rows","table":"orders","outcome":"pass"}]}
    with Store() as store:
        result=preflight.run(store,options,required=['DQ001'])
        assert not result['ok']
        store.set_setting('dq.results',str(quality))
        quality.write_text(json.dumps(doc))
        assert preflight.run(store,options,required=['DQ001'])['ok']
        for change,code in [(lambda d:d.update(scanStartTimestamp='2020-01-01T00:00:00Z'),'artifact_stale'),
                            (lambda d:d['checks'][0].update(table='unknown'),'quality_unmapped'),
                            (lambda d:d['checks'][0].update(table='wrong_schema.orders'),'quality_unmapped'),
                            (lambda d:d['checks'][0].update(outcome='unknown'),'quality_required'),
                            (lambda d:d.update(hasErrors=True),'quality_required'),
                            (lambda d:d.update(checks=[]),'quality_required')]:
            changed=json.loads(json.dumps(doc));change(changed);quality.write_text(json.dumps(changed))
            result=preflight.run(store,options,required=['DQ001'])
            assert not result['ok'] and code in {e['code'] for e in result['errors']},result


def test_source_warnings_follow_explicit_policy(artifacts):
    source_evidence(artifacts)
    root,target,base=artifacts
    edit(target,'manifest.json',lambda d:d['sources'][SOURCE]['config']['freshness'].update(warn_after={'count':5,'period':'minute'}))
    result=preflight.inspect_artifacts(target,base,root,require_freshness=True)
    assert not result['errors'] and result['warnings']
    result=preflight.inspect_artifacts(target,base,root,require_freshness=True,warnings_as_errors=True)
    assert any(e['code']=='source_freshness_warning' for e in result['errors'])


def test_unknown_soda_outcome_is_error_instead_of_pass():
    assert dq_results.parse({'hasFailures':False,'checks':[{'outcome':'unknown'}]})[0].status=='error'


def test_unsupported_mandatory_evidence_does_not_silently_certify_runtime_checks(artifacts):
    root,target,base=artifacts
    with Store() as store:
        result=preflight.run(store,{'target':target,'base':base,'root':root},required=['KAF001'])
    assert not result['ok'] and any(e['code']=='unsupported_required_evidence' for e in result['errors'])


def test_real_dbt_source_freshness_and_revision_metadata(tmp_path):
    root=tmp_path/'freshness'
    for folder in ('models','seeds'):(root/folder).mkdir(parents=True,exist_ok=True)
    (root/'dbt_project.yml').write_text("name: fresh\nversion: '1.0'\nprofile: fresh\nmodels:\n  fresh:\n    +materialized: table\n    +meta:\n      owner: ingestion\n")
    (root/'profiles.yml').write_text("fresh:\n  target: ci\n  outputs:\n    ci:\n      type: duckdb\n      path: warehouse.duckdb\n      threads: 1\n")
    (root/'seeds/source_input.csv').write_text('id,loaded_at\n1,'+datetime.now(timezone.utc).isoformat()+'\n')
    (root/'models/events.sql').write_text("-- depends_on: {{ ref('source_input') }}\nselect id,loaded_at from {{ source('raw','events') }}\n")
    (root/'models/schema.yml').write_text("""version: 2
sources:
  - name: raw
    schema: main
    tables:
      - name: events
        identifier: source_input
        config:
          loaded_at_field: cast(loaded_at as timestamp)
          freshness:
            error_after: {count: 1, period: hour}
models:
  - name: events
    columns:
      - name: id
        data_tests: [not_null]
""")
    dbt=str(Path(sys.executable).parent/'dbt')
    env={**os.environ,'DBT_ENV_CUSTOM_ENV_INTENTFLOW_REVISION':'fresh-revision','DBT_SEND_ANONYMOUS_USAGE_STATS':'false'}
    def run(args,expected=0):
        r=subprocess.run([dbt,*args,'--profiles-dir','.'],cwd=root,env=env,capture_output=True,text=True,timeout=120)
        assert r.returncode==expected,r.stdout+r.stderr
    run(['build'])
    result=(root/'target/run_results.json').read_bytes()
    run(['source','freshness'])
    run(['docs','generate'])
    (root/'target/run_results.json').write_bytes(result)
    base=tmp_path/'base';shutil.copytree(root/'target',base)
    report=preflight.inspect_artifacts(root/'target',base,root,revision='fresh-revision',require_freshness=True)
    assert not report['errors'],report
    import duckdb
    with duckdb.connect(str(root/'warehouse.duckdb')) as con:
        con.execute("update source_input set loaded_at=cast(current_timestamp-interval '2 hours' as varchar)")
    run(['source','freshness'],expected=1)
    (root/'target/run_results.json').write_bytes(result)
    report=preflight.inspect_artifacts(root/'target',base,root,revision='fresh-revision',require_freshness=True)
    assert any(e['code'] in ('source_stale','source_freshness_failed') for e in report['errors']),report
