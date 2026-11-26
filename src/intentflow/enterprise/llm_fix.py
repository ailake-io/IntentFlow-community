"""LLM-suggested fixes, inside the same governed funnel as rule-based fixes.

The model does NOT write files: it returns `edits` (exact snippets to replace). Each `find` must occur ONCE in the file that was sent;
only the finding's own files are accepted; the result becomes a `pending` proposal with a diff. Human approval, sandbox validation
(`dbt parse`/`build`), apply and rollback are the usual ones. What leaves the server: the model SQL, schemas (names/types), the finding
text and evidence without data samples. Never data rows.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from pathlib import Path

from intentflow.i18n import lang, tr

from . import file_transaction, jev, llm, proposals

MAX_FILE = 14000
MAX_EDITS = 8

SYSTEM = """You are a senior analytics engineer fixing a data-pipeline problem found by an automated checker.
Return ONLY one JSON object, no prose, no markdown fences, with this shape:
{"explanation": "<2-4 sentences in {language}: what is wrong and what you changed>",
 "confidence": <number between 0 and 1>,
 "edits": [{"file": "<path exactly as given>", "find": "<exact substring of that file>", "replace": "<new text>"}]}
Rules:
- Make the smallest change that resolves the finding. Do not reformat, rename or refactor anything else.
- Each "find" must be copied verbatim from the file, must occur exactly once in it, and should include enough surrounding text to be unique.
- Only edit the files provided. Keep dbt Jinja ({{ ref() }}, {{ config() }}, macros) intact.
- Never invent column names: use only columns listed in the provided schemas.
- If the problem cannot be fixed by editing these files (e.g. it is a data/upstream issue), return {"explanation": "...", "confidence": 0.0, "edits": []}."""


def system_prompt() -> str:
    return SYSTEM.replace("{language}", "Brazilian Portuguese" if lang() == "pt" else "English")


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_context(project, finding: dict) -> dict:
    if project.root:
        with file_transaction.locked_root(project.root, shared=True):
            return _build_context(project, finding)
    return _build_context(project, finding)


def _build_context(project, finding: dict) -> dict:
    files, schemas, down = {}, {}, set()
    for uid in finding.get("nodes") or []:
        n = project.nodes.get(uid)
        if not n or n.resource_type not in ("model", "snapshot") or len(files) >= 3:
            continue
        p = proposals.project_file(project, n.path) if project.root and n.path else None
        if p:
            files[str(p)] = {"rel": n.path, "content": p.read_text()[:MAX_FILE], "truncated": p.stat().st_size > MAX_FILE, "name": n.name}
        for d in n.depends_on:
            pn = project.nodes.get(d)
            if pn and pn.columns:
                schemas[pn.name] = {c.name: (c.type or "?") for c in list(pn.columns.values())[:40]}
        if n.columns:
            schemas[n.name] = {c.name: (c.type or "?") for c in list(n.columns.values())[:40]}
        down |= {project.nodes[x].name for x in project.downstream(uid)[:15] if x in project.nodes}
    ev = {k: v for k, v in (finding.get("evidence") or {}).items() if k not in ("sample", "samples", "rows", "examples", "observed")}
    return {"files": files, "schemas": schemas, "downstream": sorted(down),
            "finding": {"check": finding.get("check_id"), "severity": finding.get("severity"), "title": finding.get("title"),
                        "detail": (finding.get("detail") or "")[:800], "evidence": json.loads(json.dumps(ev, default=str))}}


def build_prompt(ctx: dict) -> str:
    parts = ["## Finding", json.dumps(ctx["finding"], ensure_ascii=False, indent=1),
             "## Table schemas (name: type)", json.dumps(ctx["schemas"], ensure_ascii=False, indent=1),
             "## Downstream models (do not break them)", ", ".join(ctx["downstream"]) or "(none)"]
    for _path, f in ctx["files"].items():
        parts += [f"## File: {f['rel']}" + (" (truncated)" if f["truncated"] else ""), "```sql", f["content"], "```"]
    return "\n\n".join(parts)


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError(tr("the model response contains no JSON object"))
    raw = t[a:b + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:       # a common mistake of small models: a trailing comma before } or ]
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", raw))
    except json.JSONDecodeError as e:
        raise ValueError(tr("invalid JSON in the model response ({error})", error=e.msg)) from None


_REF = re.compile(r"\{\{\s*(?:ref|source)\s*\(")
_JINJA = re.compile(r"\{\{|\{%")
# constructs that read the environment, run SQL at parse time or add hooks: an AI edit never introduces them
_RISKY = re.compile(r"env_var\s*\(|run_query\s*\(|\bpost_hook\b|\bpre_hook\b|on-run-|\{%-?\s*(?:do|call|statement)\b|\bexec\s*\(|\beval\s*\(|\badapter\s*\.|\bmodules\s*\.", re.I)


def guards(name: str, before: str, after: str) -> tuple[list[str], list[str]]:
    """Deterministic guards over the result of the edit: (rejections, warnings). They do not depend on the model."""
    reject, warn = [], []
    if len(_REF.findall(after)) < len(_REF.findall(before)):
        reject.append(tr("{file}: the edit removes a `ref()`/`source()` (it would break dbt lineage)", file=name))
    if len(_RISKY.findall(after)) > len(_RISKY.findall(before)):
        reject.append(tr("{file}: the edit adds env_var/run_query/hooks or similar; an AI edit may not introduce them", file=name))
    lost = len(_JINJA.findall(before)) - len(_JINJA.findall(after))
    if lost > 0:
        warn.append(tr("{file}: the edit removes {n} Jinja/macro expression(s); confirm that is part of the fix", file=name, n=lost))
    d = [x for x in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0) if x[:1] in "+-" and x[:3] not in ("+++", "---")]
    if len(d) > 24:
        warn.append(tr("{file}: the edit changes {n} lines; minimal fixes are safer", file=name, n=len(d)))
    return reject, warn


def apply_edits(ctx: dict, edits: list) -> tuple[list[dict], list[str]]:
    """Validate and apply; return (changes in the proposals format, warnings). Raises ValueError on an unsafe edit."""
    if not isinstance(edits, list) or not edits:
        raise ValueError(tr("the model proposed no edit"))
    if len(edits) > MAX_EDITS:
        raise ValueError(tr("too many edits ({n} > {max}); ask for a smaller fix", n=len(edits), max=MAX_EDITS))
    by_rel = {f["rel"]: (path, f) for path, f in ctx["files"].items()}
    cur = {path: f["content"] for path, f in ctx["files"].items()}
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("file", "find", "replace")):
            raise ValueError(tr("edit {i}: missing file/find/replace fields", i=i))
        if e["file"] not in by_rel:
            raise ValueError(tr("edit {i}: file outside the finding's scope ({file})", i=i, file=e["file"]))
        path, f = by_rel[e["file"]]
        if f["truncated"]:
            raise ValueError(tr("{file} is too large for automatic editing (the file was truncated when sent)", file=e["file"]))
        find = e["find"]
        if not find.strip():
            raise ValueError(tr("edit {i}: empty `find`", i=i))
        n = cur[path].count(find)
        if n != 1:
            raise ValueError(tr("edit {i}: the snippet to replace appears {n} time(s) in {file} (it must be exactly 1)", i=i, n=n, file=e["file"]))
        cur[path] = cur[path].replace(find, e["replace"], 1)
    changes, warnings = [], []
    for path, f in ctx["files"].items():
        after = cur[path]
        if after == f["content"]:
            continue
        if not after.strip():
            raise ValueError(tr("the edit would empty {file}", file=f["rel"]))
        rej, wr = guards(f["rel"], f["content"], after)
        if rej:
            raise ValueError("; ".join(rej))
        warnings += wr
        if abs(len(after) - len(f["content"])) > max(2000, len(f["content"])):
            warnings.append(tr("{file}: the edit changes the file size a lot; review it carefully", file=f["rel"]))
        diff = "".join(difflib.unified_diff(f["content"].splitlines(True), after.splitlines(True), f"a/{Path(path).name}", f"b/{Path(path).name}"))
        changes.append({"file": path, "before_hash": _h(f["content"]), "after": after, "diff": diff})
    if not changes:
        raise ValueError(tr("the edits change no file"))
    return changes, warnings


def suggest(store, project, finding: dict, user: str, *, force_llm: bool = False, chat=None, decide_kw: dict | None = None, guard=None) -> dict:
    """Full flow. `chat`: injectable in tests (replaces `llm.chat`)."""
    out: dict = {"source": None, "triage": None}
    jcfg = jev.status(store)
    triage = None
    if jcfg["enabled"] and jcfg["configured"]:
        try:
            triage = jev.triage(store, finding, project, **(decide_kw or {}))
            out["triage"] = triage
            store.set_setting(f"state.jev.{finding['id']}", triage)
        except jev.JevError as e:
            out["triage_error"] = str(e)
    has_det = (finding.get("fix") or {}).get("kind") in ("rename_column", "expand_star")
    if triage and not force_llm:
        if has_det and not jev.needs_generative_model(triage):
            p = proposals.create(store, project, finding, user, guard=guard)
            return {**out, "source": "rule", "proposal": p["id"], "note": tr("Jev pointed to a mechanical fix; no LLM call was needed")}
        if triage.get("fix_path") in ("no_action", "investigate_data") and (triage.get("fix_path_confidence") or 0) >= 0.8:
            return {**out, "source": "skipped", "note": tr("Jev says: {path} (use 'force' to ask anyway)", path=jev.FIX_PATHS[triage["fix_path"]])}
    ctx = build_context(project, finding)
    if not ctx["files"]:
        raise ValueError(tr("there is no model file linked to this finding to edit"))
    call = chat or (lambda s, u: llm.chat(store, s, u, json_mode=True))
    r = call(system_prompt(), build_prompt(ctx))
    data = extract_json(r["text"])
    changes, warnings = apply_edits(ctx, data.get("edits"))
    pid = "fix-" + _h(finding["id"] + json.dumps([(c["file"], c["after"]) for c in changes]))[:10]
    s = llm.settings(store)
    p = {"id": pid, "kind": "llm_edit", "origin": "llm", "finding_id": finding["id"],
         "summary": tr("AI-suggested fix: {title}", title=finding.get("title", "")[:90]),
         "explanation": str(data.get("explanation", ""))[:1500], "confidence": data.get("confidence"),
         "changes": changes, "warnings": warnings, "approval": None, "validation": None, "backup": None, "created_by": user,
         "llm": {"provider": s["provider"], "model": r.get("model", s["model"]), "usage": r.get("usage"), "seconds": r.get("seconds"), "ts": time.time()}}
    if project.root:
        p["project_root"] = str(Path(project.root).resolve())
    if any("Jinja" in getattr(w, "en", w) or " lines;" in getattr(w, "en", w) for w in p["warnings"]):      # English rendering: language-independent
        p["policy"] = {"validate_mode": "build", "needs_extra_review": True, "reason": tr("the edit removes Jinja/macros or is large")}
    if triage:
        p["triage"] = triage
    if jcfg["enabled"] and jcfg["configured"]:
        try:
            g = jev.risk_gate(store, p["summary"], "\n".join(c["diff"] for c in changes), "llm_edit", **(decide_kw or {}))
            p["risk"] = g
            pol = jev.policy_for_risk(g)
            if p.get("policy", {}).get("needs_extra_review"):      # Jev never loosens the deterministic guard
                pol = {"validate_mode": "build", "needs_extra_review": True, "reason": p["policy"]["reason"] + "; " + pol["reason"]}
            p["policy"] = pol
        except jev.JevError as e:
            p["warnings"].append(tr("Jev risk gate unavailable: {error}", error=e))
    if guard:
        guard(p)
    proposals.save_new(store, p)
    store.audit(user, "proposal_created", {"id": pid, "finding": finding["id"], "kind": "llm_edit", "model": p["llm"]["model"]})
    return {**out, "source": "llm", "proposal": pid}
