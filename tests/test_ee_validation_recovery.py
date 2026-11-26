"""Isolamento local e falhas reais/simuladas de escrita; nenhuma conta externa."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from test_review_regressions import local_api  # noqa: F401 — fixture compartilhada

from intentflow.enterprise import dbt_sandbox, proposals
from intentflow.enterprise import file_transaction as ft
from intentflow.store import Store


def proposal_project(tmp_path, store, *, count=3):
    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: review\nversion: '1.0'\nprofile: unsafe_original\n")
    (project / "profiles.yml").write_text("credentials: must-not-be-copied\n")
    (project / "models").mkdir()
    changes = []
    for i in range(count):
        f = project / "models" / f"m{i}.sql"
        f.write_bytes(b"select 1 as a\r\n")
        f.chmod(0o640)
        changes.append({"file": str(f), "before_hash": proposals._h(f.read_text()), "after": "select 2 as a\n"})
    p = {"id": "recoverable", "changes": changes, "created_by": "author", "project_root": str(project), "summary": "review"}
    p["approval"] = {"user": "reviewer", "change_hash": proposals._change_hash(p)}
    p["validation"] = {"ok": True, "mode": "build", "change_hash": proposals._change_hash(p), "isolation": dbt_sandbox.ISOLATION}
    store.put_proposal(p["id"], p, "approved")
    return p, project


def contents(p):
    return [Path(c["file"]).read_bytes() for c in p["changes"]]


def test_apply_and_rollback_preserve_bytes_permissions_and_clean_journal(tmp_path):
    with Store(str(tmp_path / "store.db")) as store:
        p, root = proposal_project(tmp_path, store)
        assert proposals.apply(store, p["id"])["status"] == "applied"
        assert contents(p) == [b"select 2 as a\n"] * 3
        assert all(Path(c["file"]).stat().st_mode & 0o777 == 0o640 for c in p["changes"])
        assert proposals.rollback(store, p["id"])["status"] == "rolled_back"
        assert contents(p) == [b"select 1 as a\r\n"] * 3
        assert not (root / ft.DIRECTORY / "active.json").exists()


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_write_failure_restores_entire_batch(tmp_path, monkeypatch, operation):
    with Store(str(tmp_path / "store.db")) as store:
        p, root = proposal_project(tmp_path, store)
        if operation == "rollback":
            proposals.apply(store, p["id"])
        before = contents(p)
        status = store.proposal(p["id"])["status"]
        original, n = ft.Journal.write, 0
        def fail_second(self, *args):
            nonlocal n
            n += 1
            if n == 2:
                raise OSError("simulated disk error")
            return original(self, *args)
        monkeypatch.setattr(ft.Journal, "write", fail_second)
        with pytest.raises(ValueError, match="restored"):
            getattr(proposals, operation)(store, p["id"])
        assert contents(p) == before
        assert store.proposal(p["id"])["status"] == status
        assert not (root / ft.DIRECTORY / "active.json").exists()


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_process_exit_mid_batch_is_recoverable_after_reopening(tmp_path, operation):
    db = str(tmp_path / "store.db")
    with Store(db) as store:
        p, root = proposal_project(tmp_path, store)
        if operation == "rollback":
            proposals.apply(store, p["id"])
        before = contents(p)
        status = store.proposal(p["id"])["status"]
    script = '''
import os,sys
from intentflow.enterprise import proposals, file_transaction as ft
from intentflow.store import Store
original = ft.Journal.write
n = 0
def crash(self, *args):
    global n
    original(self, *args)
    n += 1
    if n == 1: os._exit(71)
ft.Journal.write = crash
with Store(sys.argv[1]) as store:
    getattr(proposals,sys.argv[2])(store,'recoverable')
'''
    done = subprocess.run([sys.executable, "-c", script, db, operation], timeout=15)
    assert done.returncode == 71
    assert contents(p) != before
    assert (root / ft.DIRECTORY / "active.json").exists()
    with Store(db) as store:
        assert proposals.recover(store, p["id"])["status"] == status
        assert contents(p) == before
        assert proposals.recover(store, p["id"])["status"] == status  # idempotente


def test_recovery_preserves_later_edits_and_blocks_other_operations(tmp_path, monkeypatch):
    with Store(str(tmp_path / "store.db")) as store:
        p, root = proposal_project(tmp_path, store)
        original = ft.Journal.write
        def interrupt(self, *args):
            original(self, *args)
            raise SystemExit("process interrupted")
        with patch.object(ft.Journal, "write", interrupt), pytest.raises(SystemExit):
            proposals.apply(store, p["id"])
        Path(p["changes"][1]["file"]).write_text("human edit")
        before = contents(p)
        with pytest.raises(ValueError, match="preserve"):
            proposals.recover(store, p["id"])
        assert contents(p) == before
        with pytest.raises(ValueError, match="interrupted operation"):
            proposals.apply(store, p["id"])
        assert (root / ft.DIRECTORY / "active.json").exists()


def test_committed_operation_only_needs_journal_cleanup(tmp_path):
    with Store(str(tmp_path / "store.db")) as store:
        p, root = proposal_project(tmp_path, store)
        with patch.object(ft.Journal, "clear", side_effect=SystemExit("crash after commit")), pytest.raises(SystemExit):
            proposals.apply(store, p["id"])
        assert store.proposal(p["id"])["status"] == "applied"
        assert proposals.recover(store, p["id"])["status"] == "applied"
        assert contents(p) == [b"select 2 as a\n"] * 3
        assert not (root / ft.DIRECTORY / "active.json").exists()


def test_lock_excludes_other_instances_and_wrong_store_cannot_recover(tmp_path):
    with Store(str(tmp_path / "store.db")) as store:
        p, _ = proposal_project(tmp_path, store)
        with ft.locked(p), pytest.raises(ValueError, match="in progress"):
            proposals.apply(store, p["id"])
        with patch.object(ft.Journal, "write", side_effect=SystemExit), pytest.raises(SystemExit):
            proposals.apply(store, p["id"])
        with Store(str(tmp_path / "other.db")) as other:
            other.put_proposal(p["id"], p, "approved")
            with pytest.raises(ValueError, match="another store"):
                proposals.recover(other, p["id"])


def test_legacy_unisolated_validation_is_rejected(tmp_path):
    with Store() as store:
        p, _ = proposal_project(tmp_path, store)
        del p["validation"]["isolation"]
        store.put_proposal(p["id"], p, "approved")
        with pytest.raises(ValueError, match="without isolation"):
            proposals.apply(store, p["id"])
        assert contents(p) == [b"select 1 as a\r\n"] * 3


def test_stage_excludes_credentials_and_rejects_escape(tmp_path):
    with Store() as store:
        p, root = proposal_project(tmp_path, store)
        (root / ".env").write_text("SECRET=secret")
        (root / "models" / "credential.key").write_text("private")
        selectors = dbt_sandbox.stage(root, tmp_path / "staged", p["changes"])
        assert selectors[0] == "+path:models/m0.sql+"
        assert not (tmp_path / "staged/profiles.yml").exists()
        assert not (tmp_path / "staged/.env").exists()
        assert not (tmp_path / "staged/models/credential.key").exists()
        (root / "models/link.sql").symlink_to(tmp_path / "secret.sql")
        (tmp_path / "secret.sql").write_text("secret")
        with pytest.raises(ValueError, match="symlink"):
            dbt_sandbox.stage(root, tmp_path / "rejected", p["changes"])


def test_missing_docker_fails_closed_without_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(dbt_sandbox.shutil, "which", lambda _: None)
    with patch.object(dbt_sandbox.subprocess, "Popen") as popen:
        result = dbt_sandbox.run(tmp_path, [], "parse")
    assert not result["ok"] and "Docker" in result["output"]
    popen.assert_not_called()


def test_docker_flags_environment_timeout_and_cleanup(tmp_path, monkeypatch):
    with Store() as store:
        p, root = proposal_project(tmp_path, store)
        monkeypatch.setattr(dbt_sandbox.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(dbt_sandbox.stat, "S_ISSOCK", lambda _: True)
        monkeypatch.setenv("INTENTFLOW_DBT_DOCKER_SOCKET", str(root))
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-inherit")
        called = []
        def run(command, **kwargs):
            called.append(command)
            return subprocess.CompletedProcess(command, 0, "sha256:" + "a" * 64, "")
        def timeout(command, env, seconds):
            assert {"--network=none", "--read-only", "--pull=never", "--cap-drop=ALL", "--pids-limit=128"} <= set(command)
            assert "--user=10001:10001" in command and "--security-opt=no-new-privileges:true" in command
            assert "AWS_SECRET_ACCESS_KEY" not in env and "INTENTFLOW_SECRET_KEY" not in env
            assert "/work/profiles" in command[command.index("-c") + 1]
            raise subprocess.TimeoutExpired(command, seconds)
        monkeypatch.setattr(dbt_sandbox.subprocess, "run", run)
        monkeypatch.setattr(dbt_sandbox, "_run_bounded", timeout)
        result = dbt_sandbox.run(root, p["changes"], "build")
        assert not result["ok"] and "600 seconds" in result["output"]
        assert any("rm" in command and "-f" in command for command in called)


def test_real_container_isolation(tmp_path, monkeypatch, sandbox_image):
    with Store() as store:
        p, root = proposal_project(tmp_path, store)
        secret = tmp_path / "host-secret"
        secret.write_text("must-not-read")
        monkeypatch.setenv("REVIEW_SECRET", "must-not-inherit")
        script = '''
import os,json,socket
flags={"uid":os.getuid(),"secret":os.environ.get("REVIEW_SECRET"),"host_file":os.path.exists(%r)}
try:
    open('/etc/intentflow-probe','w').write('x')
    flags['root_writable']=True
except OSError: flags['root_writable']=False
try:
    socket.create_connection(('192.0.2.1',80),timeout=1)
    flags['network']=True
except OSError: flags['network']=False
flags['input_writable']=os.access('/input',os.W_OK)
print(json.dumps(flags))
''' % str(secret)
        monkeypatch.setattr(dbt_sandbox, "_ENTRY", script)
        result = dbt_sandbox.run(root, p["changes"], "parse")
        assert result["ok"], result["output"]
        assert json.loads(result["output"]) == {"uid":10001,"secret":None,"host_file":False,"root_writable":False,"network":False,"input_writable":False}


def test_real_dbt_uses_generated_profile_and_does_not_inherit_secrets(tmp_path, monkeypatch, sandbox_image):
    with Store() as store:
        p, root = proposal_project(tmp_path, store, count=1)
        monkeypatch.setenv("REVIEW_SECRET", "must-not-inherit")
        p["changes"][0]["after"] = "{% if env_var('REVIEW_SECRET', 'absent') != 'absent' %}{{ exceptions.raise_compiler_error('secret leaked') }}{% endif %}\nselect 2 as a\n"
        result = dbt_sandbox.run(root, p["changes"], "build")
        assert result["ok"], result["output"]
        assert contents(p) == [b"select 1 as a\r\n"]
        assert not list(root.glob("*.duckdb"))


def test_journal_failure_never_changes_source_files(tmp_path):
    with Store(str(tmp_path / "store.db")) as store:
        p, _ = proposal_project(tmp_path, store)
        with patch.object(ft.Journal, "save", side_effect=OSError("disk full")), pytest.raises(ValueError):
            proposals.apply(store, p["id"])
        assert contents(p) == [b"select 1 as a\r\n"] * 3
        assert store.proposal(p["id"])["status"] == "approved"


def test_failure_to_commit_database_restores_files(tmp_path, monkeypatch):
    with Store(str(tmp_path / "store.db")) as store:
        p, _ = proposal_project(tmp_path, store)
        original = store.put_proposal
        def fail_final(pid, data, status):
            if status == "applied":
                raise OSError("database unavailable at commit")
            return original(pid, data, status)
        monkeypatch.setattr(store, "put_proposal", fail_final)
        with pytest.raises(ValueError, match="restored"):
            proposals.apply(store, p["id"])
        assert contents(p) == [b"select 1 as a\r\n"] * 3
        assert store.proposal(p["id"])["status"] == "approved"


def test_failure_after_database_commit_keeps_committed_files(tmp_path, monkeypatch):
    with Store(str(tmp_path / "store.db")) as store:
        p, _ = proposal_project(tmp_path, store)
        original = store.put_proposal
        def fail_after_commit(pid, data, status):
            original(pid, data, status)
            if status == "applied":
                raise OSError("connection lost after commit")
        monkeypatch.setattr(store, "put_proposal", fail_after_commit)
        assert proposals.apply(store, p["id"])["status"] == "applied"
        assert contents(p) == [b"select 2 as a\n"] * 3


def test_unknown_file_content_blocks_entire_rollback(tmp_path):
    with Store(str(tmp_path / "store.db")) as store:
        p, _ = proposal_project(tmp_path, store)
        proposals.apply(store, p["id"])
        Path(p["changes"][2]["file"]).write_text("new human edit")
        before = contents(p)
        with pytest.raises(ValueError, match="preserve"):
            proposals.rollback(store, p["id"])
        assert contents(p) == before


def test_staging_detects_edited_input_and_parent_path_escape(tmp_path):
    with Store() as store:
        p, root = proposal_project(tmp_path, store)
        Path(p["changes"][0]["file"]).write_text("changed after proposal")
        with pytest.raises(ValueError, match="changed during"):
            dbt_sandbox.stage(root, tmp_path / "changed", p["changes"])
        (root / "dbt_project.yml").write_text("name: review\nmodel-paths: ['../outside']\n")
        with pytest.raises(ValueError, match="inside the project"):
            dbt_sandbox.stage(root, tmp_path / "outside-stage", p["changes"])


def test_staging_refuses_a_transaction_in_progress_or_recovery_pending(tmp_path):
    with Store(str(tmp_path / "store.db")) as store:
        p, root = proposal_project(tmp_path, store)
        with ft.locked(p), pytest.raises(ValueError, match="another operation is in progress"):
            dbt_sandbox.stage(root, tmp_path / "busy-stage", p["changes"])
        with ft.locked(p) as journal:
            journal.save({"version": 1, "id": "interrupted"})
        with pytest.raises(ValueError, match="interrupted operation"):
            dbt_sandbox.stage(root, tmp_path / "interrupted-stage", p["changes"])


def test_cli_and_http_expose_recovery_and_require_engineer(tmp_path, local_api, monkeypatch):
    from intentflow import cli
    path, call = local_api
    with Store(path) as store:
        p, _ = proposal_project(tmp_path, store)
        engineer = store.add_user("engineer", "engineer")
        viewer = store.add_user("viewer", "viewer")
        with patch.object(ft.Journal, "write", side_effect=SystemExit), pytest.raises(SystemExit):
            proposals.apply(store, p["id"])
    assert call("POST", "/api/proposals/recoverable/recover", token=viewer)[0] == 403
    code, data = call("POST", "/api/proposals/recoverable/recover", token=engineer)
    assert code == 200 and data["status"] == "approved"
    assert cli.main(["--store", path, "fix", "recover", p["id"], "--user", "engineer"]) == 0


def test_staging_works_with_private_host_umask(tmp_path):
    with Store() as store:
        p, root = proposal_project(tmp_path, store)
        previous = os.umask(0o077)
        try:
            destination = tmp_path / "private-umask"
            dbt_sandbox.stage(root, destination, p["changes"])
        finally:
            os.umask(previous)
        assert destination.stat().st_mode & 0o777 == 0o755
        assert (destination / "models").stat().st_mode & 0o777 == 0o755
        assert (destination / "models/m0.sql").stat().st_mode & 0o777 == 0o444


def test_source_edit_between_preflight_and_journal_is_preserved(tmp_path, monkeypatch):
    with Store(str(tmp_path / "store.db")) as store:
        p, _ = proposal_project(tmp_path, store)
        original = ft.execute
        def edit_then_execute(*args, **kwargs):
            Path(p["changes"][0]["file"]).write_text("concurrent edit")
            return original(*args, **kwargs)
        monkeypatch.setattr(ft, "execute", edit_then_execute)
        with pytest.raises(ValueError, match="before preparing"):
            proposals.apply(store, p["id"])
        assert contents(p) == [b"concurrent edit", b"select 1 as a\r\n", b"select 1 as a\r\n"]
        assert store.proposal(p["id"])["status"] == "approved"


def test_symlink_swap_never_writes_outside_project(tmp_path):
    with Store(str(tmp_path / "store.db")) as store:
        p, _ = proposal_project(tmp_path, store)
        outside = tmp_path / "outside.sql"
        outside.write_text("external")
        target = Path(p["changes"][0]["file"])
        target.unlink()
        target.symlink_to(outside)
        with pytest.raises(ValueError, match="symlink"):
            proposals.apply(store, p["id"])
        assert outside.read_text() == "external"


def test_real_build_skips_unbuildable_cross_branch_test_but_runs_selected_tests(tmp_path, sandbox_image):
    """An unrelated sibling must not break a valid selected model's offline build."""
    import hashlib
    root = tmp_path / 'project'
    for directory in ('models', 'seeds', 'tests'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / 'dbt_project.yml').write_text("name: branch_tests\nversion: '1.0'\nprofile: original\n")
    (root / 'seeds/input.csv').write_text('id\n1\n2\n')
    (root / 'models/base.sql').write_text("select id from {{ ref('input') }}\n")
    (root / 'models/sibling.sql').write_text("select id from {{ ref('base') }}\n")
    selected = root / 'models/selected.sql'
    before = "select * from {{ ref('base') }}\n"
    selected.write_text(before)
    (root / 'tests/cross_branch.sql').write_text("select b.id from {{ ref('base') }} b left join {{ ref('sibling') }} s using (id) where s.id is null\n")
    (root / 'tests/selected_nonnegative.sql').write_text("select id from {{ ref('selected') }} where id < 0\n")
    changes = [{'file': str(selected), 'before_hash': hashlib.sha256(before.encode()).hexdigest()[:16],
                'after': "select id from {{ ref('base') }}\n"}]
    good = dbt_sandbox.run(root, changes, 'build')
    assert good['ok'], good['output']
    changes[0]['after'] = "select -id as id from {{ ref('base') }}\n"
    bad = dbt_sandbox.run(root, changes, 'build')
    assert not bad['ok'] and 'selected_nonnegative' in bad['output'], bad
    assert selected.read_text() == before
