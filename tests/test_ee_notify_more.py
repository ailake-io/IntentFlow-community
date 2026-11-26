"""PR/alert outputs against a local HTTP server that records the request (each API's documented format)."""
import base64, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from intentflow import notify


@pytest.fixture
def hook():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "json": json.loads(self.rfile.read(n) or b"{}")})
            self.send_response(201)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", got
    srv.shutdown()


def test_gitlab_bitbucket_azure_pr_comments(hook, monkeypatch):
    url, got = hook
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-x")
    monkeypatch.setenv("BITBUCKET_TOKEN", "bb-x")
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "pat")
    notify.deliver({"type": "gitlab", "api": url, "project": "grupo/dados", "mr": 12}, "s", "corpo")
    assert got[0]["path"] == "/projects/grupo%2Fdados/merge_requests/12/notes" and got[0]["json"] == {"body": "corpo"}
    assert got[0]["headers"]["private-token"] == "glpat-x"
    notify.deliver({"type": "bitbucket", "api": url, "workspace": "w", "repo": "r", "pr": 3}, "s", "corpo")
    assert got[1]["path"] == "/repositories/w/r/pullrequests/3/comments" and got[1]["json"] == {"content": {"raw": "corpo"}}
    assert got[1]["headers"]["authorization"] == "Bearer bb-x"
    notify.deliver({"type": "azure_devops", "api": url, "org": "o", "project": "p", "repo": "r", "pr": 7}, "s", "corpo")
    assert got[2]["path"].startswith("/o/p/_apis/git/repositories/r/pullRequests/7/threads") and got[2]["json"]["comments"][0]["content"] == "corpo"
    assert base64.b64decode(got[2]["headers"]["authorization"].split()[1]) == b":pat"


def test_pagerduty_opsgenie_jira_teams_payloads(hook, monkeypatch):
    url, got = hook
    for k, v in {"PAGERDUTY_ROUTING_KEY": "rk", "OPSGENIE_API_KEY": "ok", "JIRA_USER": "me@x.com", "JIRA_TOKEN": "jt", "TEAMS_URL": url + "/teams"}.items():
        monkeypatch.setenv(k, v)
    meta = {"id": "inc-abc", "severity": "critical"}
    notify.deliver({"type": "pagerduty", "url": url + "/v2/enqueue"}, "[critical] orders quebrou", "detalhe", meta)
    pd = got[0]["json"]
    assert pd["routing_key"] == "rk" and pd["event_action"] == "trigger" and pd["dedup_key"] == "inc-abc"
    assert pd["payload"]["severity"] == "critical" and pd["payload"]["summary"].startswith("[critical]")
    notify.deliver({"type": "opsgenie", "api": url}, "assunto", "corpo", {"id": "inc-1", "severity": "high"})
    og = got[1]
    assert og["path"] == "/v2/alerts" and og["json"]["priority"] == "P2" and og["json"]["alias"] == "inc-1" and og["headers"]["authorization"] == "GenieKey ok"
    notify.deliver({"type": "jira", "url": url, "project": "DATA", "issue_type": "Bug", "labels": ["intentflow"]}, "resumo", "descrição")
    jr = got[2]
    assert jr["path"] == "/rest/api/2/issue" and jr["json"]["fields"]["project"] == {"key": "DATA"} and jr["json"]["fields"]["issuetype"] == {"name": "Bug"}
    assert base64.b64decode(jr["headers"]["authorization"].split()[1]) == b"me@x.com:jt"
    notify.deliver({"type": "teams", "url_env": "TEAMS_URL"}, "titulo", "texto")
    tm = got[3]["json"]
    assert tm["attachments"][0]["contentType"].endswith("adaptive") and tm["attachments"][0]["content"]["body"][0]["text"] == "titulo"


def test_missing_credentials_fail_clearly(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITLAB_TOKEN"):
        notify.deliver({"type": "gitlab", "project": "a/b", "mr": 1}, "s", "b")
    with pytest.raises(ValueError, match="unknown"):
        notify.deliver({"type": "fax"}, "s", "b")


def test_send_passes_incident_identity_for_dedup(shop_target, hook, monkeypatch, tmp_path):
    from intentflow import dbt_loader, incidents, runner
    from intentflow.store import Store
    url, got = hook
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("dbt.target", str(shop_target if (__import__("pathlib").Path(shop_target) / "manifest.json").exists() else __import__("pathlib").Path(shop_target) / "target"))
    runner.run_check(st, only=["SQL001", "DBT"])
    proj = dbt_loader.load(st.get_setting("dbt.target"))
    incs = incidents.group(st.findings("open"), proj)
    monkeypatch.setenv("PAGERDUTY_ROUTING_KEY", "rk")
    st.set_setting("notify.sinks", [{"name": "pd", "type": "pagerduty", "url": url + "/v2/enqueue"}])
    st.set_setting("notify.routes", [{"sink": "pd", "min_severity": "high"}])
    log = notify.send(st, incs, now=1000.0)
    sent = [e for e in log if e["status"] == "sent"]
    assert sent and len(got) == len(sent)
    assert all(g["json"]["dedup_key"].startswith(("inc-", "root-", "sch", "SCH", "DBT", "sql", "SQL")) or g["json"]["dedup_key"] for g in got)
    assert len({g["json"]["dedup_key"] for g in got}) == len(got)


import os as _os
import urllib.request as _ur


@pytest.mark.skipif(not _os.environ.get("INTENTFLOW_GITLAB_API"), reason="real GitLab not configured")
def test_gitlab_real_merge_request_note_is_created_and_readable(monkeypatch):
    api, proj, mr = _os.environ["INTENTFLOW_GITLAB_API"], _os.environ["INTENTFLOW_GITLAB_PROJECT"], _os.environ["INTENTFLOW_GITLAB_MR"]
    tok = _os.environ["GITLAB_TOKEN"]
    marker = f"IntentFlow teste {__import__('time').time():.0f}"
    notify.deliver({"type": "gitlab", "api": api, "project": proj, "mr": int(mr)}, "assunto", f"**{marker}**\n\n| coluna | impacto |\n|---|---|\n| amount | 3 modelos |")
    req = _ur.Request(f"{api}/projects/{proj}/merge_requests/{mr}/notes?sort=desc&per_page=5", headers={"PRIVATE-TOKEN": tok})
    notes = json.loads(_ur.urlopen(req).read())
    assert any(marker in n["body"] and "| amount | 3 modelos |" in n["body"] for n in notes)
    # a project by path (group/name) also resolves: the `/` is encoded
    namespace = json.loads(_ur.urlopen(_ur.Request(f"{api}/projects/{proj}", headers={"PRIVATE-TOKEN": tok})).read())["path_with_namespace"]
    notify.deliver({"type": "gitlab", "api": api, "project": namespace, "mr": int(mr)}, "s", f"por caminho {marker}")
    notes = json.loads(_ur.urlopen(_ur.Request(f"{api}/projects/{proj}/merge_requests/{mr}/notes?sort=desc&per_page=3", headers={"PRIVATE-TOKEN": tok})).read())
    assert any(f"por caminho {marker}" in n["body"] for n in notes)
