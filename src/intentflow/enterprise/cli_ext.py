"""Enterprise CLI commands: fix, fix-ai, llm, sso, project, access."""
from __future__ import annotations

import json
import os
from pathlib import Path

from intentflow import dbt_loader
from intentflow.cli import _store, err
from intentflow.i18n import tr
from intentflow.store import Store


def cmd_llm(a) -> int:
    from . import jev, llm
    st = _store(a)
    try:
        if a.action == "status":
            print(json.dumps({"llm": {k: v for k, v in llm.status(st).items() if k != "providers"},
                              "jev": {k: v for k, v in jev.status(st).items() if k != "providers"}}, ensure_ascii=False, indent=1))
            return 0
        if a.action == "set":
            for k in ("provider", "model", "base_url"):
                v = getattr(a, k)
                if v:
                    st.set_setting(f"llm.{k}", v)
            if a.jev_provider:
                st.set_setting("jev.provider", a.jev_provider)
                st.set_setting("jev.enabled", True)
            print(tr("configured"))
            return 0
        if a.action in ("set-key", "set-jev-key"):
            import getpass
            key = os.environ.get("INTENTFLOW_NEW_KEY") or getpass.getpass(tr("key (not echoed): "))
            (llm.set_key if a.action == "set-key" else jev.set_key)(st, key)
            print(tr("key stored encrypted"))
            return 0
        if a.action == "models":
            print("\n".join(llm.list_models(st)))
            return 0
        if a.action == "test":
            r = llm.chat(st, "Reply with the single word: ok", "ping")
            print(f"ok ({r['model']}, {r['seconds']}s): {r['text'][:60]}")
            return 0
        if a.action == "jev-test":
            r = jev.decide(st, {"text": "column removed; 3 models read it"}, {"breaks": jev.noul("Does it break consumers?")})
            print(f"ok ({r['model']}, {r['seconds']}s): p(breaks)={r['answers']['breaks']['p']:.2f}")
            return 0
    except (llm.LLMError, jev.JevError) as e:
        err(str(e))
        return 1
    return 1


def cmd_fix_ai(a) -> int:
    from . import llm, llm_fix
    st = _store(a)
    target = a.target or st.get_setting("dbt.target")
    if not target:
        err(tr("pass --target or configure dbt.target"))
        return 1
    proj = dbt_loader.load(target, st.get_setting("dbt.root") or Path(target).parent)
    f = st.finding(a.id) or st.finding_by_prefix(a.id)
    if not f:
        err(tr("finding not found"))
        return 1
    try:
        r = llm_fix.suggest(st, proj, f, a.user, force_llm=a.force)
    except (llm.LLMError, ValueError) as e:
        err(str(e))
        return 1
    if r.get("proposal"):
        p = st.proposal(r["proposal"])
        print(tr("proposal {id} ({source}): {summary}", id=p["id"], source=r["source"], summary=p["summary"]))
        if p.get("explanation"):
            print(f"  {p['explanation']}")
        for c in p["changes"]:
            print(c["diff"])
        print(tr("review and approve: intentflow fix approve/validate/apply"))
    else:
        print(r.get("note", tr("nothing to do")))
    return 0


def cmd_validate_worker(a) -> int:
    """Run queued sandbox validations (needs Docker). The UI/watch containers need none: set INTENTFLOW_VALIDATION=remote there."""
    import time
    from . import proposals
    primary = _store(a)
    while True:
        n = 0
        stores = [primary]
        for path in (primary.get_setting("projects", {}) or {}).values():
            try:
                stores.append(Store(path))
            except Exception as e:        # one unreachable project must not stop the others
                err(f"{path.split('@')[-1]}: {e}")
        try:
            for st in stores:
                try:
                    n += proposals.process_validation_requests(st)
                except Exception as e:
                    err(str(e))
        finally:
            for st in stores[1:]:
                st.close()
        if n:
            print(tr("{n} validation(s) processed", n=n), flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


def cmd_fix(a) -> int:
    from . import proposals
    st = _store(a)
    if st.has_users() and a.action in ("approve", "reject", "validate", "apply", "rollback", "recover", "create"):
        who = next((u for u in st.users() if u["name"] == a.user and not u["disabled"]), None)
        if who is None or who["role"] == "viewer":
            err(tr("'{user}' is not an active user with the engineer/admin role (use --user or INTENTFLOW_USER)", user=a.user))
            return 1
    try:
        if a.action == "list":
            for p in st.proposals():
                print(f"{p['id']}  {p['status']:10} {p['summary']}")
        elif a.action == "create":
            target = a.target or st.get_setting("dbt.target")
            if not target:
                raise ValueError(tr("pass --target or configure dbt.target"))
            p = dbt_loader.load(target, a.root or Path(target).parent)
            f = st.finding(a.id)
            if not f:
                raise ValueError(tr("finding not in the store (run `check` first)"))
            pr = proposals.create(st, p, f, a.user)
            for c in pr["changes"]:
                print(c["diff"])
            print(tr("proposal {id} created; next: fix validate/approve", id=pr["id"]))
        elif a.action == "validate":
            r = proposals.validate_and_store(st, a.id, a.mode, a.user)["validation"]
            print(tr("passed") if r["ok"] else tr("FAILED"), r["mode"], tr("(isolated local DuckDB)"), "\n" + ("" if r["ok"] else r["output"]))
            return 0 if r["ok"] else 1
        elif a.action == "approve":
            proposals.approve(st, a.id, a.user)
            print(tr("approved (scope: apply the diff; does not run pipelines)"))
        elif a.action == "reject":
            proposals.reject(st, a.id, a.user)
            print(tr("rejected"))
        elif a.action == "apply":
            proposals.apply(st, a.id, a.user)            # audit names who ran it, not only the approver
            print(tr("applied; rollback available"))
        elif a.action == "recover":
            recovered = proposals.recover(st, a.id, a.user)
            print(tr("recovery finished; state: {status}", status=recovered["status"]))
        elif a.action == "rollback":
            proposals.rollback(st, a.id, a.user)
            print(tr("rolled back"))
    except ValueError as e:
        err(str(e))
        return 1
    return 0


def cmd_project(a) -> int:
    st = _store(a)
    reg = dict(st.get_setting("projects", {}) or {})
    if a.action == "add":
        if not (a.name and a.path):
            err(tr("pass the NAME and the PATH of the store"))
            return 1
        Store(a.path).close()          # create/validate the project's store
        reg[a.name] = a.path
        st.set_setting("projects", reg)
    elif a.action == "remove":
        reg.pop(a.name, None)
        st.set_setting("projects", reg)
    print(tr("main  (this store)"))
    from intentflow.web import _redact_dsn          # also covers ?password= and key=value DSNs
    for n, pth in reg.items():
        print(f"{n:12} {_redact_dsn(pth)}")
    return 0


def cmd_sso(a) -> int:
    st = _store(a)
    if a.action == "set":
        for k in ("issuer", "client_id", "client_secret_env", "default_role"):
            v = getattr(a, k)
            if v:
                st.set_setting("sso." + k, v)
        if a.auto_provision is not None:
            st.set_setting("sso.auto_provision", a.auto_provision == "yes")
        if a.allowed_domains is not None:
            st.set_setting("sso.allowed_domains", [d.strip().lower() for d in a.allowed_domains.split(",") if d.strip()])
        if a.group_claim:
            st.set_setting("sso.group_claim", a.group_claim)
        if a.group_map is not None:
            from .web_ext import parse_group_map
            st.set_setting("sso.group_map", parse_group_map(a.group_map))
        if a.unmapped_role:
            st.set_setting("sso.unmapped_role", a.unmapped_role)
        if a.refresh_seconds is not None:
            st.set_setting("sso.refresh_seconds", a.refresh_seconds)
        if a.require_group is not None:
            st.set_setting("sso.require_group", a.require_group == "yes")
        if a.sync_roles is not None:
            st.set_setting("sso.sync_roles", a.sync_roles == "yes")
    for k, v in st.settings("sso.").items():
        print(f"{k} = {json.dumps(v)}")
    return 0


def cmd_access(a) -> int:
    st = _store(a)
    try:
        if a.action == "project-role":
            st.set_project_role(a.name, a.project, None if a.role == "inherit" else a.role)
        else:
            st.set_scopes(a.name, a.project, [x.strip() for x in (a.patterns or "").split(",") if x.strip()] or None)
    except ValueError as e:
        err(str(e))
        return 1
    print("ok")
    return 0


def register(sub) -> None:
    user_default = os.environ.get("USER", "unknown")

    lm = sub.add_parser("llm", help=tr("AI: status | set | set-key | set-jev-key | models | test | jev-test"))
    lm.add_argument("action", choices=["status", "set", "set-key", "set-jev-key", "models", "test", "jev-test"])
    lm.add_argument("--provider", choices=["anthropic", "openai", "gemini", "local"])
    lm.add_argument("--model")
    lm.add_argument("--base-url", dest="base_url")
    lm.add_argument("--jev-provider", choices=["openrouter", "typesafe"], dest="jev_provider")
    lm.set_defaults(fn=cmd_llm)

    fa = sub.add_parser("fix-ai", help=tr("ask an LLM for a fix to a finding (becomes a pending proposal, with diff)"))
    fa.add_argument("id")
    fa.add_argument("--target")
    fa.add_argument("--force", action="store_true", help=tr("call the LLM even if Jev says a mechanical fix or no action is enough"))
    fa.add_argument("--user", default=user_default)
    fa.set_defaults(fn=cmd_fix_ai)

    f = sub.add_parser("fix", help=tr("governed fixes"))
    f.add_argument("action", choices=["list", "create", "validate", "approve", "reject", "apply", "rollback", "recover"])
    f.add_argument("id", nargs="?")
    f.add_argument("--target")
    f.add_argument("--root")
    f.add_argument("--mode", default="parse", choices=["parse", "build"])
    f.add_argument("--user", default=user_default)
    f.set_defaults(fn=cmd_fix)

    vw = sub.add_parser("validate-worker", help=tr("run queued fix validations on a host with Docker"))
    vw.add_argument("--once", action="store_true")
    vw.add_argument("--interval", type=float, default=5.0)
    vw.set_defaults(fn=cmd_validate_worker)

    pj = sub.add_parser("project", help=tr("several projects on one server (one store per project)"))
    pj.add_argument("action", choices=["list", "add", "remove"])
    pj.add_argument("name", nargs="?")
    pj.add_argument("path", nargs="?")
    pj.set_defaults(fn=cmd_project)

    ac = sub.add_parser("access", help=tr("per-project role or write scope of a user"))
    ac.add_argument("action", choices=["project-role", "scope"])
    ac.add_argument("name")
    ac.add_argument("--project", required=True)
    ac.add_argument("--role", choices=["viewer", "engineer", "admin", "none", "inherit"], default="inherit")
    ac.add_argument("--patterns", help=tr("for `scope`: comma-separated file/model patterns (e.g. 'models/marts/*,airflow:*')"))
    ac.set_defaults(fn=cmd_access)

    so = sub.add_parser("sso", help=tr("OpenID Connect single sign-on (secret only through an environment variable)"))
    so.add_argument("action", choices=["show", "set"])
    so.add_argument("--issuer")
    so.add_argument("--client-id", dest="client_id")
    so.add_argument("--client-secret-env", dest="client_secret_env")
    so.add_argument("--default-role", dest="default_role", choices=["viewer", "engineer", "admin"])
    so.add_argument("--auto-provision", dest="auto_provision", choices=["yes", "no"])
    so.add_argument("--allowed-domains", dest="allowed_domains", help=tr("allowed e-mail domains, comma-separated"))
    so.add_argument("--group-claim", dest="group_claim", help=tr("name of the claim with the groups (default: groups)"))
    so.add_argument("--group-map", dest="group_map", help=tr("rules 'group=role' or 'group=role@project', separated by ';'"))
    so.add_argument("--require-group", dest="require_group", choices=["yes", "no"], help=tr("deny people without a mapped group"))
    so.add_argument("--unmapped-role", dest="unmapped_role", choices=["none", "viewer", "engineer"],
                    help=tr("global role of people matching no global group (downgrades)"))
    so.add_argument("--refresh-seconds", dest="refresh_seconds", type=float,
                    help=tr("how often to re-read groups/validate the session (0 turns it off; default 300)"))
    so.add_argument("--sync-roles", dest="sync_roles", choices=["yes", "no"], help=tr("apply group roles also to users added by hand"))
    so.set_defaults(fn=cmd_sso)
