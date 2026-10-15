"""Results of quality tools (Great Expectations, Soda) → common records.

Input: JSON files the tools themselves write (the tool is never run here):
  - Great Expectations: validation/checkpoint result (`result.to_json_dict()`, or `uncommitted/validations/**/*.json`).
  - Soda Core/Cloud scan: `soda scan ... -srf results.json`.
Configuration: `intentflow config set dq.results 'path/or/glob[,another]'` (folders work; only `.json`).
"""
from __future__ import annotations

from .i18n import tr

import glob
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DQResult:
    tool: str                 # great_expectations | soda
    table: str                # dataset/table name as the tool knows it
    column: str | None
    check: str                # expect_column_values_to_not_be_null | row_count > 0 ...
    status: str               # pass | fail | warn | error
    detail: str = ""
    observed: dict = field(default_factory=dict)
    suite: str = ""
    severity: str = ""        # declared in the tool (GX 1.x: critical|warning|info), if any


def _gx_table(v: dict) -> str:
    meta = v.get("meta") or {}
    bd = meta.get("active_batch_definition") or {}
    spec = meta.get("batch_spec") or {}
    for c in (bd.get("data_asset_name"), spec.get("table_name"), spec.get("data_asset_name"), (v.get("batch_spec") or {}).get("table_name"),
              meta.get("asset_name"), (meta.get("batch_markers") or {}).get("table_name")):
        if c:
            return str(c)
    ids = meta.get("batch_id") or (v.get("batch_id") if isinstance(v.get("batch_id"), str) else "") or ""
    return str(ids).split("-")[-1] if ids else ""        # batch_id: "<datasource>-<asset>"


def _gx_validation(v: dict) -> list[DQResult]:
    table = _gx_table(v)
    suite = v.get("suite_name") or (v.get("meta") or {}).get("expectation_suite_name") or ""
    out = []
    for r in v.get("results", []):
        cfg = r.get("expectation_config") or {}
        kw = {k: x for k, x in (cfg.get("kwargs") or {}).items() if k != "batch_id"}
        res = r.get("result") or {}
        exc = r.get("exception_info") or {}
        raised = exc.get("raised_exception") if isinstance(exc, dict) and "raised_exception" in exc else bool(exc and any(
            isinstance(x, dict) and x.get("raised_exception") for x in exc.values()))
        name = cfg.get("type") or cfg.get("expectation_type") or "expectation"
        status = "error" if raised else ("pass" if r.get("success") else "fail")
        obs = {k: res[k] for k in ("element_count", "unexpected_count", "unexpected_percent", "observed_value") if k in res}
        out.append(DQResult("great_expectations", table, kw.get("column"), name, status,
                            "" if status == "pass" else f"{name}({', '.join(f'{k}={v!r}' for k, v in kw.items() if k != 'column')})", obs, suite,
                            str(cfg.get("severity") or "")))
    return out


def _soda(doc: dict) -> list[DQResult]:
    out = []
    for c in doc.get("checks", []):
        o = str(c.get("outcome") or "").lower()
        status = {"pass": "pass", "fail": "fail", "warn": "warn"}.get(o, "error" if c.get("outcomeReasons") else "pass")
        diag = c.get("diagnostics") or {}
        obs = {k: v for k, v in diag.items() if isinstance(v, (int, float, str)) and k in ("value", "fail", "warn")}
        out.append(DQResult("soda", c.get("table") or c.get("tableName") or "", c.get("column"), c.get("name") or str(c.get("definition") or "check").strip().splitlines()[-1].strip(" -"),
                            status, "", obs, c.get("definitionName") or ""))
    return out


def parse(doc) -> list[DQResult]:
    """Detect the format from the content. Unknown → []."""
    if isinstance(doc, list):
        return [r for d in doc for r in parse(d)]
    if not isinstance(doc, dict):
        return []
    if "checks" in doc and ("hasFailures" in doc or "defaultDataSource" in doc or "definitionName" in doc):
        return _soda(doc)
    if "run_results" in doc:                              # GX 0.x/1.x checkpoint
        return [r for v in doc["run_results"].values() for r in parse(v.get("validation_result", v))]
    if "results" in doc and ("suite_name" in doc or "expectation_suite_name" in (doc.get("meta") or {}) or "statistics" in doc):
        return _gx_validation(doc)
    return []


def expand(spec: str | None) -> list[Path]:
    out: list[Path] = []
    for part in [x.strip() for x in (spec or "").split(",") if x.strip()]:
        p = Path(os.path.expanduser(part))
        if p.is_dir():
            out += sorted(p.rglob("*.json"))
        else:
            out += [Path(x) for x in sorted(glob.glob(str(p)))]
    return out


def load_all(spec: str | None) -> list[DQResult]:
    res: list[DQResult] = []
    for f in expand(spec):
        try:
            res += parse(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return res
