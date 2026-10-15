import json, os, socketserver, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import new_db
import pytest

from intentflow import cli, dbt_loader, incidents, lineage, notify
from intentflow.checks import Context, run_checks
from intentflow.store import Store


class Capture(BaseHTTPRequestHandler):
    log: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Capture.log.append((self.path, dict(self.headers), json.loads(self.rfile.read(n) or b"{}")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture()
def http_sink():
    Capture.log = []
    srv = HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class SMTP(socketserver.StreamRequestHandler):
    mails: list = []

    def handle(self):
        w = lambda s: self.wfile.write((s + "\r\n").encode())   # noqa: E731
        w("220 test")
        data, body = False, []
        for raw in self.rfile:
            line = raw.decode().rstrip("\r\n")
            if data:
                if line == ".":
                    data = False
                    SMTP.mails.append("\n".join(body))
                    w("250 ok")
                else:
                    body.append(line)
                continue
            cmd = line.upper()
            if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                w("250 test")
            elif cmd.startswith("DATA"):
                data = True
                w("354 go")
            elif cmd.startswith("QUIT"):
                w("221 bye")
                return
            else:
                w("250 ok")


def _incidents(shop_target, policies=None):
    p = dbt_loader.load(shop_target / "target", shop_target)
    lin = lineage.build(p)
    st = Store()
    fs = run_checks(Context(project=p, lineage=lin, policies=policies or {}), only=["DBT", "SQL001"])
    for f in fs:
        for n in f.nodes:
            if n in p.nodes and p.nodes[n].meta.get("owner"):
                f.evidence["owner"] = p.nodes[n].meta["owner"]
    st.sync_findings(fs, {"DBT", "SQL001"})
    return st, p, incidents.group(st.findings("open"), p)


def test_incidents_group_by_root_cause(shop_target):
    st, p, incs = _incidents(shop_target)
    assert len(incs) < len(st.findings("open"))                       # menos incidentes que achados
    pii = [i for i in incs if any(f["check_id"] == "DBT004" for f in i.findings)]
    assert len(pii) == 1                                               # customers.email e customer_contacts.email: mesma causa
    assert sum(f["check_id"] == "DBT004" for f in pii[0].findings) == 2


def test_slack_webhook_routing_and_antispam(shop_target, http_sink, monkeypatch):
    st, p, incs = _incidents(shop_target)
    monkeypatch.setenv("SLACK_URL", http_sink + "/hook")
    st.set_setting("notify.sinks", [{"name": "dados", "type": "slack", "url_env": "SLACK_URL"}])
    st.set_setting("notify.routes", [{"sink": "dados", "min_severity": "high"}])
    log = notify.send(st, incs, now=1000.0)
    sent = [e for e in log if e["status"] == "sent"]
    assert sent and len(Capture.log) == len(sent)
    assert "text" in Capture.log[0][2] and "IntentFlow" not in "" and "[" in Capture.log[0][2]["text"]
    again = notify.send(st, incs, now=1000.0 + 60)
    assert all(e["status"] == "suppressed" for e in again)            # anti-spam
    later = notify.send(st, incs, now=1000.0 + 25 * 3600)
    assert any(e["status"] == "sent" for e in later)                  # depois do cooldown


def test_owner_route_only_matches_owner(shop_target, http_sink, monkeypatch):
    st, p, incs = _incidents(shop_target)
    monkeypatch.setenv("HOOK", http_sink)
    st.set_setting("notify.sinks", [{"name": "x", "type": "webhook", "url_env": "HOOK"}])
    st.set_setting("notify.routes", [{"sink": "x", "owner": "outra-equipe@exemplo.com"}])
    assert notify.send(st, incs) == []


def test_email_sink(shop_target, monkeypatch):
    SMTP.mails = []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    st, p, incs = _incidents(shop_target)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(srv.server_address[1]))
    st.set_setting("notify.sinks", [{"name": "mail", "type": "email", "to": "time@exemplo.com"}])
    st.set_setting("notify.routes", [{"sink": "mail"}])
    log = notify.send(st, incs[:1])
    srv.shutdown()
    assert log[0]["status"] == "sent" and "Subject:" in SMTP.mails[0] and "time@exemplo.com" in SMTP.mails[0]


def test_github_pr_comment(shop_target, http_sink, monkeypatch):
    st, p, incs = _incidents(shop_target)
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    st.set_setting("notify.sinks", [{"name": "pr", "type": "github", "repo": "org/repo", "pr": "42", "api": http_sink}])
    st.set_setting("notify.routes", [{"sink": "pr"}])
    notify.send(st, incs[:1])
    path, headers, body = Capture.log[0]
    assert path == "/repos/org/repo/issues/42/comments" and headers["Authorization"] == "Bearer t0ken"
    assert body["body"].startswith("### IntentFlow")


def test_ci_formats_and_gate(shop_target, tmp_path, capsys):
    db = new_db(tmp_path, "ci.db")
    base = ["--store", db, "check", "--target", str(shop_target / "target"), "--only", "DBT"]
    cli.main(base + ["--format", "github"])
    out = capsys.readouterr().out
    assert "::error" in out and "title=DBT004" in out and "::notice" in out
    cli.main(base + ["--format", "markdown"])
    md = capsys.readouterr().out
    assert md.startswith("### IntentFlow") and "incident(s) grouped" in md
    # gate: the pre-run stage without a warehouse finds nothing → passes; a full check with --fail-on high → fails
    assert cli.main(["--store", db, "gate", "--target", str(shop_target / "target"), "--stage", "pre-run"]) == 0
    assert cli.main(base + ["--fail-on", "high"]) == 1


# ---- real SMTP (Mailpit in a container): INTENTFLOW_MAILPIT_PLAIN=host:smtp:api, INTENTFLOW_MAILPIT_TLS=host:smtp:api ----
import json as _json
import urllib.request as _ur

import pytest as _pytest

_PLAIN, _TLS = os.environ.get("INTENTFLOW_MAILPIT_PLAIN"), os.environ.get("INTENTFLOW_MAILPIT_TLS")


def _mailpit(spec):
    host, smtp, api = spec.split(":")
    _ur.urlopen(_ur.Request(f"http://{host}:{api}/api/v1/messages", method="DELETE")).read()
    return host, smtp, f"http://{host}:{api}/api/v1/messages"


def _inbox(url):
    return _json.loads(_ur.urlopen(url).read())["messages"]


@_pytest.mark.skipif(not _PLAIN, reason="real Mailpit not configured")
def test_real_smtp_plain_delivery_and_insecure_login_refused(monkeypatch):
    host, port, api = _mailpit(_PLAIN)
    monkeypatch.setenv("SMTP_HOST", host)
    monkeypatch.setenv("SMTP_PORT", port)
    monkeypatch.delenv("SMTP_USER", raising=False)
    sink = {"type": "email", "to": "time@exemplo.com", "from": "intentflow@exemplo.com"}
    notify.deliver(sink, "[high] teste", "corpo do alerta")
    m = _inbox(api)[0]
    assert m["Subject"] == "[high] teste" and m["To"][0]["Address"] == "time@exemplo.com"
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    with _pytest.raises(RuntimeError, match="without TLS"):                    # never sends a password in plain text by default
        notify.deliver(sink, "x", "y")
    notify.deliver({**sink, "allow_insecure_auth": True}, "com login", "ok")
    assert len(_inbox(api)) == 2


@_pytest.mark.skipif(not _TLS, reason="Mailpit with TLS not configured")
def test_real_smtp_starttls_with_certificate_verification(monkeypatch):
    host, port, api = _mailpit(_TLS)
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_PORT", port)
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    sink = {"type": "email", "to": "a@b.c"}
    with _pytest.raises(Exception, match="CERTIFICATE|certificate"):          # self-signed: verification on by default
        notify.deliver(sink, "x", "y")
    notify.deliver({**sink, "tls": "starttls", "tls_verify": False}, "via starttls", "login em canal criptografado")
    assert _inbox(api)[0]["Subject"] == "via starttls"


# ---- webhook estilo Slack contra Mattermost real: INTENTFLOW_MATTERMOST_HOOK, _CHANNEL, _API, _TOKEN ----
_MM = os.environ.get("INTENTFLOW_MATTERMOST_HOOK")


@_pytest.mark.skipif(not _MM, reason="real Mattermost not configured")
def test_slack_payload_is_accepted_and_rendered_by_a_real_chat_server(shop_target, monkeypatch):
    import time as _t
    st, p, incs = _incidents(shop_target)
    monkeypatch.setenv("MM_HOOK", _MM)
    st.set_setting("notify.sinks", [{"name": "chat", "type": "slack", "url_env": "MM_HOOK"}])
    st.set_setting("notify.routes", [{"sink": "chat", "min_severity": "high"}])
    log = notify.send(st, incs[:2])
    assert log and all(e["status"] == "sent" for e in log)
    api, ch, tok = os.environ["INTENTFLOW_MATTERMOST_API"], os.environ["INTENTFLOW_MATTERMOST_CHANNEL"], os.environ["INTENTFLOW_MATTERMOST_TOKEN"]
    posts = []
    for _ in range(10):
        req = _ur.Request(f"{api}/api/v4/channels/{ch}/posts", headers={"Authorization": f"Bearer {tok}"})
        d = _json.loads(_ur.urlopen(req).read())
        posts = [d["posts"][i]["message"] for i in d["order"]]
        if len(posts) >= len(log):
            break
        _t.sleep(1)
    assert len(posts) >= len(log) and any(incs[0].title.split(":")[0] in m or "IntentFlow" in m or "[" in m for m in posts)


def test_grouping_terminates_with_findings_whose_nodes_are_not_in_the_project(shop_target):
    """Regression: a finding without a project node (unmapped table, topic, lake:...) hung `incidents.group` in an endless loop."""
    from intentflow import dbt_loader
    p = dbt_loader.load(shop_target / "target" if (shop_target / "target").exists() else shop_target)
    real = next(iter(p.nodes))
    fs = [{"id": "DQ001-aaaa", "check_id": "DQ001", "severity": "high", "title": "t1", "nodes": [], "evidence": {}},
          {"id": "KAF002-bbbb", "check_id": "DQ001", "severity": "medium", "title": "t2", "nodes": ["kafka:orders-value"], "evidence": {}},
          {"id": "DBT001-cccc", "check_id": "DQ001", "severity": "low", "title": "t3", "nodes": [real], "evidence": {}}]
    inc = incidents.group(fs, p)
    assert sum(len(i.findings) for i in inc) == 3
