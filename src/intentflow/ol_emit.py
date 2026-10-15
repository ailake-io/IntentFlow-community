"""OpenLineage emitter: IntentFlow column lineage → RunEvents (DataHub, OpenMetadata, Marquez, Atlan… ingest OpenLineage).

One COMPLETE event per model, with `schema` and `columnLineage` facets (spec 1.x). Idempotent: deterministic runId per model + SQL hash.
"""
from __future__ import annotations

from .i18n import tr

import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

PRODUCER = "https://github.com/intentflow/intentflow"
_SCHEMA = "https://openlineage.io/spec/facets/1-0-0/"


def _ds(project, uid: str) -> dict:
    n = project.nodes[uid]
    name = ".".join(p.strip('"`[]') for p in n.relation.split(".")) if n.relation else n.name
    return {"namespace": project.adapter or "unknown", "name": name}


def _facet(kind: str, version: str, body: dict) -> dict:
    return {"_producer": PRODUCER, "_schemaURL": f"https://openlineage.io/spec/facets/{version}/{kind}Facet.json", **body}


def build_events(project, lineage_result, namespace: str = "intentflow", now: float | None = None) -> list[dict]:
    when = datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")
    by_dst: dict[str, list] = {}
    for e in lineage_result.edges:
        by_dst.setdefault(e.dst, []).append(e)
    events = []
    for uid, n in project.nodes.items():
        if n.resource_type not in ("model", "snapshot") or not n.depends_on:
            continue
        ins = []
        for d in n.depends_on:
            if d not in project.nodes:
                continue
            ds = _ds(project, d)
            cols = project.nodes[d].columns
            if cols:
                ds["facets"] = {"schema": _facet("Schema", "1-1-1", {"fields": [{"name": c.name, "type": c.type or ""} for c in cols.values()]})}
            ins.append(ds)
        out = _ds(project, uid)
        fields: dict[str, dict] = {}
        for e in by_dst.get(uid, []):
            src = _ds(project, e.src)
            tr = {"type": "DIRECT", "subtype": "IDENTITY" if e.kind == "direct" else "TRANSFORMATION", "masking": bool(e.masked)}
            fields.setdefault(e.dst_col, {"inputFields": [], "transformationType": "IDENTITY" if e.kind == "direct" else "TRANSFORMATION"})
            fields[e.dst_col]["inputFields"].append({**src, "field": e.src_col, "transformations": [tr]})
        facets = {}
        if n.columns:
            facets["schema"] = _facet("Schema", "1-1-1", {"fields": [{"name": c.name, "type": c.type or "", **({"description": c.description} if c.description else {})}
                                                                       for c in n.columns.values()]})
        if fields:
            facets["columnLineage"] = _facet("ColumnLineageDataset", "1-2-0", {"fields": fields})
        out["facets"] = facets
        sql_id = (n.compiled_code or n.raw_code or "")
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"intentflow:{uid}:{hash_sql(sql_id)}"))
        events.append({"eventType": "COMPLETE", "eventTime": when, "producer": PRODUCER, "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
                       "run": {"runId": run_id}, "job": {"namespace": namespace, "name": n.name,
                                                         "facets": {"sourceCode": {"_producer": PRODUCER, "_schemaURL": _SCHEMA.replace("facets/1-0-0/", "facets/1-0-1/") + "SourceCodeJobFacet.json",
                                                                                   "language": "sql", "sourceCode": sql_id}} if sql_id else {}},
                       "inputs": ins, "outputs": [out]})
    return events


def hash_sql(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def emit(url: str, events: list[dict], headers: dict | None = None, timeout: float = 15.0) -> dict:
    """POST de cada evento em `{url}/api/v1/lineage` (Marquez/DataHub/OpenMetadata). Retorna contagem e erros."""
    target = url.rstrip("/")
    if not target.endswith("/api/v1/lineage"):
        target += "/api/v1/lineage"
    sent, errors = 0, []
    for ev in events:
        req = urllib.request.Request(target, data=json.dumps(ev).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            urllib.request.urlopen(req, timeout=timeout).read()
            sent += 1
        except urllib.error.HTTPError as e:
            errors.append(f"{ev['job']['name']}: HTTP {e.code}")
        except urllib.error.URLError as e:
            errors.append(f"{ev['job']['name']}: {e.reason}")
            break
    return {"sent": sent, "errors": errors}
