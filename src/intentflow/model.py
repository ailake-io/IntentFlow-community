"""Core types: findings, lineage nodes and edges."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

SEVERITIES = ["critical", "high", "medium", "low", "info"]
STAGES = ["pre-merge", "pre-run", "continuous"]


@dataclass
class Finding:
    check: str                       # e.g. DBT002
    severity: str                    # critical|high|medium|low|info
    title: str
    detail: str = ""
    nodes: list[str] = field(default_factory=list)   # affected unique_ids
    stage: str = "pre-merge"
    evidence: dict = field(default_factory=dict)
    fix: dict | None = None          # fix proposal data (used by IntentFlow Enterprise)
    confidence: float = 1.0

    @property
    def id(self) -> str:
        title = getattr(self.title, "en", self.title)      # English rendering: the id must not depend on the language
        key = json.dumps([self.check, sorted(self.nodes), title], sort_keys=True)
        return self.check + "-" + hashlib.sha1(key.encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        return d


@dataclass(frozen=True)
class Edge:
    src: str          # unique_id
    src_col: str
    dst: str
    dst_col: str
    kind: str         # direct | derived
    masked: bool = False   # went through a hash/mask function
