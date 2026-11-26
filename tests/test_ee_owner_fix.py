import pytest

from intentflow.enterprise import proposals
from intentflow.dbt_loader import DbtNode, Project


def _proj(tmp_path, sql):
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "m.sql").write_text(sql)
    n = DbtNode("model.p.m", "m", "model", relation='"d"."s"."m"')
    n.path = "models/m.sql"
    return Project(adapter="duckdb", nodes={"model.p.m": n}, root=tmp_path)


F = {"id": "DBT003-x", "nodes": ["model.p.m"]}


def test_owner_is_prepended_when_model_has_no_config(tmp_path):
    p = proposals.build_owner(_proj(tmp_path, "select 1 as a\n"), F, "time-dados")
    assert p["changes"][0]["after"].startswith("{{ config(meta={'owner': 'time-dados'}) }}\nselect 1")


def test_owner_is_inserted_into_existing_config(tmp_path):
    p = proposals.build_owner(_proj(tmp_path, "{{ config(materialized='table') }}\nselect 1 as a\n"), F, "ana@x.com")
    assert "config(meta={'owner': 'ana@x.com'}, materialized='table')" in p["changes"][0]["after"]


def test_owner_rejects_injection_and_existing_meta(tmp_path):
    with pytest.raises(ValueError, match="invalid owner"):
        proposals.build_owner(_proj(tmp_path, "select 1\n"), F, "x'}) }} {{ drop")
    with pytest.raises(ValueError, match="already defines meta"):
        proposals.build_owner(_proj(tmp_path / "w", "{{ config(meta={'a': 1}) }}\nselect 1\n"), F, "ana")


FK = {"id": "DBT002-x", "nodes": ["model.p.m"]}


def test_unique_key_single_composite_and_existing_config(tmp_path):
    p = proposals.build_unique_key(_proj(tmp_path / "a", "select 1 as id\n"), FK, "id")
    assert p["kind"] == "add_unique_key" and p["changes"][0]["after"].startswith("{{ config(unique_key='id') }}\nselect 1")
    p = proposals.build_unique_key(_proj(tmp_path / "b", "{{ config(materialized='incremental') }}\nselect 1\n"), FK, "a, b")
    assert "config(unique_key=['a', 'b'], materialized='incremental')" in p["changes"][0]["after"]


def test_unique_key_rejects_injection_unknown_column_and_existing_key(tmp_path):
    with pytest.raises(ValueError, match="invalid key"):
        proposals.build_unique_key(_proj(tmp_path / "a", "select 1\n"), FK, "id') }} {{ drop")
    with pytest.raises(ValueError, match="already defines unique_key"):
        proposals.build_unique_key(_proj(tmp_path / "b", "{{ config(unique_key='x') }}\nselect 1\n"), FK, "id")
    proj = _proj(tmp_path / "c", "select 1\n")
    from intentflow.dbt_loader import Col
    proj.nodes["model.p.m"].columns = {"id": Col("id", "int")}
    with pytest.raises(ValueError, match="do not exist"):
        proposals.build_unique_key(proj, FK, "nope")
