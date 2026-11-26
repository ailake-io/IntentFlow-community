import json, threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

from conftest import new_db

from intentflow.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_ui_creates_validates_approves_and_applies_a_fix(shop_target, tmp_path, sandbox_image):
    import shutil
    from intentflow import runner
    from intentflow.web import make_handler
    work = tmp_path / "proj"
    shutil.copytree(shop_target, work, ignore=shutil.ignore_patterns("logs"))
    db = new_db(tmp_path, "f.db")
    st = Store(db)
    st.set_setting("dbt.target", str(work / "target"))
    runner.run_check(st, only=["SQL001"])
    fid = next(f["id"] for f in st.findings("open") if f["fix"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def post(path):
            r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", headers={"X-IntentFlow": "1"}, data=b"{}")
            try:
                return json.loads(urllib.request.urlopen(r, timeout=300).read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        pid = post(f"/api/findings/{fid}/fix")["proposal"]
        assert post(f"/api/proposals/{pid}/apply").get("error")                       # no apply without approval/validation
        assert post(f"/api/proposals/{pid}/approve")["status"] == "approved"
        assert post(f"/api/proposals/{pid}/validate?mode=build")["validation"]["ok"]
        assert post(f"/api/proposals/{pid}/apply")["status"] == "applied"
        assert "select *" not in (work / "models/marts/customers.sql").read_text().lower().split("with orders")[1].split("payments")[0]
        assert post(f"/api/proposals/{pid}/rollback")["status"] == "rolled_back"
    finally:
        srv.shutdown()
