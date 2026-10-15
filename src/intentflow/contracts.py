"""Data contracts (Data Contract Specification `models:` and ODCS v3 `schema:`) → {model: {column: rules}}.

We read the contract the team already maintains; IntentFlow defines no format of its own. Configuration: `contracts.dir` (folder or file).
"""
from __future__ import annotations

from .i18n import tr

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FAMILIES = {
    "string": {"string", "text", "varchar", "char", "character varying", "str", "uuid"},
    "integer": {"int", "integer", "bigint", "long", "smallint", "tinyint", "int32", "int64", "hugeint"},
    "number": {"float", "double", "decimal", "numeric", "number", "real", "float32", "float64", "money"},
    "boolean": {"boolean", "bool"},
    "date": {"date"},
    "timestamp": {"timestamp", "timestamp_ntz", "timestamp_tz", "timestamptz", "datetime", "timestamp without time zone", "timestamp with time zone"},
}


def family(t: str | None) -> str | None:
    if not t:
        return None
    base = str(t).lower().split("(")[0].strip()
    for fam, names in FAMILIES.items():
        if base in names or (fam == "timestamp" and base.startswith("timestamp")):
            return fam
    return None


@dataclass
class ColumnRule:
    name: str
    type: str | None = None
    required: bool = False
    unique: bool = False
    pii: bool = False
    description: str = ""


@dataclass
class ModelContract:
    name: str
    columns: dict[str, ColumnRule] = field(default_factory=dict)
    source: str = ""
    owner: str = ""


def _truthy(x) -> bool:
    return bool(x) and str(x).lower() not in ("false", "0", "no")


def _from_spec(doc: dict, src: str) -> list[ModelContract]:
    out = []
    owner = str((doc.get("info") or {}).get("owner") or "")
    for mname, m in (doc.get("models") or {}).items():          # Data Contract Specification
        mc = ModelContract(str(mname), source=src, owner=owner)
        for cname, c in ((m or {}).get("fields") or {}).items():
            c = c or {}
            mc.columns[str(cname).lower()] = ColumnRule(str(cname).lower(), c.get("type"), _truthy(c.get("required")),
                                                        _truthy(c.get("unique")) or _truthy(c.get("primary") or c.get("primaryKey")),
                                                        _truthy(c.get("pii")) or "pii" in [str(t).lower() for t in c.get("tags", [])],
                                                        c.get("description", "") or "")
        out.append(mc)
    for t in doc.get("schema") or []:                          # ODCS v3
        if not isinstance(t, dict):
            continue
        mc = ModelContract(str(t.get("name") or t.get("physicalName")), source=src, owner=owner)
        for p in t.get("properties") or []:
            mc.columns[str(p["name"]).lower()] = ColumnRule(
                str(p["name"]).lower(), p.get("physicalType") or p.get("logicalType"), _truthy(p.get("required")),
                _truthy(p.get("unique")) or _truthy(p.get("primaryKey")), _truthy(p.get("pii")) or "pii" in [str(c).lower() for c in p.get("classification", [])] if isinstance(p.get("classification"), list) else _truthy(p.get("pii")),
                p.get("description", "") or "")
        out.append(mc)
    return out


def load_all(spec: str | None) -> list[ModelContract]:
    files: list[Path] = []
    for part in [x.strip() for x in (spec or "").split(",") if x.strip()]:
        p = Path(os.path.expanduser(part))
        files += sorted([*p.rglob("*.yaml"), *p.rglob("*.yml")]) if p.is_dir() else [p]
    res: list[ModelContract] = []
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict):
            res += _from_spec(doc, str(f))
    return res
