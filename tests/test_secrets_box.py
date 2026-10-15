import os
import sqlite3
import time

import pytest

pytest.importorskip("cryptography")

from conftest import new_db  # noqa: E402
from intentflow import cli, totp  # noqa: E402
from intentflow.secrets_box import Box  # noqa: E402
from intentflow.store import Store, migrate_secrets  # noqa: E402


def _raw(db, sql, *a):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def test_box_roundtrip_prefix_and_legacy_plaintext(tmp_path, monkeypatch):
    monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)
    b = Box(str(tmp_path / "s.db"))
    assert b.enabled and b.source == "file"
    e = b.enc("JBSWY3DPEHPK3PXP")
    assert e.startswith("enc:v1:") and "JBSWY" not in e and b.dec(e) == "JBSWY3DPEHPK3PXP"
    assert b.enc(e) == e and b.dec("texto-antigo") == "texto-antigo" and b.enc(None) is None and b.enc("") == ""
    kp = tmp_path / "s.db.key"
    assert kp.exists() and oct(kp.stat().st_mode & 0o777) == "0o600"
    assert Box(str(tmp_path / "s.db")).dec(e) == "JBSWY3DPEHPK3PXP"          # same key in a new instance


def test_env_key_wins_and_wrong_key_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("INTENTFLOW_SECRET_KEY", "chave-a-bem-longa-0123456789")
    e = Box(str(tmp_path / "s.db")).enc("segredo")
    assert not (tmp_path / "s.db.key").exists()
    monkeypatch.setenv("INTENTFLOW_SECRET_KEY", "outra-chave-0123456789")
    with pytest.raises(RuntimeError, match="INTENTFLOW_SECRET_KEY"):
        Box(str(tmp_path / "s.db")).dec(e)


def test_memory_store_and_missing_key_do_not_encrypt(monkeypatch):
    monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)
    b = Box(":memory:")
    assert not b.enabled and b.enc("x") == "x" and b.status() == {"encrypted_at_rest": False, "key_source": "none"}
    b = Box("postgresql://u:p@h/db", backend="postgres")
    assert not b.enabled


def test_totp_and_sso_tokens_are_stored_encrypted_and_still_work(tmp_path, monkeypatch):
    monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)
    db = new_db(tmp_path, "t.db", "sqlite")
    st = Store(db)
    st.add_user("alice", "admin")
    secret = st.mfa_begin("alice")
    raw = _raw(db, "select totp_secret from users where name='alice'")[0][0]
    assert raw.startswith("enc:v1:") and secret not in raw
    assert st.mfa_confirm("alice", totp.code_at(secret, int(time.time() // 30)))
    assert st.mfa_check("alice", totp.code_at(secret, int(time.time() // 30) + 1))      # decifra no caminho de login
    sid = st.create_session("alice", "sso", id_token="eyJ.id.tok", refresh_token="rt-secreto", sub="s", sid="p1")
    r = _raw(db, "select id_token, refresh_token from sessions")[0]
    assert all(x.startswith("enc:v1:") for x in r) and "rt-secreto" not in r[1]
    info = st.session_info(sid)
    assert info["id_token"] == "eyJ.id.tok" and info["refresh_token"] == "rt-secreto"
    st.update_session_refresh(sid, "rt-novo", None)
    assert st.session_info(sid)["refresh_token"] == "rt-novo" and st.session_info(sid)["id_token"] == "eyJ.id.tok"


def test_migrate_encrypts_legacy_plaintext_once(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("INTENTFLOW_SECRET_KEY", raising=False)
    db = new_db(tmp_path, "m.db", "sqlite")
    st = Store(db)
    st.add_user("bob", "engineer")
    c = sqlite3.connect(db)
    c.execute("update users set totp_secret='JBSWY3DPEHPK3PXP', totp_enabled=1 where name='bob'")
    c.commit(); c.close()
    st.create_session("bob", "sso", id_token="a.b.c", refresh_token="r")
    c = sqlite3.connect(db); c.execute("update sessions set id_token='a.b.c', refresh_token='r'"); c.commit(); c.close()   # simula legado
    assert migrate_secrets(Store(db)) == {"totp": 1, "sessions": 1}
    assert migrate_secrets(Store(db)) == {"totp": 0, "sessions": 0}
    assert _raw(db, "select totp_secret from users where name='bob'")[0][0].startswith("enc:v1:")
    assert cli.main(["--store", db, "secrets", "status"]) == 0 and '"encrypted_at_rest": true' in capsys.readouterr().out
