"""Regressions found during the project review; all resources are temporary/local."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from intentflow import dbt_loader, web
from intentflow.enterprise import web_ext, llm_fix, proposals
from intentflow.model import Finding
from intentflow.store import Store


@pytest.fixture
def local_api(tmp_path):
    path = str(tmp_path / "review.db")
    with Store(path):
        pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(method, route, body=None, token=None, raw=None, headers=None):
        hs = {"X-IntentFlow": "1", **(headers or {})}
        if token:
            hs["Authorization"] = "Bearer " + token
        data = raw if raw is not None else json.dumps(body or {}).encode() if method == "POST" else None
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{route}", data=data, method=method, headers=hs)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read() or b"{}")

    yield path, call
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_ai_status_is_isolated_by_project(local_api, tmp_path):
    path, call = local_api
    other = str(tmp_path / "other.db")
    finding = Finding("TEST", "low", "same finding", nodes=["same"])
    for location in (path, other):
        with Store(location) as store:
            store.sync_findings([finding], {"TEST"})
    with Store(path) as store:
        store.set_setting("projects", {"other": other})
    key = (path, finding.id)
    with web_ext._AI_JOBS_LOCK:
        web_ext._AI_JOBS[key] = {"state": "done", "result": {"private": "main"}, "finished": time.time()}
    try:
        assert call("POST", f"/api/findings/{finding.id}/ai-status")[1]["state"] == "done"
        assert call("POST", f"/api/findings/{finding.id}/ai-status?project=other")[1] == {"state": "none"}
    finally:
        with web_ext._AI_JOBS_LOCK:
            web_ext._AI_JOBS.pop(key, None)


def test_business_rules_are_isolated_by_project_and_honor_denial(local_api, tmp_path, monkeypatch):
    path, call = local_api
    other = str(tmp_path / "rules-other.db")
    for location, label in ((path, "main"), (other, "other")):
        with Store(location) as store:
            store.set_setting("dbt.target", str(tmp_path / label / "target"))
    with Store(path) as store:
        store.set_setting("projects", {"other": other})
        token = store.add_user("reader", "viewer")
    def load(target, root=None):
        label = Path(target).parent.name
        return dbt_loader.Project({}, test_definitions={label: {
            "id": label, "name": label + "_private_rule", "models": [], "tags": ["business_rule"],
            "meta": {"owner": label}, "severity": "error", "description": "", "file": "tests/rule.sql"}})
    monkeypatch.setattr(dbt_loader, "load", load)
    assert call("GET", "/api/rules", token=token)[1]["rules"][0]["owner"] == "main"
    assert call("GET", "/api/rules?project=other", token=token)[1]["rules"][0]["owner"] == "other"
    with Store(path) as store:
        store.set_project_role("reader", "other", "none")
    assert call("GET", "/api/rules?project=other", token=token)[0] == 403


def test_change_review_is_isolated_by_project_and_honors_denial(local_api, tmp_path, monkeypatch):
    from intentflow import change_review

    path, call = local_api
    other = str(tmp_path / "changes-other.db")
    for location, label in ((path, "main"), (other, "other")):
        with Store(location) as store:
            store.set_setting("dbt.target", str(tmp_path / label / "target"))
            store.set_setting("dbt.base_target", str(tmp_path / label / "baseline"))
    with Store(path) as store:
        store.set_setting("projects", {"other": other})
        token = store.add_user("reader", "viewer")
    def load(target, base, **kwargs):
        assert Path(target).parent == Path(base).parent
        label = Path(target).parent.name
        return {"changes": [{"id": "CHG-" + label, "owner": label, "requires_review": True}], "gaps": []}
    monkeypatch.setattr(change_review, "load", load)
    assert call("GET", "/api/changes", token=token)[1]["changes"][0]["owner"] == "main"
    assert call("GET", "/api/changes?project=other", token=token)[1]["changes"][0]["owner"] == "other"
    with Store(path) as store:
        store.set_project_role("reader", "other", "none")
    assert call("GET", "/api/changes?project=other", token=token)[0] == 403


def _proposal(tmp_path, store):
    file = tmp_path / "model.sql"
    file.write_text("select 1")
    proposal = {"id": "review", "created_by": "author", "changes": [
        {"file": str(file), "before_hash": proposals._h("select 1"), "after": "select 2", "diff": "diff"}
    ], "approval": None, "validation": None}
    store.put_proposal("review", proposal, "pending")
    return file


def test_validation_is_bound_to_content_and_build_policy(tmp_path, monkeypatch):
    with Store() as store:
        file = _proposal(tmp_path, store)
        monkeypatch.setattr(proposals, "validate", lambda p, mode, **kwargs: {"ok": True, "mode": mode, "isolation": "docker-offline-v1"})
        proposals.validate_and_store(store, "review")
        p = store.proposal("review")
        p["changes"][0]["after"] = "select 3"
        store.put_proposal("review", p, "pending")
        proposals.approve(store, "review", "reviewer")
        with pytest.raises(ValueError, match="validation does not match"):
            proposals.apply(store, "review")
        assert file.read_text() == "select 1"
        proposals.validate_and_store(store, "review")
        p = store.proposal("review")
        p["policy"] = {"validate_mode": "build"}
        store.put_proposal("review", p, "approved")
        with pytest.raises(ValueError, match="requires a build validation"):
            proposals.apply(store, "review")


def test_validate_rejects_unknown_mode_and_downgraded_policy(tmp_path):
    with Store() as store:
        _proposal(tmp_path, store)
        proposal = store.proposal("review")
        with pytest.raises(ValueError, match="mode"):
            proposals.validate(proposal, "arbitrary")
        proposal["policy"] = {"validate_mode": "build"}
        with pytest.raises(ValueError, match="build"):
            proposals.validate(proposal, "parse")


def test_extra_review_cannot_be_self_approved(tmp_path):
    with Store() as store:
        _proposal(tmp_path, store)
        p = store.proposal("review")
        p["policy"] = {"needs_extra_review": True}
        store.put_proposal("review", p, "pending")
        with pytest.raises(ValueError, match="cannot approve"):
            proposals.approve(store, "review", "author")
        proposals.approve(store, "review", "reviewer")


def test_rollback_preserves_subsequent_edits(tmp_path, monkeypatch):
    with Store() as store:
        file = _proposal(tmp_path, store)
        monkeypatch.setattr(proposals, "validate", lambda p, mode, **kwargs: {"ok": True, "mode": mode, "isolation": "docker-offline-v1"})
        proposals.approve(store, "review", "reviewer")
        proposals.validate_and_store(store, "review")
        proposals.apply(store, "review")
        file.write_text("select 99")
        with pytest.raises(ValueError, match="preserve"):
            proposals.rollback(store, "review")
        assert file.read_text() == "select 99"
        assert store.proposal("review")["status"] == "applied"


def test_project_admin_cannot_access_global_users_or_change_reserved_settings(local_api):
    path, call = local_api
    with Store(path) as store:
        token = store.add_user("project-admin", "viewer")
        store.set_project_role("project-admin", "main", "admin")
    assert call("GET", "/api/users", token=token)[0] == 403
    for key in ("projects", "sso.issuer", "secret.llm_api_key", "state.running"):
        assert call("POST", "/api/settings", {"key": key, "value": "forbidden"}, token=token)[0] == 400
    with Store(path) as store:
        assert store.get_setting("projects") is None
        assert store.get_setting("sso.issuer") is None
    assert call("POST", "/api/settings", {"key": "dbt.target", "value": "target"}, token=token)[0] == 403      # paths: global admin only
    assert call("POST", "/api/settings", {"key": "scan.include_packages", "value": "shop"}, token=token)[0] == 200


def test_manifest_paths_cannot_escape_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.sql"
    outside.write_text("select 'private'")
    (root / "link.sql").symlink_to(outside)
    for path in ("../outside.sql", str(outside), "link.sql"):
        project = dbt_loader.Project({"m": dbt_loader.DbtNode("m", "m", "model", path=path)}, root=root)
        with pytest.raises(ValueError, match="outside the project"):
            proposals._model_file(project, "m")
        with pytest.raises(ValueError, match="outside the project"):
            llm_fix.build_context(project, {"nodes": ["m"]})
