"""REAL OpenLineage emitter: an Airflow (container, openlineage provider) sends events to IntentFlow's receiver.
The container must be set up with an http transport → 127.0.0.1:5055 (see docs/INTEGRATIONS.md)."""
import base64, json, os, threading, time, urllib.request
from http.server import ThreadingHTTPServer

import pytest

from intentflow import openlineage
from intentflow.store import Store

URL = os.environ.get("INTENTFLOW_AIRFLOW_OL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Airflow with OpenLineage not configured")


def _auth_header():
    """Airflow 2: Basic. Airflow 3 (INTENTFLOW_AIRFLOW_OL_API=v2): JWT via /auth/token."""
    if os.environ.get("INTENTFLOW_AIRFLOW_OL_API") == "v2":
        from intentflow.orchestrators.airflow import AirflowClient
        os.environ.pop("AIRFLOW_TOKEN", None)
        return AirflowClient(URL, "v2")._h()
    tok = base64.b64encode(f"{os.environ['AIRFLOW_USER']}:{os.environ['AIRFLOW_PASSWORD']}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


def test_real_airflow_events_reach_the_receiver(tmp_path):
    db = str(tmp_path / "ol.db")
    srv = ThreadingHTTPServer(("127.0.0.1", 5055), openlineage.make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        v2 = os.environ.get("INTENTFLOW_AIRFLOW_OL_API") == "v2"
        for dag in ("it_ol", "it_fail"):
            req = urllib.request.Request(f"{URL}/api/{'v2' if v2 else 'v1'}/dags/{dag}/dagRuns", method="POST",
                                         data=b'{"logical_date": null}' if v2 else b"{}",
                                         headers={**_auth_header(), "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
        deadline = time.time() + 120
        st = Store(db)
        while time.time() < deadline:
            jobs = {r["pipeline"]: r for r in st.runs()}
            if any(p.endswith("it_ol.load") and r["state"] == "success" for p, r in jobs.items()) and \
               any(p.endswith("it_fail.boom") and r["state"] == "failed" for p, r in jobs.items()):
                break
            time.sleep(3)
    finally:
        srv.shutdown()
    st = Store(db)
    runs = {r["pipeline"]: r for r in st.runs()}
    ok = next(r for p, r in runs.items() if p.endswith("it_ol.load"))
    bad = next(r for p, r in runs.items() if p.endswith("it_fail.boom"))
    assert ok["state"] == "success" and ok["duration"] is not None
    assert bad["state"] == "failed"
    if os.environ.get("INTENTFLOW_AIRFLOW_OL_API") == "v2":
        # Airflow 3.1 + openlineage provider 2.7.1: the emitter does NOT send inputs/outputs for hand-declared Assets
        # (checked with real events). States, duration and job names arrive; dataset lineage needs operators
        # with an extractor (e.g. SQL) or the job's native OpenLineage.
        assert any(k.endswith("it_ol.load") for k in {j["job"] for j in st.ol_jobs()} | set(runs))
        return
    io = {j["job"]: j for j in st.ol_jobs()}
    load = next(j for k, j in io.items() if k.endswith("it_ol.load"))
    assert any("public.orders" in x for x in load["inputs"]) and any("mart.sales" in x for x in load["outputs"])
