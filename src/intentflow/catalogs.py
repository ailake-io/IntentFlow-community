"""Data catalogs (AWS Glue Data Catalog, Databricks Unity Catalog): a snapshot of table schemas, without going through dbt.

`collect(store)` writes `state.catalog.cur` and, when the snapshot changes, moves the previous one to `state.catalog.prev`; check CAT001 compares both.
Config: `catalog.glue` (true; uses `aws.region`), `catalog.unity` (workspace host; token in DATABRICKS_TOKEN),
`catalog.unity_catalogs` (list/text with the catalogs; empty = all), `catalog.include` (`db.` / `catalog.schema.` prefixes to watch).
"""
from __future__ import annotations

from .i18n import tr

import os
import time

from .orchestrators import http_json


class GlueCatalog:
    def __init__(self, region=None, endpoint_url=None, boto=None):
        if boto is None:
            from .orchestrators.aws import client
            boto = client("glue", region, endpoint_url)
        self.c = boto

    def snapshot(self, include: list[str] | None = None) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for dbs in self.c.get_paginator("get_databases").paginate():
            for d in dbs.get("DatabaseList", []):
                if include and not any(f"{d['Name']}.".startswith(i) or i.startswith(d["Name"] + ".") for i in include):
                    continue
                for ts in self.c.get_paginator("get_tables").paginate(DatabaseName=d["Name"]):
                    for t in ts.get("TableList", []):
                        cols = (t.get("StorageDescriptor") or {}).get("Columns", []) + t.get("PartitionKeys", [])
                        out[f"{d['Name']}.{t['Name']}".lower()] = {c["Name"].lower(): c.get("Type", "") for c in cols}
        return out


class UnityCatalog:
    def __init__(self, host: str, fetch=http_json):
        h = host.rstrip("/")
        self.base = h if "://" in h else f"https://{h}"
        self.fetch = fetch

    def _h(self) -> dict:
        t = os.environ.get("DATABRICKS_TOKEN")
        return {"Authorization": f"Bearer {t}"} if t else {}

    def _get(self, path: str):
        return self.fetch(f"{self.base}/api/2.1/unity-catalog{path}", self._h()) or {}

    def snapshot(self, catalogs: list[str] | None = None, include: list[str] | None = None) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        cats = catalogs or [c["name"] for c in self._get("/catalogs").get("catalogs", [])]
        for c in cats:
            for s in self._get(f"/schemas?catalog_name={c}").get("schemas", []):
                prefix = f"{c}.{s['name']}."
                if include and not any(prefix.startswith(i) or i.startswith(prefix) for i in include):
                    continue
                token = None
                while True:
                    d = self._get(f"/tables?catalog_name={c}&schema_name={s['name']}" + (f"&page_token={token}" if token else ""))
                    for t in d.get("tables", []):
                        out[f"{c}.{s['name']}.{t['name']}".lower()] = {x["name"].lower(): x.get("type_text") or x.get("type_name", "")
                                                                         for x in t.get("columns", [])}
                    token = d.get("next_page_token")
                    if not token:
                        break
        return out


def _list(v) -> list[str]:
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",")]
    return [str(x) for x in (v or []) if str(x).strip()]


def collect(store, glue=None, unity=None, now: float | None = None) -> dict:
    res = {"tables": 0, "changed": False, "errors": []}
    snap: dict[str, dict[str, str]] = {}
    include = _list(store.get_setting("catalog.include")) or None
    if store.get_setting("catalog.glue"):
        try:
            g = glue or GlueCatalog(store.get_setting("aws.region"), store.get_setting("aws.endpoint_url"))
            snap.update({f"glue:{k}": v for k, v in g.snapshot(include).items()})
        except Exception as e:
            res["errors"].append(f"glue: {str(e)[:150]}")
    if store.get_setting("catalog.unity"):
        try:
            u = unity or UnityCatalog(store.get_setting("catalog.unity"))
            snap.update({f"unity:{k}": v for k, v in u.snapshot(_list(store.get_setting("catalog.unity_catalogs")) or None, include).items()})
        except Exception as e:
            res["errors"].append(f"unity: {str(e)[:150]}")
    if not snap:
        return res
    cur = store.get_setting("state.catalog.cur")
    if cur is not None and cur != snap:                 # changed: the current snapshot becomes the previous one
        store.set_setting("state.catalog.prev", cur)
        res["changed"] = True
    store.set_setting("state.catalog.cur", snap)
    store.set_setting("state.catalog.ts", now or time.time())
    res["tables"] = len(snap)
    return res
