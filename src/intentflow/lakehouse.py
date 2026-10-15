"""Delta Lake and Apache Iceberg tables: small files and schema evolution (optional: `pip install intentflow[lakehouse]`).

`lakehouse.tables`: list (or comma-separated text) of paths/URIs. Delta is detected by `_delta_log/`; Iceberg by a
`*.metadata.json` (pass the latest metadata file, or the table folder with `metadata/`).
"""
from __future__ import annotations

from .i18n import tr

import json
from pathlib import Path


def _delta(path: str) -> dict:
    import pyarrow as pa
    from deltalake import DeltaTable
    dt = DeltaTable(path)
    sizes = [int(x) for x in pa.table(dt.get_add_actions(flatten=True)).column("size_bytes").to_pylist()]
    ver = dt.version()
    prev = None
    if ver > 0:
        prev = {f.name: str(f.type.type if hasattr(f.type, "type") else f.type) for f in DeltaTable(path, version=ver - 1).schema().fields}
    cur = {f.name: str(f.type.type if hasattr(f.type, "type") else f.type) for f in dt.schema().fields}
    hist = dt.history(5)
    return {"format": "delta", "path": path, "name": Path(path.rstrip("/")).name, "version": ver, "files": len(sizes), "bytes": sum(sizes),
            "avg_file_bytes": (sum(sizes) / len(sizes)) if sizes else 0, "schema": cur, "prev_schema": prev,
            "last_ops": [h.get("operation") for h in hist]}


def _iceberg(path: str) -> dict:
    from pyiceberg.table import StaticTable
    p = Path(path)
    if p.is_dir():
        cands = sorted((p / "metadata").glob("*.metadata.json"))
        if not cands:
            raise ValueError(f"sem metadata.json em {path}")
        p = cands[-1]
    t = StaticTable.from_metadata(str(p))
    md = t.metadata
    schemas = {s.schema_id: {f.name: str(f.field_type) for f in s.fields} for s in md.schemas}
    ids = sorted(schemas)
    cur_id = md.current_schema_id
    prev_id = max([i for i in ids if i < cur_id], default=None)
    files = [f.file.file_size_in_bytes for f in t.scan().plan_files()] if md.current_snapshot_id else []
    return {"format": "iceberg", "path": str(p), "name": p.parent.parent.name if p.parent.name == "metadata" else p.stem, "version": md.current_snapshot_id,
            "files": len(files), "bytes": sum(files), "avg_file_bytes": (sum(files) / len(files)) if files else 0,
            "schema": schemas[cur_id], "prev_schema": schemas[prev_id] if prev_id is not None else None,
            "last_ops": [s.summary.operation.value if s.summary else None for s in (md.snapshots or [])[-5:]]}


def inspect(path: str) -> dict:
    p = Path(path)
    if (p / "_delta_log").exists():
        return _delta(path)
    if p.suffix == ".json" or (p / "metadata").exists():
        return _iceberg(path)
    raise ValueError(tr("{path}: not a recognized Delta or Iceberg table", path=path))


def tables(spec) -> list[str]:
    if isinstance(spec, str):
        spec = [x.strip() for x in spec.split(",")]
    return [str(x) for x in (spec or []) if str(x).strip()]
