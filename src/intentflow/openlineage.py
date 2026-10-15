"""OpenLineage input (RunEvent, spec 1.x): runs + inputs/outputs + column lineage.

Covers every OpenLineage emitter (Airflow, Spark, dbt, Flink, Dagster, Kestra) without one connector per tool.
"""
from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .orchestrators import Run
from .store import Store
from .http_utils import RequestError, read_body
from .i18n import tr

STATE = {"START": "running", "RUNNING": "running", "COMPLETE": "success", "FAIL": "failed", "ABORT": "failed", "OTHER": "other"}


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ingest_events(store: Store, events: list[dict]) -> dict:
    runs: dict[tuple[str, str], dict] = {}
    n_edges = 0
    for ev in sorted((e for e in events if isinstance(e, dict) and "run" in e and "job" in e), key=lambda e: e.get("eventTime", "")):
        rid = ev["run"]["runId"]
        job = f"{ev['job'].get('namespace', '')}.{ev['job']['name']}"
        rf = (ev.get("run") or {}).get("facets") or {}
        grouped = False
        by_parent = str(store.get_setting("openlineage.group_by_parent") or "").lower() in ("1", "true", "yes", "sim")
        if ((rf.get("processing_engine") or {}).get("name") == "spark" or by_parent) and isinstance(rf.get("parent"), dict):
            pj = rf["parent"].get("job") or {}        # each Spark action becomes a "job"; what the team recognizes is the application (other engines: opt-in)
            if pj.get("name"):
                job, grouped = f"{pj.get('namespace', '')}.{pj['name']}", True
        t = _ts(ev.get("eventTime"))
        st = STATE.get(ev.get("eventType", "OTHER"), "other")
        key = (job, rid)
        if key not in runs:
            previous = store.con.execute("select start, \"end\", state from runs where system=? and pipeline=? and run_id=?",
                                         ("openlineage", job, rid)).fetchone()
            runs[key] = {"job": job, "start": previous["start"] if previous else None,
                         "end": previous["end"] if previous else None, "state": previous["state"] if previous else st}
        r = runs[key]
        if st == "running" and t is not None:
            r["start"] = min(r["start"], t) if r["start"] is not None else t
        terminal = r["state"] in ("success", "failed")
        if st in ("success", "failed") and (r["end"] is None or (t is not None and t >= r["end"])):
            r["end"], r["state"] = t, st
        elif not terminal:
            r["state"] = st
        # dbt (and IntentFlow's own emitter) is already in the project: storing it as an "external job" would only duplicate the impact
        prod = str(ev.get("producer") or "").lower()
        own = ("/integration/dbt" in prod or "openlineage-dbt" in prod or "github.com/intentflow/" in prod) \
            and str(store.get_setting("openlineage.keep_project_lineage") or "").lower() not in ("1", "true", "yes", "sim")
        ins = [f"{d.get('namespace','')}:{d['name']}" for d in ([] if own else ev.get("inputs", []))]
        outs = [f"{d.get('namespace','')}:{d['name']}" for d in ([] if own else ev.get("outputs", []))]
        if grouped and (ins or outs):             # the application adds up what all its actions read and write
            row = store.con.execute("select inputs, outputs from ol_jobs where job=?", (job,)).fetchone()
            if row:
                ins = sorted(set(ins) | set(json.loads(row["inputs"])))
                outs = sorted(set(outs) | set(json.loads(row["outputs"])))
        if ins or outs:
            store.upsert_ol_job(job, ins, outs, t or 0)
        edges = []
        for o in ([] if own else ev.get("outputs", [])):
            cl = (o.get("facets") or {}).get("columnLineage") or {}
            for col, spec in (cl.get("fields") or {}).items():
                for f in spec.get("inputFields", []):
                    edges.append((f"{f.get('namespace','')}:{f['name']}", f["field"], f"{o.get('namespace','')}:{o['name']}", col))
        if edges:
            store.add_ol_edges(job, edges)
            n_edges += len(edges)
    out = [Run("openlineage", r["job"], rid, r["state"], r["start"], r["end"],
               (r["end"] - r["start"]) if r["end"] is not None and r["start"] is not None else None) for (_, rid), r in runs.items()]
    store.upsert_runs(out)
    return {"runs": len(out), "column_edges": n_edges}


def ingest_file(store: Store, path: str) -> dict:
    with open(path) as source:
        text = source.read().strip()
    try:
        data = json.loads(text)
        events = data if isinstance(data, list) else [data]
    except ValueError:                        # NDJSON
        events = [json.loads(l) for l in text.splitlines() if l.strip()]
    return ingest_events(store, events)


def make_handler(store_path: str):
    class H(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(15)
            super().setup()

        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.path.rstrip("/") != "/api/v1/lineage":     # Marquez-compatible path
                self.send_response(404)
                self.end_headers()
                return
            if self.headers.get("Origin") is not None:
                self.send_response(403)
                self.end_headers()
                return
            try:
                event = json.loads(read_body(self, limit=4 * 1024 * 1024))
                if not isinstance(event, dict) or not isinstance(event.get("run"), dict) or not isinstance(event.get("job"), dict):
                    raise ValueError(tr("invalid OpenLineage event"))
                with Store(store_path) as store:
                    res = ingest_events(store, [event])
                code, body = 200, json.dumps(res)
            except (ValueError, KeyError, TypeError) as e:
                self.close_connection = True
                code = e.status if isinstance(e, RequestError) else 400
                body = json.dumps({"error": str(e)})
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
    return H


def serve(store_path: str, port: int = 5000):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store_path))
    print(tr("OpenLineage receiver: {url}", url=f"http://127.0.0.1:{srv.server_address[1]}/api/v1/lineage"))
    srv.serve_forever()
