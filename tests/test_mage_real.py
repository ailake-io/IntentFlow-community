"""Real Mage (mageai/mageai container with tests/mage_project mounted at /home/src/proj). Skipped without INTENTFLOW_MAGE_URL.
Mage's own development credentials (admin@admin.com / admin): MAGE_USER/MAGE_PASSWORD."""
import json, os, time, urllib.request

import pytest

from intentflow.orchestrators.more import MageClient

URL = os.environ.get("INTENTFLOW_MAGE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="local Mage not configured")


def _post(path, body, tok):
    body = {"api_key": MageClient.DEFAULT_KEY, **body}
    req = urllib.request.Request(f"{URL}{path}", data=json.dumps(body).encode(), method="POST",
                                headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def test_mage_runs_against_real_server(monkeypatch):
    monkeypatch.setenv("MAGE_USER", os.environ.get("MAGE_USER", "admin@admin.com"))
    monkeypatch.setenv("MAGE_PASSWORD", os.environ.get("MAGE_PASSWORD", "admin"))
    c = MageClient(URL)
    tok = c._token()
    assert tok and len(tok) > 20
    for p in ("it_ok", "it_fail"):
        s = _post(f"/api/pipelines/{p}/pipeline_schedules", {"pipeline_schedule": {"name": f"t_{p}_{int(time.time())}", "schedule_type": "api", "status": "active"}}, tok)
        _post(f"/api/pipeline_schedules/{s['pipeline_schedule']['id']}/pipeline_runs", {"pipeline_run": {"variables": {}}}, tok)
    deadline, got = time.time() + 90, set()
    while time.time() < deadline:
        got = {(r.pipeline, r.state) for r in c.runs()}
        if ("it_ok", "success") in got and ("it_fail", "failed") in got:
            break
        time.sleep(3)
    assert ("it_ok", "success") in got and ("it_fail", "failed") in got, got
    done = [r for r in c.runs() if r.state in ("success", "failed")]
    assert done and all(r.duration is not None and 0 <= r.duration < 300 for r in done)       # correct UTC: no absurd durations


def test_wrong_credentials_give_clear_error(monkeypatch):
    monkeypatch.setenv("MAGE_USER", "x@y.z")
    monkeypatch.setenv("MAGE_PASSWORD", "errada")
    with pytest.raises(RuntimeError, match="login no Mage falhou|recusou"):
        MageClient(URL).runs()
