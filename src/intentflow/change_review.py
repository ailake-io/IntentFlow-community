"""Compare declared controls/interfaces before deployment; never execute project code.

Changed test SQL needs review: arbitrary SQL cannot reliably be classified as
stronger or weaker. Digests bind an acknowledgement to the exact change, while
the public report deliberately omits raw SQL and arbitrary custom metadata.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import sqlglot

from .i18n import tr

RELATIONS = {"model", "seed", "snapshot", "source"}
TESTS = {"test", "unit_test"}
MAX_IMPACT_PREVIEW = 100


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _sql(value):
    """Ignore layout/comments and pure dbt config calls, preserving literal values."""
    if not value:
        return []
    try:
        tokens = sqlglot.tokenize(value)
        out, i = [], 0
        while i < len(tokens):
            if [t.token_type.name for t in tokens[i:i+4]] == ["L_BRACE", "L_BRACE", "VAR", "L_PAREN"] and tokens[i+2].text == "config":
                depth, j = 1, i + 4
                while j < len(tokens) and depth:
                    depth += (tokens[j].token_type.name == "L_PAREN") - (tokens[j].token_type.name == "R_PAREN")
                    j += 1
                if not depth and [t.token_type.name for t in tokens[j:j+2]] == ["R_BRACE", "R_BRACE"]:
                    i = j + 2
                    continue
            token = tokens[i]
            kind = token.token_type.name
            text = token.text if kind in {"VAR", "IDENTIFIER"} or "STRING" in kind else token.text.upper()
            out.append((kind, text))
            i += 1
        return out
    except Exception:
        # Unsupported syntax remains significant; never discard an unknown body.
        return {"unparsed": value.replace("\r\n", "\n").strip()}


def _nodes(manifest):
    out = {}
    for key in ("nodes", "sources", "exposures", "semantic_models", "metrics", "saved_queries", "unit_tests"):
        section = manifest.get(key) or {}
        if not isinstance(section, dict) or any(not isinstance(n, dict) for n in section.values()):
            raise ValueError(tr("change comparison requires valid manifest resources"))
        for uid, node in section.items():
            if not isinstance(node.get("config") or {}, dict) or not isinstance(node.get("depends_on") or {}, dict):
                raise ValueError(tr("change comparison requires valid manifest resources"))
            if (node.get("config") or {}).get("enabled", True):
                out[uid] = node
    return out


def _code(node):
    raw = node.get("raw_code") or node.get("raw_sql") or ""
    if (node.get("language") or "sql").lower() != "sql":
        return raw.replace("\r\n", "\n")  # Python indentation is significant.
    return _sql(raw)


def _tags(node):
    return set(node.get("tags") or []) | set((node.get("config") or {}).get("tags") or [])


def _anchor(node):
    metadata = node.get("test_metadata") or {}
    deps = sorted((node.get("depends_on") or {}).get("nodes") or [])
    if metadata:
        return _hash(["generic", node.get("package_name"), metadata.get("namespace"), metadata.get("name"),
                      node.get("attached_node") or deps,
                      node.get("column_name") or (metadata.get("kwargs") or {}).get("column_name")])
    return _hash([node.get("resource_type"), node.get("package_name"), node.get("name")])


def _macros(manifest):
    definitions = manifest.get("macros") or {}
    if not isinstance(definitions, dict):
        raise ValueError(tr("change comparison requires valid macro definitions"))
    cache = {}
    def dependencies(node):
        found, stack = {}, list((node.get("depends_on") or {}).get("macros") or [])
        while stack:
            uid = stack.pop()
            if uid in found:
                continue
            macro = definitions.get(uid)
            if not isinstance(macro, dict):
                found[uid] = "missing"
                continue
            if uid not in cache:
                cache[uid] = _hash(_sql(macro.get("macro_sql")))
            found[uid] = cache[uid]
            stack.extend((macro.get("depends_on") or {}).get("macros") or [])
        return sorted(found.items())
    return dependencies


def _test_parts(node, macros):
    cfg = node.get("config") or {}
    policy = {key: cfg.get(key) for key in ("where", "limit")}
    # Runtime partition dates are data context, not necessarily a changed rule.
    unrendered = node.get("unrendered_config") or {}
    if "where" in unrendered:
        policy["where"] = unrendered["where"]
    policy.update(severity=str(cfg.get("severity") or "error").lower(),
                  error_if=cfg.get("error_if") or "!= 0", warn_if=cfg.get("warn_if") or "!= 0",
                  fail_calc=cfg.get("fail_calc") or "count(*)")
    descriptive = {"tags", "meta", "description", "docs", "group", "alias", "schema", "database", "store_failures", "store_failures_as", "grants", "persist_docs", "enabled"}
    policy["additional_config"] = {key: value for key, value in cfg.items() if key not in descriptive and key not in policy
                                    and value is not None and value != [] and value != {}}
    return {"sql": _sql(node.get("raw_code") or node.get("raw_sql")),
            "arguments": (node.get("test_metadata") or {}).get("kwargs") or {},
            "inputs": sorted((node.get("depends_on") or {}).get("nodes") or []),
            "policy": policy, "macros": macros(node),
            "unit_fixtures": {key: node.get(key) for key in ("given", "expect", "overrides", "model")}}


def compare(base, head, base_catalog, head_catalog, *, review_model_logic=False):
    for key in ("project_name", "adapter_type"):
        old, new = (base.get("metadata") or {}).get(key), (head.get("metadata") or {}).get(key)
        if not old or old != new:
            raise ValueError(tr("base/head metadata differs or is absent: {key}", key=key))
    old_nodes, new_nodes = _nodes(base), _nodes(head)
    gaps = []
    for side, graph, catalog in (("base", old_nodes, base_catalog), ("head", new_nodes, head_catalog)):
        for uid, node in graph.items():
            if node.get("resource_type") not in RELATIONS or (node.get("config") or {}).get("materialized") == "ephemeral":
                continue
            section = catalog.get("sources" if node.get("resource_type") == "source" else "nodes") or {}
            if not isinstance(section, dict) or not isinstance(section.get(uid), dict) or not section[uid].get("columns"):
                gaps.append({"side": side, "resource": uid, "message": str(tr("Catalog columns unavailable for change comparison."))})
    all_nodes = {**old_nodes, **new_nodes}
    children = defaultdict(set)
    for graph in (old_nodes, new_nodes):
        for uid, node in graph.items():
            if node.get("resource_type") in TESTS:
                continue
            for parent in (node.get("depends_on") or {}).get("nodes") or []:
                children[parent].add(uid)
    def impact(roots):
        seen, stack = set(roots), sorted(roots)
        truncated = len(seen) > MAX_IMPACT_PREVIEW
        while stack and not truncated:
            for uid in sorted(children[stack.pop()]):
                if uid not in seen:
                    seen.add(uid)
                    stack.append(uid)
                    if len(seen) > MAX_IMPACT_PREVIEW:
                        truncated = True
                        break
        return ([{"id": uid, "name": all_nodes[uid].get("name", uid), "type": all_nodes[uid].get("resource_type")}
                 for uid in sorted(seen)[:MAX_IMPACT_PREVIEW] if uid in all_nodes], truncated)
    changes = []
    def add(kind, resource, before, after, message, *, review=True, roots=None, fields=()):
        identity = {"kind": kind, "resource": resource, "before": before, "after": after}
        node = new_nodes.get(resource) or old_nodes.get(resource) or {}
        meta = {**(node.get("meta") or {}), **((node.get("config") or {}).get("meta") or {})}
        owner = meta.get("owner") or meta.get("team")
        affected, truncated = impact([resource] if roots is None else roots)
        changes.append({"id": "CHG-" + _hash(identity)[:24], "kind": kind, "resource": resource,
                        "name": node.get("name", resource), "requires_review": review, "message": str(message),
                        "before": before, "after": after, "changed_fields": list(fields),
                        "owner": owner if isinstance(owner, str) else None, "impact": affected, "impact_truncated": truncated})
    old_macros, new_macros = _macros(base), _macros(head)
    for uid, node in old_nodes.items():
        if node.get("resource_type") not in RELATIONS:
            continue
        other = new_nodes.get(uid)
        if other is None:
            add("resource_removed", uid, _hash(node), None, tr("Existing model or source removed; verify its consumers."))
            continue
        a, b = node.get("config") or {}, other.get("config") or {}
        old_alias = node.get("alias") or node.get("identifier") or a.get("alias") or node.get("name")
        new_alias = other.get("alias") or other.get("identifier") or b.get("alias") or other.get("name")
        if old_alias != new_alias:
            add("relation_alias_changed", uid, {"alias": old_alias}, {"alias": new_alias}, tr("Physical table/view name changed; verify consumers outside dbt."))
        if (a.get("contract") or {}).get("enforced") and not (b.get("contract") or {}).get("enforced"):
            add("model_contract_disabled", uid, True, False, tr("Model contract enforcement was disabled."))
        def constraints(n):
            values = list(n.get("constraints") or [])
            values += [{"column": col, "constraint": item} for col, value in (n.get("columns") or {}).items() for item in value.get("constraints") or []]
            return Counter(_hash(value) for value in values)
        old_constraints, new_constraints = constraints(node), constraints(other)
        dropped = old_constraints - new_constraints
        if dropped:
            add("constraints_removed", uid, sorted(old_constraints.elements()), sorted(new_constraints.elements()), tr("Declared model/column constraints were removed or replaced."))
        if "incremental" in (a.get("materialized"), b.get("materialized")):
            old_key = {k: a.get(k) for k in ("materialized", "unique_key", "incremental_strategy")}
            new_key = {k: b.get(k) for k in old_key}
            if old_key != new_key:
                add("incremental_changed", uid, _hash(old_key), _hash(new_key), tr("Incremental materialization, key or strategy changed; verify retry and backfill behavior."), fields=[k for k in old_key if old_key[k] != new_key[k]])
        if node.get("resource_type") == "source":
            def freshness(n):
                cfg = n.get("config") or {}
                effective = cfg.get("freshness", n.get("freshness"))
                effective = dict(effective) if isinstance(effective, dict) else effective
                raw = ((n.get("unrendered_config") or {}).get("freshness") or {})
                if isinstance(effective, dict) and "filter" in raw:
                    effective["filter"] = raw["filter"]
                return {"freshness": effective,
                        "loaded_at_field": cfg.get("loaded_at_field", n.get("loaded_at_field")),
                        "loaded_at_query": cfg.get("loaded_at_query", n.get("loaded_at_query"))}
            f0, f1 = freshness(node), freshness(other)
            if f0 != f1 and any(isinstance((f0["freshness"] or {}).get(k), dict) and f0["freshness"][k].get("count") is not None for k in ("error_after", "warn_after")):
                add("source_freshness_changed", uid, _hash(f0), _hash(f1), tr("Source freshness policy changed; verify thresholds, filters and the reference clock."))
        p0 = {"code": _code(node), "macros": old_macros(node)}
        p1 = {"code": _code(other), "macros": new_macros(other)}
        if p0 != p1:
            add("model_logic_changed", uid, _hash(p0), _hash(p1), tr("Model SQL or a declared macro dependency changed; compare data results."), review=review_model_logic)
    for uid, node in new_nodes.items():
        if uid not in old_nodes and node.get("resource_type") in RELATIONS:
            add("resource_added", uid, None, _hash(node), tr("Model or source added; validate its data and consumers."), review=False)
    for uid in sorted(set(old_nodes) & set(new_nodes)):
        if old_nodes[uid].get("resource_type") not in RELATIONS:
            continue
        old_cols = ((base_catalog.get("sources") if old_nodes[uid].get("resource_type") == "source" else base_catalog.get("nodes")) or {}).get(uid, {}).get("columns") or {}
        new_cols = ((head_catalog.get("sources") if new_nodes[uid].get("resource_type") == "source" else head_catalog.get("nodes")) or {}).get(uid, {}).get("columns") or {}
        for col in sorted(set(old_cols) - set(new_cols)):
            add("column_removed", uid, {"column": col, "type": old_cols[col].get("type")}, None, tr("Column removed; absence of declared consumers does not prove it is unused."))
        for col in sorted(set(new_cols) - set(old_cols)):
            add("column_added", uid, None, {"column": col, "type": new_cols[col].get("type")}, tr("Column added; strict-schema consumers may still need validation."), review=False)
        for col in sorted(set(old_cols) & set(new_cols)):
            a, b = old_cols[col].get("type"), new_cols[col].get("type")
            if str(a).lower() != str(b).lower():
                add("column_type_changed", uid, {"column": col, "type": a}, {"column": col, "type": b}, tr("Column type changed; verify casts, precision and consumers."))

    before = {u:n for u,n in old_nodes.items() if n.get("resource_type") in TESTS}
    after = {u:n for u,n in new_nodes.items() if n.get("resource_type") in TESTS}
    old_parts = {u:_test_parts(n, old_macros) for u,n in before.items()}
    new_parts = {u:_test_parts(n, new_macros) for u,n in after.items()}
    def pair(left, right):
        n0, n1 = before[left], after[right]
        roots = sorted(set((old_parts[left]["inputs"] or []) + (new_parts[right]["inputs"] or [])))
        changed = [key for key in old_parts[left] if old_parts[left][key] != new_parts[right][key]]
        if changed:
            add("test_definition_changed", right, _hash(old_parts[left]), _hash(new_parts[right]),
                tr("An existing test definition changed; arbitrary SQL cannot be assumed to be stronger."), roots=roots, fields=changed)
        if "business_rule" in _tags(n0) and "business_rule" not in _tags(n1):
            add("business_tracking_removed", right, True, False, tr("Business-rule tracking was removed from an existing test."), roots=roots)
    for uid in sorted(set(before) & set(after)):
        pair(uid, uid)
    left, right = defaultdict(list), defaultdict(list)
    for uid in sorted(set(before) - set(after)):
        left[_anchor(before[uid])].append(uid)
    for uid in sorted(set(after) - set(before)):
        right[_anchor(after[uid])].append(uid)
    for anchor in sorted(set(left) | set(right)):
        remaining_old, remaining_new = list(left[anchor]), list(right[anchor])
        for uid in list(remaining_old):
            matches = [v for v in remaining_new if old_parts[uid] == new_parts[v]]
            if matches:
                match = min(matches, key=lambda v:(('business_rule' in _tags(before[uid])) != ('business_rule' in _tags(after[v])), v))
                pair(uid, match)
                remaining_old.remove(uid)
                remaining_new.remove(match)
        if len(remaining_old) == len(remaining_new) == 1:
            pair(remaining_old[0], remaining_new[0])
        elif remaining_old and remaining_new:
            roots = sorted({v for u in remaining_old for v in old_parts[u]["inputs"]} | {v for u in remaining_new for v in new_parts[u]["inputs"]})
            add("test_set_changed", remaining_new[0], sorted(_hash(old_parts[u]) for u in remaining_old),
                sorted(_hash(new_parts[u]) for u in remaining_new), tr("Several tests share an identity; review the changed set without assuming a match."), roots=roots)
        else:
            for uid in remaining_old:
                add("test_removed", uid, _hash(old_parts[uid]), None, tr("An existing test was removed or disabled, even if other tests still pass."), roots=old_parts[uid]["inputs"])
            for uid in remaining_new:
                add("test_added", uid, None, _hash(new_parts[uid]), tr("Test added; confirm that it exercises the intended expectation."), review=False, roots=new_parts[uid]["inputs"])
    changes.sort(key=lambda c:(not c["requires_review"], c["resource"], c["kind"], c["id"]))
    return {"changes": changes, "gaps": gaps, "summary": {"total": len(changes), "review_required": sum(c["requires_review"] for c in changes),
                                             "by_kind": dict(Counter(c["kind"] for c in changes))},
            "baseline_revision": ((base.get("metadata") or {}).get("env") or {}).get("INTENTFLOW_REVISION"),
            "candidate_revision": ((head.get("metadata") or {}).get("env") or {}).get("INTENTFLOW_REVISION"),
            "note": str(tr("Impact includes declared baseline/candidate dependencies, not every external consumer. Review acknowledgements are not verified approvals."))}


def load(target, base, *, review_model_logic=False, baseline_revision=None):
    if Path(target).resolve() == Path(base).resolve():
        raise ValueError(tr("candidate and baseline must be different artifact locations"))
    def read(folder, name):
        try:
            doc = json.loads((Path(folder) / name).read_text())
            if not isinstance(doc, dict):
                raise ValueError("expected object")
            return doc
        except (OSError, ValueError, UnicodeError) as e:
            raise ValueError(tr("change comparison artifact unavailable or invalid: {name}", name=name)) from e
    try:
        old, old_catalog = read(base, "manifest.json"), read(base, "catalog.json")
        if baseline_revision is not None:
            if not baseline_revision.strip():
                raise ValueError(tr("expected artifact revision must not be empty"))
            for doc in (old, old_catalog):
                if ((doc.get("metadata") or {}).get("env") or {}).get("INTENTFLOW_REVISION") != baseline_revision:
                    raise ValueError(tr("baseline artifacts do not match the expected deployed revision"))
        return compare(old, read(target, "manifest.json"), old_catalog, read(target, "catalog.json"), review_model_logic=review_model_logic)
    except (TypeError, KeyError, AttributeError) as e:
        raise ValueError(tr("change comparison requires valid manifest resources")) from e


def acknowledge(report, acknowledgements=()):
    reasons = {}
    available = {c["id"] for c in report["changes"] if c["requires_review"]}
    for item in acknowledgements:
        identity, separator, reason = item.partition("=")
        if not separator or not reason.strip() or identity not in available or identity in reasons:
            raise ValueError(tr("accept-change requires an exact current change id and a nonempty reason"))
        reasons[identity] = reason.strip()
    for change in report["changes"]:
        change["acknowledged"] = change["id"] in reasons
        if change["acknowledged"]:
            change["reason"] = reasons[change["id"]]
    report["unreviewed"] = [c["id"] for c in report["changes"] if c["requires_review"] and not c["acknowledged"]]
    return report
