"""Real dbt-ol (openlineage-dbt) → receiver. Opt-in: INTENTFLOW_DBT_OL_BIN = the `bin` directory of a venv with dbt-core<1.11, dbt-duckdb and openlineage-dbt."""
import os
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from intentflow import openlineage
from intentflow.store import Store
from conftest import ROOT, new_db

BIN = os.environ.get("INTENTFLOW_DBT_OL_BIN")
pytestmark = pytest.mark.skipif(not (BIN and (Path(BIN) / "dbt-ol").is_file()), reason="dbt-ol unavailable (see docstring)")


def test_real_dbt_ol_events_are_runs_not_external_jobs(tmp_path):
    db = new_db(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), openlineage.make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proj = tmp_path / "shop"
    shutil.copytree(ROOT / "examples/dbt_shop", proj, ignore=shutil.ignore_patterns("target", "logs"))
    env = {**os.environ, "PATH": f"{BIN}:{os.environ['PATH']}", "OPENLINEAGE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
           "OPENLINEAGE_NAMESPACE": "dbt-real"}
    try:
        subprocess.run([str(Path(BIN) / "dbt"), "build", "--profiles-dir", "."], cwd=proj, env=env, capture_output=True, text=True, timeout=300)   # cria o duckdb
        r = subprocess.run([str(Path(BIN) / "dbt-ol"), "build", "--profiles-dir", "."], cwd=proj, env=env, capture_output=True, text=True, timeout=300)
        assert "Emitted" in r.stdout + r.stderr, (r.stdout + r.stderr)[-1500:]
    finally:
        srv.shutdown()
    with Store(db) as st:
        assert len(st.runs()) >= 10                     # dbt runs recorded
        assert st.ol_jobs() == [] and st.ol_edges() == []   # the project already has this lineage
