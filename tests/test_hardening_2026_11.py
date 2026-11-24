"""Schema version guard, SIGTERM exit, HTTP retries, access log, API rate limit, secret key rotation."""
import json
import os
import signal
import urllib.error

import pytest

from intentflow import cli, http_utils
from intentflow.store import Store, rotate_secrets


def test_newer_schema_is_refused(tmp_path):
    p = str(tmp_path / "s.db")
    with Store(p) as st:
        st.con.execute("update schema_version set version=9999")
        st.con.commit()
    from intentflow import store as store_mod
    store_mod._INITIALIZED.discard(os.path.abspath(p))
    with pytest.raises(SystemExit, match="newer"):
        Store(p)


def test_sigterm_unwinds_like_ctrl_c(monkeypatch):
    def fake(a):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("not interrupted")
    monkeypatch.setattr(cli, "cmd_watch", fake)
    prev = signal.getsignal(signal.SIGTERM)
    assert cli.main(["--store", ":memory:", "watch"]) == 130
    assert signal.getsignal(signal.SIGTERM) is prev


def test_urlopen_retries_transient_only(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def flaky(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError("u", 503, "busy", {}, None)
        return "ok"
    monkeypatch.setattr("urllib.request.urlopen", flaky)
    assert http_utils.urlopen("u", 1) == "ok" and len(calls) == 3

    calls.clear()

    def forbidden(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError("u", 403, "no", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        http_utils.urlopen("u", 1)
    assert len(calls) == 1


def test_access_log_json_drops_query(monkeypatch, capsys):
    monkeypatch.setenv("INTENTFLOW_ACCESS_LOG", "json")

    class H:
        client_address = ("10.0.0.1", 1)
        command = "GET"
        path = "/api/me?token=secret"
    http_utils.access_log(H(), 200)
    rec = json.loads(capsys.readouterr().err)
    assert rec["path"] == "/api/me" and rec["status"] == 200 and "secret" not in json.dumps(rec)


def test_api_rate_limit_per_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("INTENTFLOW_API_RATE_PER_MIN", "2")
    from intentflow import web
    H = web.make_handler(str(tmp_path / "s.db"))
    sent = []

    class Fake(H):
        def __init__(self, ip):
            self.path, self._ip = "/api/me", ip

        def _client_ip(self):
            return self._ip

        def _json(self, code, obj, cookies=()):
            sent.append(code)
    assert Fake("10.0.0.1")._rate_ok() and Fake("10.0.0.1")._rate_ok()
    assert not Fake("10.0.0.1")._rate_ok() and sent == [429]
    absolute = Fake("10.0.0.1")
    absolute.path = "http://evil/api/me"            # absolute-form target routes to /api/me: limited too
    assert not absolute._rate_ok()
    assert Fake("10.0.0.2")._rate_ok()            # other client unaffected
    assert all(Fake("127.0.0.1")._rate_ok() for _ in range(5))   # loopback exempt


def test_secret_key_rotation(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    p = str(tmp_path / "s.db")
    monkeypatch.setenv("INTENTFLOW_SECRET_KEY", "old-key-" + "x" * 32)
    with Store(p) as st:
        st.add_user("ana", "admin")
        st.con.execute("update users set totp_secret=? where name='ana'", (st.box.enc("TOTPSEED"),))
        st.con.commit()
    monkeypatch.setenv("INTENTFLOW_SECRET_KEY", "new-key-" + "y" * 32)
    monkeypatch.setenv("INTENTFLOW_SECRET_KEY_OLD", "old-key-" + "x" * 32)
    with Store(p) as st:
        assert rotate_secrets(st)["totp"] == 1
    monkeypatch.delenv("INTENTFLOW_SECRET_KEY_OLD")
    with Store(p) as st:                       # readable with the new key alone
        raw = st.con.execute("select totp_secret from users where name='ana'").fetchone()[0]
        assert st.box.dec(raw) == "TOTPSEED"


def test_audit_mirrors_to_stderr_as_json(monkeypatch, capsys):
    monkeypatch.setenv("INTENTFLOW_AUDIT_LOG", "json")
    with Store(":memory:") as st:
        st.audit("system", "watch_error", {"error": "boom"})
    rec = json.loads(capsys.readouterr().err)
    assert rec["event"] == "watch_error" and rec["detail"] == {"error": "boom"}
