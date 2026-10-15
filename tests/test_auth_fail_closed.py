"""Stored per-project restrictions keep working without the Enterprise extension (no fail-open)."""
from intentflow import auth
from intentflow.web import scope_ok


def test_project_restrictions_and_scopes_are_honored_without_enterprise():
    who = {"role": "admin", "project_roles": {"main": "none"}, "scopes": {"main": ["models/marts/*"]}}
    assert auth.role_in(who, "main") == "none"
    eng = {"role": "engineer", "scopes": {"main": ["models/marts/*"]}}
    assert not scope_ok(eng, "main", {"evidence": {"file": "models/staging/a.sql"}, "nodes": []}, "engineer")
    assert scope_ok(eng, "main", {"evidence": {"file": "models/marts/b.sql"}, "nodes": []}, "engineer")
