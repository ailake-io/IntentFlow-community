"""SQLite × PostgreSQL store parity, migration between backends and concurrent initialization.
PostgreSQL tests are skipped without the database (INTENTFLOW_TEST_PG_ADMIN, default: local intentflow-pg container)."""
import json, os, subprocess, sys, threading, time
from pathlib import Path

import pytest

from conftest import new_db
from intentflow.model import Finding
from intentflow.monitoring import Sample
from intentflow.orchestrators import Run
from intentflow.store import Store, copy_store

ROOT = Path(__file__).resolve().parent.parent


def pg_available():
    try:
        import psycopg2
        from conftest import PG_ADMIN
        psycopg2.connect(PG_ADMIN, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres" and not pg_available():
        pytest.skip("local PostgreSQL unavailable")
    return Store(new_db(tmp_path, "b.db", request.param))


def test_findings_lifecycle_and_stats(store):
    fs = [Finding("SQL001", "medium", f"m{i}", nodes=[f"n{i}"], evidence={"owner": "a"}) for i in range(3)]
    assert store.sync_findings(fs, {"SQL001"})["new"] == 3
    assert store.sync_findings(fs, {"SQL001"})["new"] == 0
    store.mark_false_positive(fs[0].id, "me", "esperado")
    store.sync_findings(fs[:1], {"SQL001"})                       # os outros dois somem → resolvidos
    st = {c["check"]: c for c in store.check_stats()}["SQL001"]
    assert (st["false_positive"], st["resolved"], st["fp_rate"]) == (1, 2, 0.33)
    store.ack(fs[1].id, "me", "ok", until=1.0)
    assert store.findings(None) and all(isinstance(f["evidence"], dict) for f in store.findings(None))


def test_settings_runs_proposals_cache_are_upserts(store):
    store.set_setting("k", {"a": 1})
    store.set_setting("k", {"a": 2})                              # upsert, no duplicate
    assert store.get_setting("k") == {"a": 2} and len(store.settings("k")) == 1
    r1 = Run("airflow", "etl", "r1", "running", 1.0, None, None, "u")
    store.upsert_runs([r1])
    store.upsert_runs([Run("airflow", "etl", "r1", "success", 1.0, 5.0, 4.0, "u")])
    runs = store.runs()
    assert len(runs) == 1 and runs[0]["state"] == "success" and runs[0]["end"] == 5.0
    store.put_proposal("p1", {"x": 1}, "pending")
    store.put_proposal("p1", {"x": 2}, "approved")
    assert store.proposal("p1")["x"] == 2 and store.proposal("p1")["status"] == "approved"
    store.set("c1", {"v": 1})
    store.set("c1", {"v": 2})
    store.flush()
    assert store.get("c1") == {"v": 2}
    store.upsert_ol_job("j", ["a"], ["b"], 1.0)
    store.upsert_ol_job("j", ["a"], ["c"], 2.0)
    store.add_ol_edges("j", [("s", "c1", "d", "c2"), ("s", "c1", "d", "c2")])
    assert store.ol_jobs()[0]["outputs"] == ["c"] and len(store.ol_edges()) == 1


def test_history_samples_audit_notes_ids(store):
    store.add_history("k", 1.0, 2.5)
    store.add_history("k", 2.0, 3.5)
    assert store.history("k") == [(1.0, 2.5), (2.0, 3.5)]
    now = time.time()
    store.add_samples([Sample(now, "host", "h", "memory_bytes", 5.0, 10.0)])
    assert store.recent_samples(now - 1)[0].limit == 10.0
    store.sync_findings([Finding("DBT001", "high", "t", nodes=["a"])], {"DBT001"})
    fid = store.findings("open")[0]["id"]
    store.add_note(fid, "ana", "primeiro")
    store.add_note(fid, "bob", "segundo")
    assert [n["author"] for n in store.notes(fid)] == ["ana", "bob"]
    assert store.audit_log()[-1]["actor"] == "bob"
    assert store.findings("open")[0]["notes"] == 2


def test_team_auth_features_have_identical_semantics(store):
    tok = store.add_user("alice", "admin", email="a@x.com", expires_days=5)
    assert store.user_by_token(tok)["role"] == "admin" and store.user_by_token("if_x") is None
    store.set_project_role("alice", "crm", "none")
    store.set_scopes("alice", "crm", ["models/*"])
    u = store.user_by_token(tok)
    assert u["project_roles"] == {"crm": "none"} and u["scopes"] == {"crm": ["models/*"]}
    sid = store.create_session("alice", "sso", id_token="idt", sub="s", sid="p-1", refresh_token="rt")
    assert store.user_by_session(sid)["via"] == "sso"
    assert store.session_info(sid)["id_token"] == "idt"
    assert store.end_sessions_by_provider("p-1", None) == 1 and store.user_by_session(sid) is None
    sec = store.mfa_begin("alice")
    from intentflow import totp
    assert store.mfa_confirm("alice", totp.code_at(sec, int(time.time() // 30)))
    codes = store.mfa_recovery_codes("alice")
    assert store.mfa_use_recovery("alice", codes[0]) and not store.mfa_use_recovery("alice", codes[0])
    store.attempt_failed("1.2.3.4")
    assert store.attempts_recent("1.2.3.4") == 1
    t2 = store.rotate_token("alice")
    assert store.user_by_token(tok) is None and store.user_by_token(t2)


def test_prune_and_rowcounts(store):
    store.add_history("k", 1.0, 1.0)
    store.add_history("k", time.time(), 1.0)
    assert store.prune(days=1)["history"] == 1


def test_concurrent_writers_do_not_fail(store):
    errs = []

    def w(n):
        try:
            s = Store(store.spec)
            for i in range(25):
                s.add_history(f"k{n}", float(i), float(i))
                s.set_setting(f"w{n}.{i % 3}", i)
        except Exception as e:
            errs.append(repr(e)[:80])
    ts = [threading.Thread(target=w, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == [] and sum(len(store.history(f"k{n}")) for n in range(8)) == 200


@pytest.mark.skipif(not pg_available(), reason="local PostgreSQL unavailable")
def test_migrate_sqlite_to_postgres_and_back_preserves_everything(tmp_path):
    src = Store(new_db(tmp_path, "src.db", "sqlite"))
    tok = src.add_user("alice", "admin")
    src.sync_findings([Finding("DBT001", "high", "t", nodes=["a"], evidence={"owner": "x"})], {"DBT001"})
    fid = src.findings("open")[0]["id"]
    for i in range(3):
        src.add_note(fid, "alice", f"n{i}")
    src.set_setting("policy.max_estimated_rows", 5)
    src.upsert_runs([Run("kestra", "f", "r", "success", 1.0, 2.0, 1.0, "u")])
    src.put_proposal("fix-1", {"summary": "s", "changes": []}, "approved")
    dst = Store(new_db(tmp_path, "dst", "postgres"))
    n = copy_store(src, dst)
    assert n >= 8
    assert dst.user_by_token(tok)["name"] == "alice"                       # the token stays valid
    assert dst.findings("open")[0]["evidence"] == {"owner": "x"} and len(dst.notes(fid)) == 3
    assert dst.get_setting("policy.max_estimated_rows") == 5 and dst.runs()[0]["end"] == 2.0
    dst.add_note(fid, "alice", "n3")                                         # the id sequence continues after the copy
    assert [n["text"] for n in dst.notes(fid)] == ["n0", "n1", "n2", "n3"]
    back = Store(new_db(tmp_path, "back.db", "sqlite"))
    copy_store(dst, back)
    assert back.proposal("fix-1")["status"] == "approved" and back.user_by_token(tok)["role"] == "admin"


@pytest.mark.skipif(not pg_available(), reason="local PostgreSQL unavailable")
def test_env_spec_and_parallel_schema_initialization(tmp_path, monkeypatch):
    dsn = new_db(tmp_path, "e", "postgres")
    monkeypatch.setenv("IF_TEST_DB_URL", dsn)
    s = Store("env:IF_TEST_DB_URL")
    assert s.backend == "postgres" and "env:" in s.spec and s.path == dsn
    s.set_setting("a", 1)
    with pytest.raises(ValueError, match="is not set"):
        Store("env:IF_NAO_EXISTE")
    # 6 processos criando o esquema do MESMO banco novo ao mesmo tempo (advisory lock evita corrida)
    dsn2 = new_db(tmp_path, "p", "postgres")
    code = "import sys; from intentflow.store import Store; s=Store(sys.argv[1]); s.set_setting('p'+sys.argv[2], 1); print('ok')"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    ps = [subprocess.Popen([sys.executable, "-c", code, dsn2, str(i)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
    outs = [p.communicate(timeout=120) + (p.returncode,) for p in ps]
    assert [o[2] for o in outs] == [0] * 6, [o[1][-200:] for o in outs if o[2]]
    assert len(Store(dsn2).settings("p")) == 6


def test_cli_backup_and_migrate_store(tmp_path, capsys):
    from intentflow import cli
    src = new_db(tmp_path, "s.db", "sqlite")
    st = Store(src)
    st.add_user("alice", "admin")
    bk = str(tmp_path / "bk.db")
    assert cli.main(["--store", src, "backup", bk]) == 0
    assert Store(bk).has_users()
    dst = str(tmp_path / "d.db")
    assert cli.main(["migrate-store", src, dst]) == 0
    assert "rows copied" in capsys.readouterr().out
    st.set_setting("x", 1)
    assert cli.main(["migrate-store", src, dst]) == 1                     # destination with data: refused
    assert cli.main(["migrate-store", src, dst, "--overwrite"]) == 0
    assert Store(dst).get_setting("x") == 1


def test_pool_drops_connections_killed_by_a_server_restart(tmp_path):
    """Regression: after the server killed every backend, the next requests kept drawing dead pooled connections."""
    if not pg_available():
        pytest.skip("local PostgreSQL unavailable")
    import psycopg2
    from urllib.parse import urlsplit
    from conftest import PG_ADMIN
    dsn = new_db(tmp_path, backend="postgres")
    stores = [Store(dsn) for _ in range(4)]
    for s in stores:
        s.close()                                   # four idle connections now sit in the pool
    c = psycopg2.connect(PG_ADMIN)
    c.autocommit = True
    c.cursor().execute("select pg_terminate_backend(pid) from pg_stat_activity where datname=%s", (urlsplit(dsn).path[1:],))
    c.close()
    s = Store(dsn)
    assert s.has_users() is False                   # would raise OperationalError with a dead pooled connection
    s.close()
