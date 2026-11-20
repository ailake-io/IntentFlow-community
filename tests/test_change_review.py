import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from intentflow import change_review, cli, preflight
from intentflow.store import Store
from test_preflight import artifacts, edit, MODEL, TEST  # noqa: F401
from test_review_regressions import local_api  # noqa: F401


def test_preflight_does_not_approve_removing_a_test_when_other_tests_pass(artifacts):
    root, target, base = artifacts
    other = 'test.shop.not_null_orders_id.def'
    def add_other(doc):
        node = copy.deepcopy(doc['nodes'][TEST])
        node.update(unique_id=other, name='not_null_orders_id')
        node['test_metadata']['name'] = 'not_null'
        doc['nodes'][other] = node
    edit(base, 'manifest.json', add_other)
    edit(target, 'manifest.json', add_other)
    edit(target, 'manifest.json', lambda d:d['nodes'].pop(TEST))
    edit(target, 'run_results.json', lambda d:d['results'][1].update(unique_id=other))
    with Store() as store:
        report = preflight.run(store, {'target':target, 'base':base, 'root':root}, fail_on='critical')
    assert not report['ok'], 'An existing successful test must not hide another test being removed'


@pytest.fixture
def documents(artifacts):
    _, target, base = artifacts
    def read(folder, name):return json.loads((folder/name).read_text())
    return read(base,'manifest.json'),read(target,'manifest.json'),read(base,'catalog.json'),read(target,'catalog.json')


def compare(documents):
    return change_review.acknowledge(change_review.compare(*documents))


@pytest.mark.parametrize('change',[
    lambda n:n['config'].update(severity='warn'),
    lambda n:n['config'].update(error_if='> 100'),
    lambda n:n['config'].update(where='1 = 0'),
    lambda n:n['config'].update(limit=1),
    lambda n:n['config'].update(sql_header="set timezone='UTC'"),
    lambda n:n['config'].update(custom_test_option=False),
    lambda n:n.update(raw_code='select 1 where 1=0'),
])
def test_successful_build_does_not_make_changed_test_guarantees_safe(documents,change):
    before,after,*_=documents
    before['nodes'][TEST]['config']={}
    after['nodes'][TEST]['config']={}
    change(after['nodes'][TEST])
    result=compare(documents)
    assert [c['kind'] for c in result['changes']]==['test_definition_changed']
    assert result['unreviewed']
    assert result['changes'][0]['impact'][0]['id']==MODEL


def test_tracking_removal_and_disabled_test_are_visible(documents):
    before,after,*_=documents
    before['nodes'][TEST]['tags']=['business_rule']
    assert compare(documents)['changes'][0]['kind']=='business_tracking_removed'
    after['nodes'][TEST]['config']={'enabled':False}
    assert compare(documents)['changes'][0]['kind']=='test_removed'


def test_comments_layout_and_owner_do_not_require_business_policy_review(documents):
    before,after,*_=documents
    before['nodes'][TEST].update(raw_code="{{ config(meta={'owner':'old'}) }} select id from {{ ref('orders') }} where id > 0")
    after['nodes'][TEST].update(raw_code="{{ config(meta={'owner':'new'}) }}\n SELECT id\nFROM {{ ref('orders') }} -- comment\nWHERE id > 0")
    assert not compare(documents)['unreviewed']
    after['nodes'][TEST]['raw_code']="select 'a  b' from {{ ref('orders') }}"
    before['nodes'][TEST]['raw_code']="select 'a b' from {{ ref('orders') }}"
    assert compare(documents)['unreviewed']


def test_config_call_parser_preserves_literal_delimiters(documents):
    before,after,*_=documents
    before['nodes'][TEST]['raw_code']="{{ config(meta={'note':'}} ) private'}) }} select 1"
    after['nodes'][TEST]['raw_code']="{{ config(meta={'note':'other'}) }} select 1"
    assert not compare(documents)['unreviewed']
    after['nodes'][TEST]['raw_code']+=" where 1=0"
    assert compare(documents)['unreviewed']


def test_generic_test_identity_survives_dbt_argument_hash_change(documents):
    before,after,*_=documents
    new_id='test.shop.unique_orders_id.changedhash'
    after['nodes'][new_id]=after['nodes'].pop(TEST)
    after['nodes'][new_id]['unique_id']=new_id
    after['nodes'][new_id]['test_metadata']['kwargs']['allowed_values']=['new']
    changes=compare(documents)['changes']
    assert len(changes)==1 and changes[0]['kind']=='test_definition_changed'
    assert changes[0]['resource']==new_id


def test_referenced_macro_changes_are_reviewed_and_unused_macros_are_not(documents):
    before,after,*_=documents
    macro='macro.shop.rule'
    for manifest in (before,after):
        manifest['nodes'][TEST]['depends_on']['macros']=[macro]
        manifest['macros']={macro:{'macro_sql':'select 1','depends_on':{'macros':[]}},'macro.shop.unused':{'macro_sql':'select 2'}}
    after['macros']['macro.shop.unused']['macro_sql']='select 3'
    assert not compare(documents)['unreviewed']
    after['macros'][macro]['macro_sql']='select 1 where 1=0'
    result=compare(documents)
    assert result['changes'][0]['changed_fields']==['macros']
    assert result['unreviewed']


def test_incremental_grain_contract_constraints_and_alias_are_reviewed(documents):
    before,after,*_=documents
    before['nodes'][MODEL]['config'].update(materialized='incremental',unique_key='order_id',contract={'enforced':True})
    before['nodes'][MODEL]['columns']={'id':{'constraints':[{'type':'not_null'}]}}
    after['nodes'][MODEL]['config'].update(materialized='incremental',unique_key='customer_id')
    after['nodes'][MODEL]['alias']='customer_orders'
    kinds={c['kind'] for c in compare(documents)['changes']}
    assert kinds=={'incremental_changed','model_contract_disabled','constraints_removed','relation_alias_changed'}


def test_schema_comparison_covers_columns_without_declared_consumers(documents):
    _,_,old,new=documents
    new['nodes'][MODEL]['columns']={'new_id':{'type':'VARCHAR'}}
    report=compare(documents)
    assert {c['kind'] for c in report['changes']}=={'column_removed','column_added'}
    assert sum(c['requires_review'] for c in report['changes'])==1
    new['nodes'][MODEL]['columns']={'id':{'type':'VARCHAR'}}
    assert compare(documents)['changes'][0]['kind']=='column_type_changed'


def test_schema_environment_names_do_not_masquerade_as_table_renames(documents):
    _,head,*_=documents
    head['nodes'][MODEL]['relation_name']='"ci_db"."ci_schema"."orders"'
    assert not compare(documents)['changes']


def test_model_logic_can_also_require_an_explicit_review(documents):
    documents[1]['nodes'][MODEL]['raw_code']='select 2 as id'
    assert not compare(documents)['unreviewed']
    strict=change_review.acknowledge(change_review.compare(*documents,review_model_logic=True))
    assert strict['unreviewed'] and strict['changes'][0]['kind']=='model_logic_changed'


def test_removed_model_retains_transitive_dashboard_impact(documents):
    before,after,*_=documents
    daily='model.shop.daily_sales';dashboard='exposure.shop.dashboard'
    for manifest in (before,after):
        manifest['nodes'][daily]={'resource_type':'model','name':'daily_sales','config':{'materialized':'ephemeral'},'depends_on':{'nodes':[MODEL]}}
        manifest['exposures']={dashboard:{'name':'dashboard','resource_type':'exposure','depends_on':{'nodes':[daily]}}}
    after['nodes'].pop(MODEL)
    item=next(c for c in compare(documents)['changes'] if c['kind']=='resource_removed')
    assert {n['id'] for n in item['impact']}=={MODEL,daily,dashboard}


def test_missing_catalog_is_an_explicit_gap_not_no_changes(documents):
    documents[3]['nodes'].clear()
    assert compare(documents)['gaps'][0]['resource']==MODEL


def test_large_impact_preview_is_bounded_and_explicitly_marked(documents):
    before,after,*_=documents
    for index in range(500):
        uid=f'model.shop.child_{index}'
        node={'name':str(index),'resource_type':'model','config':{'materialized':'ephemeral'},'depends_on':{'nodes':[MODEL]}}
        before['nodes'][uid]=copy.deepcopy(node);after['nodes'][uid]=copy.deepcopy(node)
    after['nodes'].pop(MODEL)
    removed=next(c for c in compare(documents)['changes'] if c['kind']=='resource_removed')
    assert removed['impact_truncated'] and len(removed['impact'])<=100


def test_source_freshness_policy_cannot_be_silently_relaxed(documents):
    before,after,old,new=documents
    source='source.shop.raw.orders'
    for manifest,catalog in ((before,old),(after,new)):
        manifest['sources']={source:{'resource_type':'source','name':'raw_orders','config':{'freshness':{'error_after':{'count':1,'period':'hour'}}}}}
        catalog['sources']={source:{'columns':{'id':{'type':'INT'}}}}
    after['sources'][source]['config']['freshness']['error_after']['count']=48
    assert compare(documents)['changes'][0]['kind']=='source_freshness_changed'


def test_exact_acknowledgement_is_required_and_does_not_apply_to_later_edits(artifacts):
    root,target,base=artifacts
    edit(target,'manifest.json',lambda d:d['nodes'][TEST].update(config={'severity':'warn'}))
    options={'target':target,'base':base,'root':root}
    with Store() as store:
        blocked=preflight.run(store,options,fail_on='critical')
        assert not blocked['ok']
        identity=blocked['change_review']['unreviewed'][0]
        accepted=preflight.run(store,options,accepted_changes=[identity+'=Intentional policy change, approved in the PR'])
        assert accepted['ok'],accepted
        assert accepted['change_review']['changes'][0]['reason']
        edit(target,'manifest.json',lambda d:d['nodes'][TEST]['config'].update(error_if='> 999'))
        with pytest.raises(ValueError):preflight.run(store,options,accepted_changes=[identity+'=old acknowledgement'])


@pytest.mark.parametrize('acceptance',['*=', 'CHG-any=reason', 'CHG-any', 'CHG-any='])
def test_unknown_or_blanket_acknowledgements_are_rejected(documents,acceptance):
    with pytest.raises(ValueError):change_review.acknowledge(compare(documents),[acceptance])


def test_preview_cli_is_read_only_and_can_be_used_as_a_review_gate(artifacts,tmp_path,capsys):
    _,target,base=artifacts
    edit(target,'manifest.json',lambda d:d['nodes'][TEST].update(config={'severity':'warn'}))
    args=['--store',str(tmp_path/'preview.db'),'changes','--target',str(target),'--base',str(base),'--format','json']
    assert cli.main(args)==0
    result=json.loads(capsys.readouterr().out)
    assert result['unreviewed']
    assert cli.main(args+['--fail-on-review'])==1


def test_change_acknowledgement_does_not_waive_failed_build_or_checks(artifacts,monkeypatch):
    from intentflow.checks import REGISTRY,load_all
    root,target,base=artifacts
    edit(target,'manifest.json',lambda d:d['nodes'][TEST].update(config={'severity':'warn'}))
    options={'target':target,'base':base,'root':root}
    with Store() as store:
        identity=preflight.run(store,options)['change_review']['unreviewed'][0]
        edit(target,'run_results.json',lambda d:d['results'][1].update(status='fail'))
        report=preflight.run(store,options,accepted_changes=[identity+'=Intentional policy review'])
        assert not report['ok'] and any(e['code']=='build_unsuccessful' for e in report['errors'])
        edit(target,'run_results.json',lambda d:d['results'][1].update(status='pass'))
        load_all()
        def broken(ctx):raise RuntimeError('unavailable')
        monkeypatch.setattr(REGISTRY['SQL001'],'fn',broken)
        report=preflight.run(store,options,accepted_changes=[identity+'=Intentional policy review'])
        assert not report['ok'] and 'SQL001' in report['failed_checks']


def test_python_indentation_is_significant_in_model_change_preview(documents):
    before,after,*_=documents
    before['nodes'][MODEL].update(language='python',raw_code='def model():\n  if True:\n    return 1\n  return 2')
    after['nodes'][MODEL].update(language='python',raw_code='def model():\n  if True:\n    return 1\n    return 2')
    assert compare(documents)['changes'][0]['kind']=='model_logic_changed'


def test_runtime_partition_date_is_not_a_changed_declared_rule(documents):
    before,after,*_=documents
    for manifest in (before,after):
        manifest['nodes'][TEST]['unrendered_config']={'where':"event_date = '{{ var(\"day\") }}'"}
    before['nodes'][TEST]['config']={'where':"event_date='2026-11-19'"}
    after['nodes'][TEST]['config']={'where':"event_date='2026-11-20'"}
    assert not compare(documents)['unreviewed']


def test_preview_does_not_expose_raw_sql_or_arbitrary_metadata(documents):
    before,after,*_=documents
    before['nodes'][TEST]['raw_code']="select 'sensitive example'"
    after['nodes'][TEST].update(raw_code="select 'another sensitive example'",meta={'private_note':'private context'})
    encoded=json.dumps(compare(documents))
    assert 'sensitive example' not in encoded and 'private context' not in encoded


def test_change_preview_api_requires_authentication(artifacts,local_api):
    _,target,base=artifacts
    path,call=local_api
    with Store(path) as store:
        store.set_setting('dbt.target',str(target));store.set_setting('dbt.base_target',str(base))
        token=store.add_user('reader','viewer')
    assert call('GET','/api/changes')[0]==401
    status,result=call('GET','/api/changes',token=token)
    assert status==200 and result['summary']['total']==0 and not result['gaps']


def test_baseline_cannot_be_the_candidate_or_the_wrong_deployed_revision(artifacts):
    root,target,base=artifacts
    with Store() as store:
        with pytest.raises(ValueError):preflight.run(store,{'target':target,'base':target,'root':root})
        mismatch=preflight.run(store,{'target':target,'base':base,'root':root},baseline_revision='deployed-a')
        assert not mismatch['ok'] and any(e['code']=='change_comparison_unavailable' for e in mismatch['errors'])
        for name in ('manifest.json','catalog.json'):
            edit(base,name,lambda d:d['metadata'].update(env={'INTENTFLOW_REVISION':'deployed-a'}))
        assert preflight.run(store,{'target':target,'base':base,'root':root},baseline_revision='deployed-a')['ok']
    with pytest.raises(ValueError):change_review.load(target,target)


def test_ambiguous_generic_test_changes_are_reviewed_as_a_set(documents):
    before,after,*_=documents
    for manifest in (before,after):
        template=manifest['nodes'].pop(TEST)
        for index in (1,2):
            uid=TEST+str(index)+('old' if manifest is before else 'new')
            node=copy.deepcopy(template)
            node['test_metadata']['kwargs']['custom_limit']=index+(0 if manifest is before else 10)
            manifest['nodes'][uid]=node
    report=compare(documents)
    assert [c['kind'] for c in report['changes']]==['test_set_changed']
    assert len(report['unreviewed'])==1


def test_transitive_macro_cycles_terminate_and_changes_require_review(documents):
    before,after,*_=documents
    for manifest in (before,after):
        manifest['nodes'][TEST]['depends_on']['macros']=['macro.shop.a']
        manifest['macros']={
            'macro.shop.a':{'macro_sql':'select 1','depends_on':{'macros':['macro.shop.b']}},
            'macro.shop.b':{'macro_sql':'select 2','depends_on':{'macros':['macro.shop.a']}}}
    after['macros']['macro.shop.b']['macro_sql']='select 3'
    report=compare(documents)
    assert report['changes'][0]['changed_fields']==['macros']
    assert report['unreviewed']


def test_embedded_unit_expectation_changes_require_review(documents):
    before,after,*_=documents
    uid='unit_test.shop.orders.projection'
    for manifest in (before,after):
        manifest['unit_tests']={uid:{'resource_type':'unit_test','name':'projection','package_name':'shop',
                                    'depends_on':{'nodes':[MODEL]},'expect':{'rows':[{'id':1,'amount':10}]}}}
    after['unit_tests'][uid]['expect']={'rows':[{'id':1}]}
    report=compare(documents)
    assert report['changes'][0]['changed_fields']==['unit_fixtures']
    assert report['unreviewed']


def test_native_dbt_passing_build_cannot_hide_removed_or_vacuous_rule(tmp_path):
    root=tmp_path/'native'
    for folder in ('models','seeds','tests'):
        (root/folder).mkdir(parents=True,exist_ok=True)
    (root/'dbt_project.yml').write_text("name: review\nversion: '1.0'\nprofile: review\nmodels:\n  review:\n    +materialized: table\n    +meta:\n      owner: finance\n")
    (root/'profiles.yml').write_text('review:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: warehouse.duckdb\n      threads: 2\n')
    (root/'seeds/input.csv').write_text('id,amount\n1,10\n2,20\n')
    (root/'models/orders.sql').write_text("select id,amount from {{ ref('input') }}\n")
    (root/'tests/unique_ids.sql').write_text("select id from {{ ref('orders') }} group by id having count(*)>1\n")
    rule=root/'tests/positive_amount.sql'
    original="{{ config(tags=['business_rule'],meta={'owner':'finance'}) }}\nselect id from {{ ref('orders') }} where amount<=0\n"
    rule.write_text(original)
    target,base=root/'target',tmp_path/'baseline'
    dbt=str(Path(sys.executable).parent/'dbt')
    def build():
        def invoke(args):
            result=subprocess.run([dbt,*args,'--profiles-dir','.'],cwd=root,capture_output=True,text=True,timeout=120)
            assert result.returncode==0,result.stdout+result.stderr
        invoke(['build','--full-refresh'])
        man,res=(target/'manifest.json').read_bytes(),(target/'run_results.json').read_bytes()
        invoke(['docs','generate'])
        (target/'manifest.json').write_bytes(man)
        (target/'run_results.json').write_bytes(res)
        assert all(r['status'] in ('success','pass') for r in json.loads(res)['results'])
    build()
    shutil.copytree(target,base)
    with Store() as store:
        options={'root':root,'target':target,'base':base}
        assert preflight.run(store,options)['ok']
        for kind in ('test_removed','test_definition_changed'):
            if kind=='test_removed':rule.unlink()
            else:rule.write_text(original.replace('amount<=0','1=0'))
            build()
            report=preflight.run(store,options)
            assert not report['ok'] and {e['code'] for e in report['errors']}=={'changes_require_review'},report
            change=report['change_review']['changes'][0]
            assert change['kind']==kind
            assert preflight.run(store,options,accepted_changes=[change['id']+'=Intentional synthetic test'])['ok']
