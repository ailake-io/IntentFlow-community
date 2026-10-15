"""IntentFlow CLI: checks, lineage, data diff, run collection, notifications and the UI."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import dbt_loader, extensions, lineage as lin, overview as ov
from .i18n import tr
from .model import SEVERITIES
from .store import Store


def _store(a) -> Store:
    return Store(a.store)


def err(msg: str) -> None:
    print(tr("error: {msg}", msg=msg), file=sys.stderr)


def cmd_check(a) -> int:
    from . import runner
    st = _store(a)
    over = {"target": a.target, "root": a.root, "base": a.base, "base_root": a.base_root, "airflow_dags": a.airflow_dags,
            "kestra_flows": a.kestra_flows, "duckdb": a.duckdb, "postgres_env": a.postgres_env}
    if a.save:
        for k, v in over.items():
            if v is not None:
                st.set_setting(runner.SOURCE_KEYS[k], v)
    stages = set(a.stage.split(",")) if getattr(a, "stage", None) else None
    try:
        r = runner.run_check(st, over, a.only.split(",") if a.only else None, stages)
    except ValueError as e:
        err(str(e))
        return 2
    findings, lineage, res = r["findings"], r["lineage"], r["store"]
    fmt = "json" if a.json else a.format
    if fmt == "json":
        print(json.dumps({"findings": [f.to_dict() for f in findings], "lineage": lineage, "store": res}, indent=2, default=str))
    elif fmt == "github":
        lvl = {"critical": "error", "high": "error", "medium": "warning", "low": "notice", "info": "notice"}
        for f in findings:
            file = f.evidence.get("file")
            loc = f"file={file}," if file else ""
            print(f"::{lvl[f.severity]} {loc}title={f.check}::{f.title}".replace("\n", " "))
    elif fmt == "markdown":
        from . import incidents as inc_mod, notify
        rows = [{**f.to_dict(), "check_id": f.check} for f in findings]
        print(notify.render_markdown(inc_mod.group(rows, r["project"])))
        print("\n<sub>" + tr("lineage resolved: {ratio}", ratio=f"{lineage['resolved_ratio']:.0%}") + "</sub>")
    else:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        for f in sorted(findings, key=lambda f: (order[f.severity], f.check)):
            print(f"[{f.severity:8}] {f.check:7} {f.title}")
        print("\n" + tr("{n} finding(s) | lineage resolved: {ratio} | new: {new}, resolved: {resolved}", n=len(findings),
                        ratio=f"{lineage['resolved_ratio']:.0%}", new=res["new"], resolved=res["resolved"]))
    if a.fail_on:
        gate = SEVERITIES[: SEVERITIES.index(a.fail_on) + 1]
        if any(f.severity in gate for f in findings):
            return 1
    return 0


def _parse_value(v: str):
    try:
        return json.loads(v)
    except ValueError:
        return v


def cmd_config(a) -> int:
    st = _store(a)
    if a.action == "set":
        st.set_setting(a.key, _parse_value(a.value))
        st.audit(os.environ.get("USER", "cli"), "config_set", {"key": a.key})
    elif a.action == "unset":
        st.unset_setting(a.key)
    elif a.action == "get":
        print(json.dumps(st.get_setting(a.key)))
    else:
        for k, v in st.settings().items():
            if not k.startswith("state."):
                print(f"{k} = {json.dumps(v, ensure_ascii=False)}")
    return 0


def cmd_policy(a) -> int:
    st = _store(a)
    if a.action == "pii-allow":
        cur = list(st.get_setting("policy.pii_allowed_models", []))
        if a.remove and a.name in cur:
            cur.remove(a.name)
        elif not a.remove and a.name not in cur:
            cur.append(a.name)
        st.set_setting("policy.pii_allowed_models", cur)
    elif a.action == "sla":
        st.set_setting(f"policy.sla.{a.name}", {"minutes": float(a.value)})   # name = system:pipeline
    elif a.action == "max-rows":
        st.set_setting("policy.max_estimated_rows", int(a.name))
    elif a.action == "disable-check":
        cur = set(st.get_setting("policy.disabled_checks", []))
        (cur.discard if a.remove else cur.add)(a.name)
        st.set_setting("policy.disabled_checks", sorted(cur))
    elif a.action == "max-age":
        st.set_setting(f"policy.max_age_hours.{a.name}", float(a.value))
    elif a.action == "airflow-target":      # target Airflow version: 3 turns on the migration check (AF3xx) in regular runs
        st.set_setting("policy.airflow_target_major", int(a.name))
    for k, v in st.policies().items():
        print(f"{k} = {json.dumps(v, ensure_ascii=False)}")
    return 0


def cmd_runs(a) -> int:
    from . import runner
    r = runner.collect_runs(_store(a), a.airflow, a.airflow_api, a.kestra, a.prefect, a.dagster, a.airbyte, a.fivetran, a.spark, a.databricks, a.argo, a.mage)
    for e in r["errors"]:
        print(tr("warning: {msg}", msg=e), file=sys.stderr)
    print(tr("{n} run(s) collected", n=r["runs"]))
    return 0 if not r["errors"] or r["runs"] else 1


def cmd_monitor(a) -> int:
    from . import runner
    n = runner.collect_samples(_store(a), host=a.host or not (a.host or a.docker), docker=a.docker or not (a.host or a.docker))
    print(tr("{n} sample(s)", n=n))
    return 0


def _target(a, st: Store) -> str | None:
    target = a.target or st.get_setting("dbt.target")
    if not target:
        err(tr("pass --target or run `intentflow config set dbt.target <dir>`"))
    return target


def cmd_lineage(a) -> int:
    st = _store(a)
    target = _target(a, st)
    if not target:
        return 2
    p = dbt_loader.load(target, a.root or Path(target).parent)
    r = lin.build(p, cache=st)      # only models whose SQL/schema changed since the last run are recomputed
    st.flush()
    from . import external
    decl = a.declared or str(Path(a.root or Path(target).parent) / external.DECLARED_FILE)
    if a.alias or a.unmatched or a.coverage:
        if a.alias:
            ds, _, model = a.alias.partition("=")
            if not ds or not model:
                err(tr("use --alias DATASET=MODEL"))
                return 2
            if external.match_dataset(p, model) is None:
                err(tr("'{model}' does not match a single project model", model=model))
                return 2
            external.add_alias(decl, ds, model)
            print(tr("alias saved in {file}: {dataset} → {model}", file=decl, dataset=ds, model=model))
        reads = external.load(st, decl, p)
        if a.unmatched:
            for ds, v in sorted(external.unmatched(p, reads).items()):
                sug = external.suggest(p, ds)
                print(tr("{reason}: {dataset}  (jobs: {jobs})", reason=tr(external.REASONS[v["reason"]]), dataset=ds, jobs=", ".join(v["jobs"]))
                      + ("  " + tr("suggestion: {names}", names=", ".join(sug)) if sug else ""))
        if a.coverage:
            for c in external.coverage(reads):
                print(tr("conservative propagation: {job}  (no feeds: {datasets})", job=c["job"], datasets=", ".join(c["reads_without_feeds"])))
    if a.column:
        m, c = a.column.split(".")
        uid = next(u for u, n in p.nodes.items() if n.name == m)
        for e in r.upstream_of(uid, c):
            print(f"{p.nodes[e.src].name}.{e.src_col} --{e.kind}{' (' + tr('masked') + ')' if e.masked else ''}--> {m}.{c}")
        for e in r.downstream_of(uid, c):
            print(f"{m}.{c} --{e.kind}--> {p.nodes[e.dst].name}.{e.dst_col}")
        if a.impact:      # transitive closure, including indirect dependency (join/filter)
            from . import lineage_api
            for x in lineage_api.impact(p, r, m, c, external=external.load(st, decl, p))["affected"]:
                print(tr("impact d={d}: ", d=x["distance"]) + ("job " if x.get("kind") == "job" else "") + x["name"]
                      + (" (" + tr("via job {job}", job=x["via_job"]) + ")" if x.get("via_job") else "")
                      + (f" ({', '.join(x['columns'])})" if x["columns"] else "")
                      + (" " + tr("via {what}", what=", ".join(x["indirect_via"])) if x.get("indirect_via") else ""))
    elif not (a.alias or a.unmatched or a.coverage):
        print(json.dumps(r.precision_report))
        for n, c, why in r.unresolved:
            print("  " + tr("unresolved: {column}: {why}", column=f"{p.nodes[n].name}.{c}", why=tr(why)))
    return 0


def cmd_notify(a) -> int:
    from . import incidents as inc_mod, notify
    st = _store(a)
    sinks, routes = st.get_setting("notify.sinks", []), st.get_setting("notify.routes", [])
    if a.action == "add-sink":
        if a.type not in notify.sink_types():
            err(tr("unknown sink type: {type}", type=a.type))
            return 2
        sink = {"name": a.name, "type": a.type}
        for k in ("url_env", "to", "repo", "pr", "api", "token_env", "min_severity"):
            v = getattr(a, k, None)
            if v:
                sink[k] = v
        st.set_setting("notify.sinks", [s for s in sinks if s["name"] != a.name] + [sink])
    elif a.action == "route":
        r = {"sink": a.name}
        for k in ("owner", "check_prefix", "min_severity"):
            v = getattr(a, k, None)
            if v:
                r[k] = v
        st.set_setting("notify.routes", routes + [r])
    elif a.action == "send":
        target = st.get_setting("dbt.target")
        proj = dbt_loader.load(target, Path(target).parent) if target else None
        incs = inc_mod.group(st.findings("open"), proj)
        for entry in notify.send(st, incs, dry_run=a.dry_run):
            print(f"{entry['incident']} -> {entry['sink']}: {entry['status']}")
        return 0
    print(json.dumps({"sinks": st.get_setting("notify.sinks", []), "routes": st.get_setting("notify.routes", [])}, ensure_ascii=False, indent=2))
    return 0


def cmd_openlineage(a) -> int:
    from . import openlineage
    if a.action == "serve":
        openlineage.serve(a.store, a.port)
        return 0
    if a.action == "emit":
        from . import ol_emit
        target = a.target or _store(a).get_setting("dbt.target")
        if not target or not a.url:
            err(tr("pass the catalog --url and --target (or configure dbt.target)"))
            return 1
        proj = dbt_loader.load(target, Path(target).parent)
        evs = ol_emit.build_events(proj, lin.build(proj, cache=_store(a)))
        if a.dry_run:
            print(json.dumps(evs, ensure_ascii=False, indent=1))
            return 0
        tok = os.environ.get("OPENLINEAGE_API_KEY")
        r = ol_emit.emit(a.url, evs, {"Authorization": f"Bearer {tok}"} if tok else None)
        print(tr("{sent}/{total} event(s) sent", sent=r["sent"], total=len(evs)))
        for e in r["errors"]:
            err(e)
        return 0 if not r["errors"] else 1
    r = openlineage.ingest_file(_store(a), a.file)
    print(tr("{runs} run(s), {edges} column edge(s)", runs=r["runs"], edges=r["column_edges"]))
    return 0


def cmd_watch(a) -> int:
    from . import runner
    interval = a.interval or float(_store(a).get_setting("watch.interval_s", 300))
    print(tr("scheduler running (every {s}s). Ctrl-C to stop.", s=f"{interval:g}"))
    try:
        runner.watch(_store(a), interval, a.iterations)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_backup(a) -> int:
    n = _store(a).backup(a.dest)
    remote = "://" in a.dest
    print(tr("backup written to {dest} ({n} {unit})", dest=a.dest.split("@")[-1] if remote else a.dest, n=n, unit=tr("rows") if remote else "bytes"))
    return 0


def cmd_migrate_store(a) -> int:
    from .store import copy_store
    src, dst = Store(a.src), Store(a.dst)
    if a.dst_has_data_ok is False and any(dst.con.execute(f"select 1 from {t} limit 1").fetchone() for t in ("findings", "users", "settings")):
        err(tr("the destination already has data; use --overwrite to replace it"))
        return 1
    n = copy_store(src, dst)
    print(tr("{n} rows copied from {src} to {dst}", n=n, src=src.backend, dst=dst.backend))
    return 0


def cmd_dbt_cloud(a) -> int:
    from .orchestrators.dbtcloud import DbtCloudClient
    st = _store(a)
    acc = a.account or st.get_setting("dbt_cloud.account_id")
    if not acc:
        err(tr("pass --account or configure dbt_cloud.account_id"))
        return 1
    c = DbtCloudClient(a.host or st.get_setting("dbt_cloud.host"), acc)
    if a.action == "runs":
        for r in c.runs(a.limit):
            print(f"{r.state:8} {r.pipeline:30} #{r.run_id}  {'' if r.duration is None else f'{r.duration:.0f}s'}")
        return 0
    job = a.job or st.get_setting("dbt_cloud.job_id")
    if not job:
        err(tr("pass --job or configure dbt_cloud.job_id"))
        return 1
    r = c.pull_artifacts(job, a.dest, a.run)
    print(tr("{n} artifact(s) in {dir}", n=len(r["files"]), dir=r["dir"]) + (" (" + tr("missing: {names}", names=", ".join(r["missing"])) + ")" if r["missing"] else ""))
    print(tr("use: intentflow config set dbt.target {dir}", dir=r["dir"]))
    return 0


def cmd_lineage_verify(a) -> int:
    from . import lineage_oracle
    target = a.target or _store(a).get_setting("dbt.target")
    db = a.duckdb or _store(a).get_setting("warehouse.duckdb")
    if not (target and db):
        err(tr("pass --target and --duckdb (or configure dbt.target and warehouse.duckdb)"))
        return 1
    proj = dbt_loader.load(target, Path(target).parent)
    res = lin.build(proj)
    rep = lineage_oracle.run(proj, res, db, a.package, a.max_columns, a.model, only_used=a.only_used, only_sources=a.source, sample_rows=a.sample_rows,
                             progress=lambda i, n, c: print(f"  [{i}/{n}] {c}", file=sys.stderr) if a.verbose else None)
    if a.out:
        Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    d, m = rep["direct"], rep["model_level"]
    pct = lambda v: "n/a" if v is None else f"{v:.1%}"
    print(tr("{models} model(s), {cols} source column(s) perturbed in {s}s", models=rep["models"], cols=rep["source_columns_tested"], s=rep["seconds"]))
    print("  " + tr("Direct lineage (column by column):"))
    print("    " + tr("hits {tp} · suspected false positives {fp} · precision ≥ {p}", tp=d["tp"], fp=d["fp_suspect"], p=pct(d["precision_lower_bound"])))
    print("    " + tr("changed only through indirect dependency (join/filter/...): {n} · truly lost: {lost}", n=d["changed_but_only_indirect"], lost=d["lost"]))
    print("  " + tr("With indirect dependency (per model):"))
    print("    " + tr("hits {tp} · suspected false positives {fp} · lost {fn} · precision ≥ {p} · recall {r}",
                      tp=m["tp"], fp=m["fp_suspect"], fn=m["fn"], p=pct(m["precision_lower_bound"]), r=pct(m["recall"])))
    for k, tag in (("false_negatives", "LOST"), ("suspect_false_positives", "FP?")):
        for x in rep[k][:a.show]:
            print(f"    {tag} {x}")
    return 0


def cmd_secrets(a) -> int:
    from .store import migrate_secrets
    st = _store(a)
    if a.action == "status":
        print(json.dumps(st.box.status()))
        return 0 if st.box.enabled else 1
    try:
        print(json.dumps(migrate_secrets(st)))
    except RuntimeError as e:
        err(str(e))
        return 1
    return 0


def cmd_otel_push(a) -> int:
    from . import otel
    try:
        st = otel.push(_store(a), a.endpoint)
    except Exception as e:
        err(str(e))
        return 1
    print(tr("metrics sent (HTTP {status})", status=st))
    return 0


def cmd_sqlmesh_export(a) -> int:
    from . import sqlmesh_loader
    data = sqlmesh_loader.export(a.project, a.gateway)
    out = Path(a.output or Path(a.project) / sqlmesh_loader.SNAPSHOT)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(tr("{n} model(s) exported to {file}", n=len(data["models"]), file=out))
    return 0


def cmd_airflow3(a) -> int:
    from .checks.airflow3 import migrate_dir
    res = migrate_dir(a.dags, write=a.write)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 1 if any(r["issues"] for r in res) and not a.write else 0
    total = sum(len(r["issues"]) for r in res)
    for r in res:
        print(f"\n{r['file']}")
        for p in r["issues"]:
            print(f"  L{p['line']:<4} {p['rule']} [{p['severity']}] {p['message']}\n        → {p['recommendation']}{'  (auto)' if p['fixable'] else ''}")
        if r["written"]:
            print("  " + tr("rewritten: {n} import(s); original in .af2.bak; {left} manual item(s) left", n=r["rewritable"], left=len(r["remaining"])))
    fix = sum(r["rewritable"] for r in res)
    print("\n" + tr("{files} file(s) with changes, {total} item(s) to handle, {fix} mechanical rewrite(s) {state}.", files=len(res), total=total, fix=fix,
                    state=tr("applied") if a.write else tr("possible (use --write)")))
    return 1 if total and not a.write else 0


def cmd_prune(a) -> int:
    print(json.dumps(_store(a).prune(a.days)))
    return 0


def cmd_fp(a) -> int:
    _store(a).mark_false_positive(a.id, a.user, a.reason or "")
    print(tr("marked as false positive (will not reopen)"))
    return 0


def cmd_stats(a) -> int:
    for c in _store(a).check_stats():
        print(f"{c['check']:8} total={c['total']:3} open={c['open']:3} resolved={c['resolved']:3} false+={c['false_positive']:3} "
              f"fp_rate={'—' if c['fp_rate'] is None else c['fp_rate']}")
    return 0


def cmd_diff(a) -> int:
    from . import datadiff, warehouses
    st = _store(a)
    wh = warehouses.connect(lambda k: (a.duckdb if k == "warehouse.duckdb" and a.duckdb else a.postgres_env if k == "warehouse.postgres_env" and a.postgres_env
                                       else st.get_setting(k)))
    if wh is None:
        err(tr("pass --duckdb/--postgres-env or configure `warehouse.type` (or `warehouse.duckdb`)"))
        return 2
    base, head = a.base, a.head
    if a.model:
        target = a.target or st.get_setting("dbt.target")
        p = dbt_loader.load(target, Path(target).parent)
        n = next((x for x in p.nodes.values() if x.name == a.model), None)
        if n is None:
            err(tr("model '{model}' not found", model=a.model))
            return 2
        db, sch, tbl = [x.strip('"`') for x in n.relation.split(".")]
        base = f'"{db}"."{a.base_schema or sch}"."{tbl}"'
        head = f'"{db}"."{a.head_schema or sch}"."{tbl}"'
    try:
        d = datadiff.diff_relations(wh, base, head, a.key.split(","), a.sample, sample_pct=a.sample_pct)
    except Exception as e:
        err(str(e))
        return 2
    print(json.dumps(d, indent=2, default=str) if a.json else datadiff.to_markdown(d))
    return 0 if d["identical"] or not a.fail_on_diff else 1


def cmd_risks(a) -> int:
    from . import external, risks
    st = _store(a)
    target = _target(a, st)
    if not target:
        return 2
    p = dbt_loader.load(target, a.root or Path(target).parent)
    res = lin.build(p, cache=st)
    st.flush()
    decl = a.declared or st.get_setting("lineage.declared") or str(Path(a.root or Path(target).parent) / external.DECLARED_FILE)
    rep = risks.build(p, res, st, base=a.base, declared=decl, reads=external.load(st, decl, p))
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else risks.to_markdown(rep))
    return 1 if (a.fail_on_high and rep["summary"]["high"]) else 0


def cmd_overview(a) -> int:
    print(json.dumps(ov.build(_store(a)), indent=2, default=str))
    return 0


def cmd_ui(a) -> int:
    from .web import serve
    serve(a.store, a.port, a.host, a.tls_cert, a.tls_key, a.behind_proxy)
    return 0


def cmd_user(a) -> int:
    st = _store(a)
    try:
        if a.action == "add":
            tok = st.add_user(a.name, a.role, expires_days=a.expires_days)
            st.audit(os.environ.get("USER", "cli"), "user_add", {"name": a.name, "role": a.role})
            print(tr("user {name} ({role}) created.\nToken (save it now; it will not be shown again):\n{token}", name=a.name, role=a.role, token=tok))
        elif a.action == "rotate":
            print(tr("new token (save it now):") + "\n" + st.rotate_token(a.name, a.expires_days))
        elif a.action in ("disable", "enable"):
            st.set_user(a.name, disabled=(a.action == "disable"))
            print("ok")
        elif a.action == "role":
            st.set_user(a.name, role=a.role)
            print("ok")
        elif a.action == "mfa-recovery":
            print(tr("recovery codes (save them; each works once):") + "\n" + "\n".join(st.mfa_recovery_codes(a.name)))
        elif a.action == "mfa-begin":
            from . import totp
            sec = st.mfa_begin(a.name)
            print(tr("TOTP secret for {name}: {secret}", name=a.name, secret=sec) + f"\n{totp.uri(sec, a.name)}\n"
                  + tr("confirm with: intentflow user mfa-confirm {name} --code 123456", name=a.name))
        elif a.action == "mfa-confirm":
            print(tr("MFA enabled") if st.mfa_confirm(a.name, a.code or "") else tr("invalid code"))
            return 0 if st.user_record(a.name)["totp_enabled"] else 1
        elif a.action == "mfa-disable":
            st.mfa_disable(a.name)
            print(tr("MFA disabled"))
        else:
            for u in st.users():
                print(f"{u['name']:24} {u['role']:9} {tr('disabled') if u['disabled'] else tr('active')}")
    except ValueError as e:
        err(str(e))
        return 1
    return 0


def cmd_assign(a) -> int:
    try:
        _store(a).assign(a.id, None if a.assignee == "-" else a.assignee, a.user)
    except ValueError as e:
        err(str(e))
        return 1
    print("ok")
    return 0


def cmd_note(a) -> int:
    try:
        _store(a).add_note(a.id, a.user, a.text)
    except ValueError as e:
        err(str(e))
        return 1
    print("ok")
    return 0


def cmd_ack(a) -> int:
    until = time.time() + a.days * 86400 if a.days else None
    _store(a).ack(a.id, a.user, a.reason, until)
    print("ok")
    return 0


def main(argv=None) -> int:
    from . import notify
    ap = argparse.ArgumentParser(prog="intentflow", description=__doc__)
    ap.add_argument("--version", action="version", version=f"intentflow {__import__('intentflow').__version__}")
    ap.add_argument("--store", default=os.environ.get("INTENTFLOW_STORE", ".intentflow.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    user_default = os.environ.get("USER", "unknown")

    c = sub.add_parser("check", help=tr("run the preventive checks"))
    c.add_argument("--target", help=tr("dbt target/ directory (default: saved setting `dbt.target`)"))
    c.add_argument("--root", help=tr("dbt project root (default: parent of the target)"))
    c.add_argument("--base", help=tr("target/ of the previous version (schema diff and impact)"))
    c.add_argument("--base-root")
    c.add_argument("--airflow-dags")
    c.add_argument("--kestra-flows")
    c.add_argument("--duckdb", help=tr("DuckDB file for probes"))
    c.add_argument("--postgres-env", help=tr("variable holding the PostgreSQL URL for probes"))
    c.add_argument("--save", action="store_true", help=tr("save the given sources as the default configuration"))
    c.add_argument("--only", help=tr("comma-separated ids/prefixes (e.g. SQL,DBT001)"))
    c.add_argument("--fail-on", choices=SEVERITIES)
    c.add_argument("--json", action="store_true")
    c.add_argument("--format", choices=["text", "json", "markdown", "github"], default="text")
    c.add_argument("--stage", help=tr("limit to stages (pre-merge,pre-run,continuous)"))
    c.set_defaults(fn=cmd_check)

    gt = sub.add_parser("gate", help=tr("gate before execution (use in Airflow/Kestra before dbt run)"))
    for arg in ("--target", "--root", "--base", "--base-root", "--airflow-dags", "--kestra-flows", "--duckdb", "--postgres-env", "--only"):
        gt.add_argument(arg)
    gt.add_argument("--fail-on", choices=SEVERITIES, default="critical")
    gt.add_argument("--format", choices=["text", "json", "markdown", "github"], default="text")
    gt.add_argument("--stage", default="pre-run,pre-merge")
    gt.set_defaults(fn=cmd_check, save=False, json=False)

    nt = sub.add_parser("notify", help=tr("outputs (Slack, webhook, e-mail, GitHub PR) and routes"))
    nt.add_argument("action", choices=["list", "add-sink", "route", "send"])
    nt.add_argument("name", nargs="?")
    nt.add_argument("--type", help=", ".join(notify.sink_types()))
    for opt in ("--url-env", "--to", "--repo", "--pr", "--api", "--token-env", "--owner", "--check-prefix"):
        nt.add_argument(opt)
    nt.add_argument("--min-severity", choices=SEVERITIES)
    nt.add_argument("--dry-run", action="store_true")
    nt.set_defaults(fn=cmd_notify)

    r = sub.add_parser("runs", help=tr("collect runs from orchestrators"))
    r.add_argument("--airflow")
    r.add_argument("--airflow-api", choices=["v1", "v2"])
    r.add_argument("--kestra")
    r.add_argument("--argo", help=tr("Argo Server URL (token in ARGO_TOKEN; namespace in argo.namespace)"))
    r.add_argument("--mage", help=tr("Mage URL"))
    r.add_argument("--spark", help=tr("Spark UI/History Server URL (e.g. http://driver:4040)"))
    r.add_argument("--databricks", help=tr("workspace host (token in DATABRICKS_TOKEN)"))
    for opt in ("--airbyte", "--fivetran", "--prefect", "--dagster"):
        r.add_argument(opt)
    r.set_defaults(fn=cmd_runs)

    m = sub.add_parser("monitor", help=tr("sample memory of the host/containers/pools"))
    m.add_argument("--host", action="store_true")
    m.add_argument("--docker", action="store_true")
    m.set_defaults(fn=cmd_monitor)

    l = sub.add_parser("lineage", help=tr("column lineage / precision report"))
    l.add_argument("--target", help=tr("default: saved setting `dbt.target`"))
    l.add_argument("--root")
    l.add_argument("--column", help=tr("model.column"))
    l.add_argument("--declared", help=tr("hand-declared lineage (default: <root>/intentflow.lineage.json)"))
    l.add_argument("--alias", help=tr("DATASET=MODEL: save an alias in intentflow.lineage.json"))
    l.add_argument("--coverage", action="store_true", help=tr("jobs whose propagation through outputs is conservative (reads without `feeds`)"))
    l.add_argument("--unmatched", action="store_true", help=tr("list external job datasets that do not match a model (missing/ambiguous)"))
    l.add_argument("--impact", action="store_true", help=tr("with --column: also list the transitive downstream impact"))
    l.set_defaults(fn=cmd_lineage)

    d = sub.add_parser("diff", help=tr("data diff between two relations (runs in the warehouse)"))
    d.add_argument("--base")
    d.add_argument("--head")
    d.add_argument("--model", help=tr("dbt model (uses --base-schema/--head-schema)"))
    d.add_argument("--base-schema")
    d.add_argument("--head-schema")
    d.add_argument("--target")
    d.add_argument("--key", required=True, help=tr("key column(s), comma-separated"))
    d.add_argument("--sample", type=int, default=5)
    d.add_argument("--sample-pct", type=float, default=None, help=tr("compare only ~N %% of the keys (hash); for huge tables"))
    d.add_argument("--duckdb")
    d.add_argument("--postgres-env")
    d.add_argument("--json", action="store_true")
    d.add_argument("--fail-on-diff", action="store_true")
    d.set_defaults(fn=cmd_diff)

    ol = sub.add_parser("openlineage", help=tr("OpenLineage: ingest <file> | serve | emit (send the project lineage to a catalog)"))
    ol.add_argument("action", choices=["ingest", "serve", "emit"])
    ol.add_argument("file", nargs="?")
    ol.add_argument("--url", help=tr("emit: catalog base URL (Marquez/DataHub/OpenMetadata); token in OPENLINEAGE_API_KEY"))
    ol.add_argument("--target", help=tr("emit: project (dbt target or SQLMesh)"))
    ol.add_argument("--dry-run", action="store_true")
    ol.add_argument("--port", type=int, default=5000)
    ol.set_defaults(fn=cmd_openlineage)

    w = sub.add_parser("watch", help=tr("scheduler: collect, check and notify periodically"))
    w.add_argument("--interval", type=float, help=tr("seconds (default: setting watch.interval_s or 300)"))
    w.add_argument("--iterations", type=int)
    w.set_defaults(fn=cmd_watch)

    mg = sub.add_parser("migrate-store", help=tr("copy the whole store between backends (e.g. SQLite → PostgreSQL)"))
    mg.add_argument("src", help=tr("source: SQLite file, postgresql://… or env:VAR"))
    mg.add_argument("dst", help=tr("destination: SQLite file, postgresql://… or env:VAR"))
    mg.add_argument("--overwrite", dest="dst_has_data_ok", action="store_true", default=False, help=tr("replace existing data in the destination"))
    mg.set_defaults(fn=cmd_migrate_store)

    bk = sub.add_parser("backup", help=tr("consistent copy of the store (safe while the UI is running)"))
    bk.add_argument("dest")
    bk.set_defaults(fn=cmd_backup)

    dc = sub.add_parser("dbt-cloud", help=tr("dbt Cloud: runs | pull (artifacts of the job's last successful run)"))
    dc.add_argument("action", choices=["runs", "pull"])
    dc.add_argument("--account")
    dc.add_argument("--host")
    dc.add_argument("--job")
    dc.add_argument("--run", help=tr("pull: a specific run instead of the last successful one"))
    dc.add_argument("--dest", default=".dbt_cloud_target")
    dc.add_argument("--limit", type=int, default=20)
    dc.set_defaults(fn=cmd_dbt_cloud)

    lv = sub.add_parser("lineage-verify", help=tr("measure lineage precision/recall by perturbing real data (DuckDB)"))
    lv.add_argument("--target")
    lv.add_argument("--duckdb")
    lv.add_argument("--package", help=tr("only models of this dbt package (e.g. jaffle_shop)"))
    lv.add_argument("--model", action="append", help=tr("only these models (repeatable)"))
    lv.add_argument("--max-columns", type=int)
    lv.add_argument("--source", action="append", default=None, help=tr("only these sources (`name` or `name.column`, repeatable)"))
    lv.add_argument("--sample-rows", type=int, default=None, help=tr("shrink each source to ~N rows (key hash; keeps joins) in the temporary copy"))
    lv.add_argument("--only-used", action="store_true", help=tr("test only columns the lineage says are read (fast, but hides false negatives)"))
    lv.add_argument("--out", help=tr("write the JSON report"))
    lv.add_argument("--show", type=int, default=15)
    lv.add_argument("-v", "--verbose", action="store_true")
    lv.set_defaults(fn=cmd_lineage_verify)

    sc = sub.add_parser("secrets", help=tr("encryption at rest: status | migrate (encrypt old plain-text secrets)"))
    sc.add_argument("action", choices=["status", "migrate"])
    sc.set_defaults(fn=cmd_secrets)

    ot = sub.add_parser("otel-push", help=tr("send metrics to the OpenTelemetry Collector (OTLP/HTTP)"))
    ot.add_argument("--endpoint", help=tr("default: OTEL_EXPORTER_OTLP_ENDPOINT"))
    ot.set_defaults(fn=cmd_otel_push)

    sx = sub.add_parser("sqlmesh-export", help=tr("export a SQLMesh project to JSON (usable as --target, without SQLMesh in CI)"))
    sx.add_argument("project")
    sx.add_argument("-o", "--output")
    sx.add_argument("--gateway")
    sx.set_defaults(fn=cmd_sqlmesh_export)

    a3 = sub.add_parser("airflow3", help=tr("Airflow 2 → 3 migration: find what breaks in the DAGs and rewrite mechanical imports"))
    a3.add_argument("dags", help=tr("folder with the DAGs"))
    a3.add_argument("--write", action="store_true", help=tr("apply the mechanical import changes (keeps .af2.bak)"))
    a3.add_argument("--json", action="store_true")
    a3.set_defaults(fn=cmd_airflow3)

    pr = sub.add_parser("prune", help=tr("delete old data from the store"))
    pr.add_argument("--days", type=float, default=30)
    pr.set_defaults(fn=cmd_prune)

    fp = sub.add_parser("fp", help=tr("mark a finding as a false positive"))
    fp.add_argument("id")
    fp.add_argument("--reason")
    fp.add_argument("--user", default=user_default)
    fp.set_defaults(fn=cmd_fp)

    sub.add_parser("stats", help=tr("precision per check (false positives)")).set_defaults(fn=cmd_stats)
    sub.add_parser("overview", help=tr("overview as JSON")).set_defaults(fn=cmd_overview)

    rk = sub.add_parser("risks", help=tr("what can break in the whole process: visibility gaps, fragile points and process failures"))
    rk.add_argument("--target", help=tr("default: saved setting `dbt.target`"))
    rk.add_argument("--root")
    rk.add_argument("--base", help=tr("target of the base version (the PR gate depends on it)"))
    rk.add_argument("--declared", help=tr("declared lineage (default: <root>/intentflow.lineage.json)"))
    rk.add_argument("--json", action="store_true")
    rk.add_argument("--fail-on-high", action="store_true", help=tr("exit 1 if there is a high severity item"))
    rk.set_defaults(fn=cmd_risks)

    u = sub.add_parser("ui", help=tr("web interface"))
    u.add_argument("--port", type=int, default=8787)
    u.add_argument("--host", default="127.0.0.1", help=tr("outside loopback requires users and HTTPS (or --behind-proxy)"))
    u.add_argument("--tls-cert")
    u.add_argument("--tls-key")
    u.add_argument("--behind-proxy", action="store_true", help=tr("TLS terminated at a reverse proxy"))
    u.set_defaults(fn=cmd_ui)

    us = sub.add_parser("user", help=tr("team: users and roles (viewer, engineer, admin)"))
    us.add_argument("action", choices=["list", "add", "rotate", "disable", "enable", "role", "mfa-begin", "mfa-confirm", "mfa-disable", "mfa-recovery"])
    us.add_argument("name", nargs="?")
    us.add_argument("--role", default="viewer", choices=["viewer", "engineer", "admin"])
    us.add_argument("--code")
    us.add_argument("--expires-days", dest="expires_days", type=float, help=tr("token validity (add/rotate)"))
    us.set_defaults(fn=cmd_user)

    asg = sub.add_parser("assign", help=tr("assign a finding to someone ('-' removes)"))
    asg.add_argument("id")
    asg.add_argument("assignee")
    asg.add_argument("--user", default=user_default)
    asg.set_defaults(fn=cmd_assign)

    nt2 = sub.add_parser("note", help=tr("comment on a finding"))
    nt2.add_argument("id")
    nt2.add_argument("text")
    nt2.add_argument("--user", default=user_default)
    nt2.set_defaults(fn=cmd_note)

    g = sub.add_parser("config", help=tr("settings (set/get/unset/list) kept in the store"))
    g.add_argument("action", choices=["set", "get", "unset", "list"])
    g.add_argument("key", nargs="?")
    g.add_argument("value", nargs="?")
    g.set_defaults(fn=cmd_config)

    po = sub.add_parser("policy", help=tr("policies (allowed PII, SLA, limits)"))
    po.add_argument("action", choices=["list", "pii-allow", "sla", "max-rows", "max-age", "disable-check", "airflow-target"])
    po.add_argument("name", nargs="?", help=tr("model | pipeline | value"))
    po.add_argument("value", nargs="?", help=tr("SLA minutes"))
    po.add_argument("--remove", action="store_true")
    po.set_defaults(fn=cmd_policy)

    k = sub.add_parser("ack", help=tr("acknowledge a finding (optionally until a date)"))
    k.add_argument("id")
    k.add_argument("--reason", required=True)
    k.add_argument("--days", type=float)
    k.add_argument("--user", default=user_default)
    k.set_defaults(fn=cmd_ack)

    extensions.each("register_cli", sub)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
