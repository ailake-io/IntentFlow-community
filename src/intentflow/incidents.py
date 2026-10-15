"""Groups findings by root cause: findings whose affected nodes share the same upstream origin
become a single incident (one alert instead of dozens)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import SEVERITIES
from .i18n import cat, tr


@dataclass
class Incident:
    origin: str
    title: str
    severity: str
    findings: list[dict] = field(default_factory=list)
    impacted: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return "inc-" + self.origin.replace("model.", "").replace("seed.", "").replace("source.", "").replace(".", "-")[:40]


def group(findings: list[dict], project=None) -> list[Incident]:
    """`findings`: dicts (like `Store.findings`). `project`: dbt_loader.Project (for ancestry)."""
    def ancestors(uid):
        out, stack = set(), [uid]
        while stack:
            cur = stack.pop()                       # always drain the stack (an unknown node has no parents, but does not hang)
            node = project.nodes.get(cur) if project else None
            for d in (node.depends_on if node else []):
                if d not in out:
                    out.add(d)
                    if d in project.nodes:
                        stack.append(d)
        return out

    by: dict[str, Incident] = {}
    for f in findings:
        nodes = f.get("nodes") or [f["id"]]
        known = [n for n in nodes if project and n in project.nodes]
        if known:
            # origin = affected node without an affected ancestor; tie → the first (the finding itself names the node)
            roots = [n for n in known if not (ancestors(n) & set(known))]
            origin = (roots or known)[0]
        else:
            origin = nodes[0]
        inc = by.setdefault(origin, Incident(origin, "", f["severity"]))
        inc.findings.append(f)
        inc.impacted = sorted(set(inc.impacted) | set(nodes))
        if SEVERITIES.index(f["severity"]) < SEVERITIES.index(inc.severity):
            inc.severity = f["severity"]
        o = (f.get("evidence") or {}).get("owner")
        if o and o not in inc.owners:
            inc.owners.append(o)
    # the same check on origin and descendant = the same root cause: merge into the ancestor
    def checks(i):
        return {(f.get("check_id") or f.get("check", "")) for f in i.findings}
    if project:
        merged = True
        while merged:
            merged = False
            for a in list(by.values()):
                for b in list(by.values()):
                    if a is not b and a.origin in by and b.origin in by and a.origin in ancestors(b.origin) and checks(a) & checks(b):
                        a.findings += b.findings
                        a.impacted = sorted(set(a.impacted) | set(b.impacted))
                        a.owners += [o for o in b.owners if o not in a.owners]
                        if SEVERITIES.index(b.severity) < SEVERITIES.index(a.severity):
                            a.severity = b.severity
                        del by[b.origin]
                        merged = True
                        break
                if merged:
                    break
    out = []
    for inc in by.values():
        top = min(inc.findings, key=lambda x: SEVERITIES.index(x["severity"]))
        inc.title = cat(top["title"], " (" + tr("+{n} related", n=len(inc.findings) - 1) + ")") if len(inc.findings) > 1 else top["title"]
        out.append(inc)
    out.sort(key=lambda i: (SEVERITIES.index(i.severity), -len(i.findings), i.origin))
    return out
