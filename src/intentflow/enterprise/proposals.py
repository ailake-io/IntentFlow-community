"""Governed fixes: proposal → approval bound to the content → sandbox validation → apply → rollback.

Deterministic fixes: column rename (word-boundary substitution in the affected dbt models), `SELECT *` expansion, owner and
`unique_key`. Nothing is applied without `approve`; the approval is invalidated if any target file changes afterwards.
Validation runs dbt in Docker without network, with a temporary DuckDB profile.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import base64
from functools import wraps
import time
from pathlib import Path

from intentflow.i18n import tr
from intentflow.store import Store
from . import dbt_sandbox, file_transaction


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def project_file(project, relative: str) -> Path:
    """Resolve only regular files contained in the configured project, including symlinks."""
    if not project.root:
        raise ValueError(tr("project root not configured"))
    root = Path(project.root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(tr("file outside the project or missing"))
    # a crafted manifest could name profiles.yml or .env as a "model": only model/snapshot sources are editable or sent to an LLM
    if path.suffix not in (".sql", ".py") or any(part.startswith(".") for part in path.relative_to(root).parts):
        raise ValueError(tr("not a model source file"))
    return path


def _model_file(project, name: str) -> Path | None:
    for n in project.nodes.values():
        if n.name == name and n.resource_type == "model" and n.path and project.root:
            return project_file(project, n.path)
    return None


_CLAUSE = re.compile(r"\b(select|from|where|on|and|or|group\s+by|order\s+by|having|join|using|qualify|when|then|else)\b", re.I)


def _rewrite(text: str, old: str, new: str) -> tuple[str, int]:
    """Replace `alias.old` with `alias.new`. In the SELECT list, keep the output name (`as old`).
    Unqualified occurrences are left alone (ambiguous) and counted as pending."""
    pat = re.compile(rf"(?P<q>\b\w+\.){re.escape(old)}\b(?!\s*\()")
    out, pos = [], 0
    for m in pat.finditer(text):
        out.append(text[pos:m.start()])
        kw = list(_CLAUSE.finditer(text[:m.start()]))
        in_select = bool(kw) and kw[-1].group(1).lower() == "select"
        tail = text[m.end():]
        has_alias = re.match(r"\s+(as\s+)?\w+\s*(,|\n|$)", tail, re.I) and not re.match(r"\s*(from|where|join)\b", tail, re.I)
        out.append(f"{m.group('q')}{new}" + (f" as {old}" if in_select and not has_alias and re.match(r"\s*(,|\n|$)", tail) else ""))
        pos = m.end()
    out.append(text[pos:])
    new_text = "".join(out)
    bare = len(re.findall(rf"(?<![\w.]){re.escape(old)}(?![\w(])", new_text.replace(f" as {old}", "")))
    return new_text, bare


_SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")


def build_rename(project, fix: dict, finding_id: str) -> dict:
    old, new = fix["old"], fix["new"]
    # column names come from the PR branch's catalog and are written into model SQL: only plain identifiers
    if not (_SQL_IDENT.match(str(old)) and _SQL_IDENT.match(str(new))):
        raise ValueError(tr("column name is not a plain identifier; rename it by hand"))
    changes, warnings = [], []
    for target in fix["targets"]:
        f = _model_file(project, target)
        if not f:
            warnings.append(tr("{model}: model file not found", model=target))
            continue
        before = f.read_text()
        after, bare = _rewrite(before, old, new)
        if bare:
            warnings.append(tr("{model}: {n} unqualified occurrence(s) of `{column}` need manual review", model=target, n=bare, column=old))
        if after == before:
            continue
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{f.name}", f"b/{f.name}"))
        changes.append({"file": str(f), "before_hash": _h(before), "after": after, "diff": diff})
    pid = "fix-" + _h(finding_id + json.dumps([(c["file"], c["after"]) for c in changes]))[:10]
    return {"id": pid, "kind": "rename_column", "finding_id": finding_id, "summary": tr("rename column {old} → {new} in {models}", old=old, new=new, models=", ".join(fix["targets"])),
            "changes": changes, "warnings": warnings, "approval": None, "validation": None, "backup": None}


def _change_hash(p: dict) -> str:
    return _h(json.dumps([(c["file"], c["before_hash"], _h(c["after"])) for c in p["changes"]]))


def build_expand_star(project, fix: dict, finding_id: str) -> dict:
    f = _model_file(project, fix["node"])
    if not f:
        raise ValueError(tr("model file not found"))
    before = f.read_text()

    def sub(m):
        cols = fix["refs"].get(m.group(1))
        return m.group(0) if not cols or not all(_SQL_IDENT.match(str(c)) for c in cols) else "select " + ", ".join(cols) + m.group(0)[m.group(0).lower().index("from") - 1:]
    pat = re.compile(r"select\s+\*\s+from\s+\{\{\s*ref\(\s*['\"](\w+)['\"]\s*\)\s*\}\}", re.I)
    after = pat.sub(sub, before)
    changes = []
    if after != before:
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{f.name}", f"b/{f.name}"))
        changes.append({"file": str(f), "before_hash": _h(before), "after": after, "diff": diff})
    pid = "fix-" + _h(finding_id + json.dumps([(c["file"], c["after"]) for c in changes]))[:10]
    return {"id": pid, "kind": "expand_star", "finding_id": finding_id, "summary": tr("expand SELECT * in {model}", model=fix["node"]),
            "changes": changes, "warnings": [], "approval": None, "validation": None, "backup": None}


_OWNER = re.compile(r"^[\w .@-]{1,60}$")


def _inject_config(before: str, kv: str, existing: str, err: str) -> str:
    """Add `kv` (e.g. `unique_key='id'`) to the model's `{{ config(...) }}`, or create the block at the top. `existing` = regex of the key
    that must not already be in config() (otherwise raise `err`)."""
    m = re.search(r"\{\{-?\s*config\(", before)
    if not m:
        return "{{ config(" + kv + ") }}\n" + before
    close = before.find(")", m.end())
    if re.search(existing, before[m.end():close if close > 0 else None]):
        raise ValueError(err)
    sep = "," if before[m.end():].lstrip().startswith(("\n", "'", '"')) or re.match(r"\s*\w", before[m.end():]) else ""
    return before[:m.end()] + kv + sep + " " + before[m.end():]


def _config_proposal(project, finding: dict, kind: str, kv: str, existing: str, err: str, summary: str, explanation: str) -> dict:
    uid = (finding.get("nodes") or [None])[0]
    n = project.nodes.get(uid)
    f = _model_file(project, n.name) if n else None
    if not f:
        raise ValueError(tr("model file not found"))
    before = f.read_text()
    after = _inject_config(before, kv, existing, err)
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{f.name}", f"b/{f.name}"))
    changes = [{"file": str(f), "before_hash": _h(before), "after": after, "diff": diff}]
    pid = "fix-" + _h(finding["id"] + json.dumps([(c["file"], c["after"]) for c in changes]))[:10]
    return {"id": pid, "kind": kind, "origin": "rule", "finding_id": finding["id"], "summary": summary.replace("{name}", n.name),
            "explanation": explanation, "changes": changes, "warnings": [], "approval": None, "validation": None, "backup": None}


def build_owner(project, finding: dict, owner: str) -> dict:
    """DBT003: declare `meta.owner` in the model's `config()` (a rule, no LLM: the owner is a human decision)."""
    owner = (owner or "").strip()
    if not _OWNER.match(owner):
        raise ValueError(tr("invalid owner: use letters, digits, space, dot, @ or hyphen (up to 60 characters)"))
    return _config_proposal(project, finding, "add_owner", "meta={'owner': '" + owner + "'}", r"\bmeta\s*=",
                            tr("this model's config() already defines meta; add `owner` to it by hand"),
                            tr("set the owner of {name}: {owner}", owner=owner, name="{name}"),
                            tr("Adds meta.owner = {owner} to the model's config(); alerts and proposals now have someone to be routed to.", owner=owner))


_IDENT = re.compile(r"^[A-Za-z_]\w{0,62}$")


def build_unique_key(project, finding: dict, key: str) -> dict:
    """DBT002: declare `unique_key` in the incremental model's `config()`. The key is a human decision (the rule only checks the format and,
    with a catalog, that the column exists). A comma separates a composite key."""
    cols = [c.strip() for c in (key or "").split(",") if c.strip()]
    if not cols or len(cols) > 6 or not all(_IDENT.match(c) for c in cols):
        raise ValueError(tr("invalid key: pass column names (letters, digits, _), comma-separated if composite"))
    uid = (finding.get("nodes") or [None])[0]
    n = project.nodes.get(uid)
    if n and n.columns:
        known = {c.lower() for c in n.columns}
        missing = [c for c in cols if c.lower() not in known]
        if missing:
            raise ValueError(tr("column(s) {missing} do not exist in {model} (columns: {columns})", missing=missing, model=n.name, columns=sorted(n.columns)[:12]))
    lit = repr(cols[0]) if len(cols) == 1 else repr(cols)
    return _config_proposal(project, finding, "add_unique_key", f"unique_key={lit}", r"\bunique_key\s*=",
                            tr("this model's config() already defines unique_key"),
                            tr("set the unique_key of {name}: {key}", key=", ".join(cols), name="{name}"),
                            tr("Adds unique_key={key} to the incremental model's config(); reprocessing a window now updates the rows "
                               "instead of duplicating them. Confirm the key is unique at the source.", key=lit))


def _save_new_locked(store: Store, p: dict):
    existing = store.proposal(p["id"])
    if existing and existing["status"] in ("approved", "applied", "applying", "rolling_back"):
        raise ValueError(tr("proposal already approved/applied; it cannot be overwritten"))
    store.put_proposal(p["id"], p, "pending")


def save_new(store: Store, p: dict):
    with file_transaction.locked(p) as journal:
        journal.require_idle()
        _save_new_locked(store, p)


def create_owner(store: Store, project, finding: dict, owner: str, user: str, guard=None) -> dict:
    if not project.root:
        raise ValueError(tr("project root not configured"))
    root = Path(project.root).resolve()
    with file_transaction.locked_root(root) as journal:
        journal.require_idle()
        p = build_owner(project, finding, owner)
        p["created_by"] = user
        p["project_root"] = str(root)
        if guard:
            guard(p)            # e.g. write-scope check over every file the fix would change
        _save_new_locked(store, p)
        store.audit(user, "proposal_created", {"id": p["id"], "finding": finding["id"], "kind": "add_owner"})
    return p


def create_unique_key(store: Store, project, finding: dict, key: str, user: str, guard=None) -> dict:
    if not project.root:
        raise ValueError(tr("project root not configured"))
    root = Path(project.root).resolve()
    with file_transaction.locked_root(root) as journal:
        journal.require_idle()
        p = build_unique_key(project, finding, key)
        p["created_by"] = user
        p["project_root"] = str(root)
        if guard:
            guard(p)            # e.g. write-scope check over every file the fix would change
        _save_new_locked(store, p)
        store.audit(user, "proposal_created", {"id": p["id"], "finding": finding["id"], "kind": "add_unique_key"})
    return p


def create(store: Store, project, finding: dict, user: str = "rule:auto", guard=None) -> dict:
    fix = finding.get("fix")
    if not fix or fix.get("kind") not in ("rename_column", "expand_star"):
        raise ValueError(tr("the finding has no deterministic fix available"))
    if not project.root:
        raise ValueError(tr("project root not configured"))
    root = Path(project.root).resolve()
    with file_transaction.locked_root(root) as journal:
        journal.require_idle()
        p = build_rename(project, fix, finding["id"]) if fix["kind"] == "rename_column" else build_expand_star(project, fix, finding["id"])
        if not p["changes"]:
            raise ValueError(tr("no model file contains the column (nothing to change)"))
        p["created_by"] = user
        p["project_root"] = str(root)
        if guard:
            guard(p)            # e.g. write-scope check over every file the fix would change
        _save_new_locked(store, p)
        store.audit(user if user != "rule:auto" else "rule:rename", "proposal_created", {"id": p["id"], "finding": finding["id"]})
    return p



def _guard(fn):
    @wraps(fn)
    def guarded(store, pid, *args, **kwargs):
        p = store.proposal(pid)
        if not p:
            return fn(store, pid, *args, **kwargs)
        with file_transaction.locked(p) as journal:
            journal.require_idle()
            return fn(store, pid, *args, **kwargs)
    return guarded


@_guard
def approve(store: Store, pid: str, user: str, enforce_separation: bool | None = None) -> dict:
    p = store.proposal(pid)
    if not p or p["status"] != "pending":
        raise ValueError(tr("proposal missing or not pending"))
    # separation of duties: with more than one registered user, whoever created the proposal does not approve it
    enforce = enforce_separation if enforce_separation is not None else len(store.users()) > 1
    if p.get("created_by") == user and ((enforce and store.get_setting("policy.separate_approver", True))
                                         or (p.get("policy") or {}).get("needs_extra_review")):
        raise ValueError(tr("whoever created the fix cannot approve it (ask someone else on the team)"))
    p["approval"] = {"user": user, "ts": time.time(), "change_hash": _change_hash(p), "execute": False}
    store.put_proposal(pid, p, "approved")
    store.audit(user, "approved", {"id": pid})
    return p


@_guard
def reject(store: Store, pid: str, user: str) -> dict:
    p = store.proposal(pid)
    if not p or p["status"] != "pending":
        raise ValueError(tr("proposal missing or not pending"))
    store.put_proposal(pid, p, "rejected")
    store.audit(user, "rejected", {"id": pid})
    return p


def validate(p: dict, mode: str = "parse", *, source_lock_held: bool = False) -> dict:
    """Validate with local Docker, without network, using a temporary DuckDB."""
    if mode not in ("parse", "build"):
        raise ValueError(tr("invalid validation mode (use parse or build)"))
    if (p.get("policy") or {}).get("validate_mode") == "build" and mode != "build":
        raise ValueError(tr("this proposal's policy requires a build validation"))
    root = file_transaction.project_root(p)
    if not (root / "dbt_project.yml").is_file():
        return {"ok": False, "mode": mode, "output": tr("dbt_project.yml not found")}
    for c in p["changes"]:
        if _h(Path(c["file"]).read_text()) != c["before_hash"]:
            raise ValueError(tr("file changed; create a new proposal"))
    return dbt_sandbox.run(root, p["changes"], mode, source_lock_held=source_lock_held)


@_guard
def validate_and_store(store: Store, pid: str, mode: str = "parse", actor: str = "system") -> dict:
    p = store.proposal(pid)
    if not p:
        raise ValueError(tr("proposal not found"))
    if p["status"] not in ("pending", "approved", "invalid"):
        raise ValueError(tr("proposal is not available for validation"))
    validated_hash = _change_hash(p)
    validation = {**validate(p, mode, source_lock_held=True), "change_hash": validated_hash}
    current = store.proposal(pid)
    if not current or _change_hash(current) != validated_hash or current["status"] not in ("pending", "approved", "invalid"):
        raise ValueError(tr("proposal changed during validation; validate again"))
    p = current
    p["validation"] = validation
    status = p["status"]
    if validation["ok"] and status == "invalid":
        status = "pending"
        p["approval"] = None
    store.put_proposal(pid, p, status if validation["ok"] else "invalid")
    store.audit(actor, "validated", {"id": pid, "ok": p["validation"]["ok"], "mode": mode})
    return p


def request_validation(store: Store, pid: str, mode: str, actor: str) -> dict:
    """Queue a validation for a worker on a host with Docker (`intentflow validate-worker`). The UI process may have no Docker."""
    if mode not in ("parse", "build"):
        raise ValueError(tr("invalid validation mode (use parse or build)"))
    p = store.proposal(pid)
    if not p:
        raise ValueError(tr("proposal not found"))
    with file_transaction.locked(p) as journal:       # same lock as approve/apply: no stale read-modify-write
        journal.require_idle()
        p = store.proposal(pid)
        if p["status"] not in ("pending", "approved", "invalid"):
            raise ValueError(tr("proposal is not available for validation"))
        p["validation_request"] = {"mode": mode, "by": actor, "at": time.time(), "change_hash": _change_hash(p)}
        store.put_proposal(pid, p, p["status"])
    store.audit(actor, "validation_queued", {"id": pid, "mode": mode})
    return p


def _clear_request(store: Store, pid: str, req: dict) -> None:
    """Remove the queued request, unless a newer one replaced it, under the proposal lock."""
    cur = store.proposal(pid)
    if not cur:
        return
    with file_transaction.locked(cur) as journal:
        journal.require_idle()
        cur = store.proposal(pid)
        if cur and (cur.get("validation_request") or {}).get("at") == req.get("at"):
            status = cur.pop("status")
            cur.pop("id", None)
            cur.pop("validation_request")
            store.put_proposal(pid, cur, status)


def process_validation_requests(store: Store) -> int:
    """Run every queued validation; the result lands in the proposal exactly as an inline validation would. Returns how many ran.
    A request that could not run for a transient reason (lock busy, too many validations) stays queued for the next pass."""
    done = 0
    for q in store.proposals():
        req = q.get("validation_request")
        if not req:
            continue
        pid = q["id"]
        retry = False
        try:
            if req.get("change_hash") != _change_hash(q):
                raise ValueError(tr("proposal changed during validation; validate again"))
            validate_and_store(store, pid, req.get("mode", "parse"), req.get("by", "worker"))
        except file_transaction.Busy as e:       # transient: try again on the next pass
            retry = True
            store.audit("worker", "validation_deferred", {"id": pid, "error": str(e)[:300]})
        except Exception as e:      # recorded, never fatal: the next request must still run
            store.audit("worker", "validation_failed", {"id": pid, "error": str(e)[:300]})
        if not retry:
            _clear_request(store, pid, req)
            done += 1
    return done


def apply(store: Store, pid: str, actor: str | None = None) -> dict:
    p = store.proposal(pid)
    if not p:
        raise ValueError(tr("applying requires a proposal approved by a human"))
    with file_transaction.locked(p) as journal:
        journal.require_idle()
        p = store.proposal(pid)
        if p["status"] != "approved" or not p.get("approval"):
            raise ValueError(tr("applying requires a proposal approved by a human"))
        if p["approval"]["change_hash"] != _change_hash(p):
            raise ValueError(tr("proposal content differs from what was approved"))
        if not (p.get("validation") or {}).get("ok"):
            raise ValueError(tr("proposal was not validated in the sandbox (run the validation before applying)"))
        if p["validation"].get("change_hash") != _change_hash(p):
            raise ValueError(tr("the validation does not match the current content; validate again"))
        if (p.get("policy") or {}).get("validate_mode") == "build" and p["validation"].get("mode") != "build":
            raise ValueError(tr("this proposal's policy requires a build validation"))
        if p["validation"].get("isolation") != dbt_sandbox.ISOLATION:
            raise ValueError(tr("old validation without isolation; validate again in the container"))
        backups, raw_backups, contents, expected = {}, {}, {}, {}
        for c in p["changes"]:
            original, _ = journal.read(journal.relative(c["file"]))
            text = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            if _h(text) != c["before_hash"]:
                raise ValueError(tr("file changed after the approval; create a new proposal"))
            expected[c["file"]] = original
            backups[c["file"]] = text
            raw_backups[c["file"]] = base64.b64encode(original).decode("ascii")
            contents[c["file"]] = c["after"].encode("utf-8")
        after = {**p, "backup": backups, "backup_bytes": raw_backups}
        return file_transaction.execute(journal, store, p, after, contents, "applied", actor or p["approval"]["user"], expected)   # audit: whoever applied, not only the approver


def rollback(store: Store, pid: str, actor: str | None = None) -> dict:
    p = store.proposal(pid)
    if not p or p["status"] != "applied" or not p.get("backup"):
        raise ValueError(tr("only applied proposals can be rolled back"))
    with file_transaction.locked(p) as journal:
        journal.require_idle()
        p = store.proposal(pid)
        if p["status"] != "applied":
            raise ValueError(tr("only applied proposals can be rolled back"))
        expected = {c["file"]: c["after"].encode("utf-8") for c in p["changes"]}
        if set(p["backup"]) != set(expected):
            raise ValueError(tr("the backup does not match the proposal files"))
        contents = {}
        for path, text in p["backup"].items():
            if journal.read(journal.relative(path))[0] != expected[path]:
                raise ValueError(tr("file changed after applying; rollback refused to preserve the edits"))
            raw = (p.get("backup_bytes") or {}).get(path)
            contents[path] = base64.b64decode(raw, validate=True) if raw is not None else text.encode("utf-8")
        return file_transaction.execute(journal, store, p, p, contents, "rolled_back", actor or "system", expected)


def recover(store: Store, pid: str, actor: str = "system") -> dict:
    p = store.proposal(pid)
    if not p:
        raise ValueError(tr("proposal not found"))
    with file_transaction.locked(p) as journal:
        return file_transaction.recover_locked(journal, store, pid, actor)
