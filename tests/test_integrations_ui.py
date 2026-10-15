import json, re, shutil, subprocess
from pathlib import Path

from intentflow import integrations
from intentflow.model import Finding
from intentflow.monitoring import Sample
from intentflow.orchestrators import Run
from intentflow.store import Store

HTML = Path(__file__).resolve().parent.parent / "src/intentflow/web.html"


def _cards(r):
    return {c["id"]: c for g in r["groups"] for c in g["cards"]}


def test_nothing_configured_everything_off_except_store(tmp_path):
    r = integrations.build(Store(str(tmp_path / "s.db")))
    c = _cards(r)
    assert r["configured"] == 2 and c["store"]["configured"] and c["secrets"]["configured"]       # store + encryption through a key file
    assert all(c[k]["state"] == "off" for k in ("airflow", "kafka", "kubernetes", "notify", "warehouse", "glue", "adf"))
    assert r["warnings"] == 0


def test_configured_integrations_report_data_and_warnings(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("airflow.url", "http://af")
    st.set_setting("flink.url", "http://fl")
    st.set_setting("kubernetes.enabled", True)
    st.set_setting("kafka.bootstrap", "k:9092")
    st.set_setting("notify.sinks", [{"name": "pd", "type": "pagerduty"}])
    st.upsert_runs([Run("airflow", "etl", "r1", "failed", 100.0, 160.0, 60.0, "u"), Run("airflow", "etl", "r2", "success", 200.0, 230.0, 30.0, "u")])
    import time
    now = time.time()
    st.add_samples([Sample(now, "kubernetes", "ns/p/c", "oom_killed", 1.0), Sample(now, "kafka", "g:t", "consumer_lag", 5000.0)])
    c = _cards(integrations.build(st, now=now))
    assert c["airflow"]["state"] == "ok" and "2 run(s), 1 failed" in c["airflow"]["summary"]
    assert c["flink"]["state"] == "warn" and "no runs collected" in c["flink"]["summary"]
    assert c["kubernetes"]["state"] == "warn" and "1 OOM" in c["kubernetes"]["summary"]
    assert "max lag 5000" in c["kafka"]["summary"]
    assert c["notify"]["state"] == "warn" and "pd (pagerduty)" in c["notify"]["summary"]         # sink sem rota
    st.set_setting("notify.routes", [{"sink": "pd"}])
    assert _cards(integrations.build(st))["notify"]["state"] == "ok"


def test_ui_has_integrations_page_and_valid_js(tmp_path):
    h = HTML.read_text()
    for marker in ("integrationsPage", 'T("Available to turn on")', "'/api/integrations'", "['integrations',T(\"Integrations\")]"):
        assert marker in h, marker
    if shutil.which("node"):
        js = tmp_path / "ui.js"
        js.write_text(re.search(r"<script>(.*)</script>", h, re.S).group(1))
        assert subprocess.run(["node", "--check", str(js)], capture_output=True).returncode == 0
