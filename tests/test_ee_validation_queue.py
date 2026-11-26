"""A UI process without Docker queues validations; a worker on a Docker host runs them and stores the result."""
import pytest
from conftest import new_db
from intentflow.store import Store
from intentflow.enterprise import proposals


def _proposal(st, tmp_path):
    f = tmp_path / "m.sql"
    f.write_text("select 1")
    p = {"changes": [{"file": str(f), "before_hash": proposals._h("select 1"), "after": "select 2"}], "summary": "x", "project_root": str(tmp_path)}
    st.put_proposal("fix-1", p, "pending")


def test_worker_runs_queued_validation_and_clears_request(tmp_path, monkeypatch):
    st = Store(new_db(tmp_path, "s.db"))
    _proposal(st, tmp_path)
    monkeypatch.setattr(proposals, "validate", lambda p, mode, source_lock_held=False: {"ok": True, "mode": mode, "isolation": "x"})
    proposals.request_validation(st, "fix-1", "build", "bob")
    assert st.proposal("fix-1")["validation_request"]["by"] == "bob"
    assert proposals.process_validation_requests(st) == 1
    p = st.proposal("fix-1")
    assert p["validation"]["ok"] and p["validation"]["mode"] == "build" and "validation_request" not in p
    assert any(a["actor"] == "bob" and a["event"] == "validated" for a in st.audit_log())
    assert proposals.process_validation_requests(st) == 0


def test_request_is_dropped_when_the_proposal_changed_after_queueing(tmp_path, monkeypatch):
    st = Store(new_db(tmp_path, "s.db"))
    _proposal(st, tmp_path)
    monkeypatch.setattr(proposals, "validate", lambda *a, **k: pytest.fail("must not validate a changed proposal"))
    proposals.request_validation(st, "fix-1", "parse", "bob")
    p = st.proposal("fix-1"); p["changes"][0]["after"] = "select 3"; st.put_proposal("fix-1", p, "pending")
    assert proposals.process_validation_requests(st) == 1
    assert "validation_request" not in st.proposal("fix-1") and "validation" not in st.proposal("fix-1")
    assert any(a["event"] == "validation_failed" for a in st.audit_log())


def test_busy_request_stays_queued_and_unsafe_rename_names_are_refused(tmp_path, monkeypatch):
    from intentflow.enterprise import file_transaction as ft
    st = Store(new_db(tmp_path, "s.db"))
    _proposal(st, tmp_path)
    def busy(*a, **k): raise ft.Busy("host at its validation cap")
    monkeypatch.setattr(proposals, "validate", busy)
    proposals.request_validation(st, "fix-1", "build", "bob")
    assert proposals.process_validation_requests(st) == 0           # deferred, not lost
    assert st.proposal("fix-1")["validation_request"]["by"] == "bob"
    monkeypatch.setattr(proposals, "validate", lambda p, mode, source_lock_held=False: {"ok": True, "mode": mode, "isolation": "x"})
    assert proposals.process_validation_requests(st) == 1
    with pytest.raises(ValueError, match="plain identifier"):
        proposals.build_rename(None, {"old": "a", "new": "x }} {{ run_query('drop') }}", "targets": []}, "f1")

