"""Enterprise routes for the IntentFlow UI/API: SSO (OIDC), per-project roles and scopes, multiple projects,
governed fixes, AI suggestions and triage."""
from __future__ import annotations

import json
import urllib.error
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

from intentflow import auth
from intentflow.http_utils import RequestError, read_body
from intentflow.i18n import tr
from intentflow.store import Store
from intentflow.web import DEFAULT_PROJECT, cookie, scope_ok

from . import oidc as oidc_mod

SSO_KEYS = ("issuer", "client_id", "client_secret_env", "default_role", "auto_provision", "allowed_domains",
            "group_claim", "group_map", "require_group", "sync_roles", "unmapped_role", "refresh_seconds")

_AI_JOBS: dict[tuple[str, str], dict] = {}  # isolated per store and finding
_AI_JOBS_LOCK = threading.Lock()


def parse_group_map(text: str) -> list[dict]:
    """One rule per line: `group=role` or `group=role@project` (e.g. `data-admin=admin`, `crm-eng=engineer@crm`)."""
    out = []
    for line in text.replace(";", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(tr("invalid rule '{line}' (use group=role or group=role@project)", line=line))
        g, r = line.split("=", 1)
        r, _, p = r.partition("@")
        if r.strip() not in ("none", "viewer", "engineer", "admin"):
            raise ValueError(tr("invalid role in '{line}'", line=line))
        out.append({"group": g.strip(), "role": r.strip(), **({"project": p.strip()} if p.strip() else {})})
    return out


_OIDC_CACHE: dict = {}
_OIDC_LOCK = threading.Lock()


def _oidc(h, prim: Store):
    if h.options.get("oidc_client") is not None:
        return h.options["oidc_client"]
    iss, cid = prim.get_setting("sso.issuer"), prim.get_setting("sso.client_id")
    if not (iss and cid):
        return None
    secret = os.environ.get(prim.get_setting("sso.client_secret_env") or "", "")
    scheme = "https" if h._secure_cookie() else "http"
    redirect = f"{scheme}://{h.headers.get('Host', '')}/auth/sso/callback"
    # one client per configuration: keeps discovery/JWKS cached, so unauthenticated hits do not each fetch from the provider
    key = (iss, cid, secret, redirect)
    with _OIDC_LOCK:
        hit = _OIDC_CACHE.get(key)
        if hit is None or time.time() - hit[0] > 600:
            if len(_OIDC_CACHE) >= 16:
                _OIDC_CACHE.clear()
            hit = _OIDC_CACHE[key] = (time.time(), oidc_mod.OIDC(iss, cid, secret, redirect))
        return hit[1]


def _sync_roles(prim: Store, rec: dict, claims: dict) -> None:
    """Apply groups → roles (accounts managed by SSO, or all of them with `sso.sync_roles`)."""
    gmap = prim.get_setting("sso.group_map", []) or []
    if not gmap or not (rec.get("sso_managed") or prim.get_setting("sso.sync_roles", False)):
        return
    g_role, g_proj = oidc_mod.map_groups(claims, prim.get_setting("sso.group_claim", "groups"), gmap)
    if claims.get("_groups_overage"):
        prim.audit("sso", "groups_overage", {"user": rec["name"], "note": "group list missing from the token; treated as empty"})
    if g_role is None:                          # no global group matched: unmapped_role, else the floor (a removed group must demote)
        g_role = prim.get_setting("sso.unmapped_role") or prim.get_setting("sso.default_role", "viewer")
    # Only projects the group map speaks about are managed here; a stored "none" (explicit block) is never lifted by the provider.
    current = json.loads(rec.get("project_roles") or "{}")
    covered = {m["project"] for m in gmap if m.get("project")}
    merged = {k: v for k, v in current.items() if k not in covered or v == "none"}
    for k, v in g_proj.items():
        if merged.get(k) != "none":
            merged[k] = v
    prim.sso_sync(rec["name"], g_role, merged)
    if g_role != rec["role"] or merged != current:
        prim.audit("sso", "roles_synced", {"user": rec["name"], "role": g_role, "projects": merged})


# ---- hooks ----
def me(h, prim: Store, who) -> dict:
    return {"sso": bool(h.options.get("oidc_client") is not None or (prim.get_setting("sso.issuer") and prim.get_setting("sso.client_id")))}


_REFRESH_LOCKS: dict[str, list] = {}     # sid -> [lock, users]; removed only when nobody holds or waits for it
_REFRESH_GUARD = threading.Lock()


def refresh_session(h, prim: Store, sid: str, rec_name: str) -> bool:
    """One refresh per session at a time: two concurrent requests presenting the same rotating refresh token would make the second
    fail with invalid_grant and log the person out. (Per process; several server processes rely on provider tolerance.)"""
    with _REFRESH_GUARD:
        slot = _REFRESH_LOCKS.setdefault(sid, [threading.Lock(), 0])
        slot[1] += 1
    try:
        with slot[0]:
            return _refresh_session(h, prim, sid, rec_name)
    finally:
        with _REFRESH_GUARD:
            slot[1] -= 1
            if slot[1] == 0 and _REFRESH_LOCKS.get(sid) is slot:
                del _REFRESH_LOCKS[sid]


def _refresh_session(h, prim: Store, sid: str, rec_name: str) -> bool:
    """Refresh the SSO session periodically: re-read groups and end the session if the provider refuses."""
    every = float(prim.get_setting("sso.refresh_seconds", 300) or 0)
    info = prim.session_info(sid)
    if not info or info.get("via") != "sso" or every <= 0 or not info.get("refresh_token"):
        return True
    if time.time() - (info.get("refreshed_at") or 0) < every:
        return True
    o = _oidc(h, prim)
    if o is None:
        return True
    try:
        claims = o.refresh(info["refresh_token"])
    except (urllib.error.HTTPError, oidc_mod.SSOError) as e:
        if isinstance(e, urllib.error.HTTPError) and e.code not in (400, 401, 403):
            return True                 # provider error (5xx/429): not a revocation
        # invalid_grant: user removed/disabled at the provider, session revoked, etc.
        prim.audit("sso", "session_revoked", {"user": rec_name, "error": str(e)[:120]})
        prim.end_session(sid)
        return False
    except Exception:                   # network blip / timeout: keep the session, try again at the next refresh
        return True
    rec = prim.user_record(rec_name)
    if rec and not rec["disabled"]:
        _sync_roles(prim, rec, claims)
    prim.update_session_refresh(sid, claims.get("_refresh_token"), claims.get("_raw_id_token"))
    return True


def logout_url(h, prim: Store, info: dict):
    o = _oidc(h, prim)
    if o is None:
        return None
    try:
        scheme = "https" if h._secure_cookie() else "http"
        return o.logout_url(info.get("id_token"), f"{scheme}://{h.headers.get('Host', '')}/")
    except Exception:     # provider down: end only the local session
        return None


def web_get(h, prim: Store, path: str, q: dict):
    if path == "/auth/sso/login":
        o = _oidc(h, prim)
        if o is None:
            h._send(404, tr("SSO is not configured"), "text/plain")
            return True
        url, st = o.start()
        sealed = oidc_mod.seal(h.cookie_key(), {**st, "t": time.time()})
        h._send(302, "", cookies=[f"if_oidc={sealed}; HttpOnly; SameSite=Lax; Path=/auth{h._secure_cookie()}"], location=url)
        return True
    if path == "/auth/sso/callback":
        _sso_callback(h, prim, q)
        return True
    if path == "/api/users":
        who = h._need(prim, "read")
        if auth.role_in(who, None) != "admin":
            h._json(403, {"error": tr("global admin only")})
            return True
        h._json(200, {"users": prim.users_admin(), "projects": list(h._stores(prim)),
                      "sso": {k: prim.get_setting("sso." + k) for k in SSO_KEYS}})
        return True
    if path == "/api/llm":
        from . import jev, llm
        who = h._need(prim, "read")
        st = h._data(prim, who)
        tri = {k.removeprefix("state.jev."): v for k, v in st.settings("state.jev.").items()}
        h._json(200, {"llm": llm.status(st), "jev": jev.status(st), "triage": tri})
        return True
    return None


def _sso_callback(h, prim: Store, q: dict):
    o = _oidc(h, prim)
    sealed = oidc_mod.unseal(h.cookie_key(), cookie(h.headers.get("Cookie", ""), "if_oidc") or "")
    clear = f"if_oidc=; Max-Age=0; HttpOnly; Path=/auth{h._secure_cookie()}"
    if o is None or not sealed or sealed.get("state") != q.get("state") or "code" not in q:
        return h._send(400, tr("SSO: invalid or expired request"), "text/plain", cookies=[clear])
    try:
        claims = o.finish(q["code"], sealed["verifier"], sealed["nonce"])
    except Exception as e:     # any provider/validation failure → deny
        prim.audit("sso", "login_failed", {"error": str(e)[:200]})
        return h._send(401, tr("SSO: validation failed"), "text/plain", cookies=[clear])
    email = (claims.get("email") or "").lower()
    if not email or claims.get("email_verified") is not True:
        return h._send(403, tr("SSO: e-mail missing or not verified"), "text/plain", cookies=[clear])
    gmap = prim.get_setting("sso.group_map", []) or []
    g_role, g_proj = oidc_mod.map_groups(claims, prim.get_setting("sso.group_claim", "groups"), gmap)
    if gmap and prim.get_setting("sso.require_group", False) and g_role is None and not g_proj:
        prim.audit("sso", "login_denied", {"email": email, "reason": "no mapped group"})
        return h._send(403, tr("SSO: your account is not in any authorized group."), "text/plain", cookies=[clear])
    rec = next((u for u in prim.users_admin() if (u.get("email") or "").lower() == email or u["name"].lower() == email), None)
    if rec is None:
        doms = prim.get_setting("sso.allowed_domains", []) or []
        if not prim.get_setting("sso.auto_provision", False) or (doms and email.split("@")[-1] not in doms):
            prim.audit("sso", "login_denied", {"email": email})
            return h._send(403, tr("SSO: your account has no access. Ask an administrator."), "text/plain", cookies=[clear])
        role = g_role or prim.get_setting("sso.default_role", "viewer")
        if role not in ("viewer", "engineer", "admin"):         # a group mapped to `none` (or a bad default): no account is created
            prim.audit("sso", "login_denied", {"email": email, "reason": f"role {role}"})
            return h._send(403, tr("SSO: your account has no access. Ask an administrator."), "text/plain", cookies=[clear])
        prim.add_user(email, role, email=email, random_token=True)
        prim.sso_sync(email, None, None)
        prim.audit("sso", "user_provisioned", {"email": email, "role": role})
        rec = prim.user_record(email)
    rec = prim.user_record(rec["name"])          # full record (includes sso_managed and project_roles as JSON)
    if rec["disabled"]:
        return h._send(403, tr("SSO: account disabled"), "text/plain", cookies=[clear])
    _sync_roles(prim, rec, claims)
    sid = prim.create_session(rec["name"], "sso", id_token=claims.get("_raw_id_token"), sub=claims.get("sub"),
                              sid=claims.get("sid"), refresh_token=claims.get("_refresh_token"))
    prim.audit(rec["name"], "login", {"via": "sso"})
    h._send(302, "", cookies=[clear, h.session_cookie(sid)], location="/")


def web_post_raw(h, prim: Store, parts: list):
    """Back-channel logout: called by the provider, so it has no anti-CSRF header."""
    if parts != ["auth", "sso", "backchannel"]:
        return None
    try:
        form = dict(parse_qsl(read_body(h).decode()))
    except (RequestError, UnicodeDecodeError) as e:
        h.close_connection = True
        h._json(e.status if isinstance(e, RequestError) else 400, {"error": str(e)})
        return True
    o = _oidc(h, prim)
    if o is None or "logout_token" not in form:
        h._json(400, {"error": "invalid_request"})
        return True
    try:
        c = o.verify_logout_token(form["logout_token"])
    except Exception as e:
        prim.audit("sso", "backchannel_rejected", {"error": str(e)[:120]})
        h._json(400, {"error": "invalid_logout_token"})
        return True
    k = prim.end_sessions_by_provider(c.get("sid"), c.get("sub"))
    prim.audit("sso", "backchannel_logout", {"sessions": k})
    h._send(200, "", "text/plain")
    return True


def web_post(h, prim: Store, parts: list, body: dict, query: dict):
    if parts == ["api", "users"] and body.get("action") in ("scope", "project_role"):
        who = h._global_admin(prim)
        name = str(body.get("name", ""))
        if body["action"] == "scope":
            prim.set_scopes(name, str(body.get("project", "")), [x.strip() for x in (body.get("patterns") or []) if str(x).strip()] or None)
            prim.audit(who["name"], "user_scope", {"name": name, "project": body.get("project"), "patterns": body.get("patterns")})
        else:
            prim.set_project_role(name, str(body.get("project", "")), body.get("role") or None)
            prim.audit(who["name"], "user_project_role", {"name": name, "project": body.get("project"), "role": body.get("role")})
        h._json(200, {"ok": True})
        return True
    if parts == ["api", "projects"]:
        who = h._global_admin(prim)
        reg = dict(prim.get_setting("projects", {}) or {})
        name = str(body.get("name", "")).strip()
        if body.get("action") == "add":
            if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name) or name == DEFAULT_PROJECT:
                raise ValueError(tr("invalid project name"))
            path = str(body.get("store", ""))
            if not path:
                raise ValueError(tr("enter the path of the project's store"))
            h._store(path)                 # create/validate; closed when the request ends
            reg[name] = path
        elif body.get("action") == "remove":
            reg.pop(name, None)
        else:
            raise ValueError(tr("unknown action"))
        prim.set_setting("projects", reg)
        prim.audit(who["name"], "project_" + body["action"], {"name": name})
        h._json(200, {"ok": True})
        return True
    if parts == ["api", "sso"]:
        who = h._global_admin(prim)
        for k in SSO_KEYS:
            if k in body:
                v = body[k]
                if k == "allowed_domains" and isinstance(v, str):
                    v = [x.strip().lower() for x in v.split(",") if x.strip()]
                if k == "group_map" and isinstance(v, str):
                    v = parse_group_map(v)
                if v in ("", None, []):
                    prim.unset_setting("sso." + k)
                else:
                    prim.set_setting("sso." + k, v)
        prim.audit(who["name"], "sso_config", {})
        h._json(200, {"ok": True})
        return True
    if parts[:2] in (["api", "llm"], ["api", "jev"]):
        _ai_settings(h, prim, parts, body)
        return True
    if parts[:2] == ["api", "findings"] and len(parts) == 4 and parts[3] in FIX_ACTIONS:
        _fix_action(h, prim, parts[2], parts[3], body)
        return True
    if parts[:2] == ["api", "proposals"] and len(parts) == 4:
        _proposal_action(h, prim, parts[2], parts[3], query)
        return True
    return None


def _ai_settings(h, prim: Store, parts: list, body: dict):
    from . import jev, llm
    sub = parts[2] if len(parts) > 2 else ""
    who = h._need(prim, "settings")
    st = h._data(prim, who)
    if str(body.get("base_url") or "").strip() and parts[1] == "llm":
        # a custom endpoint makes the server send requests (and stored keys) wherever it points: global admins only
        h._global_admin(prim)
    try:
        if parts[1] == "llm" and sub == "":
            for k in ("provider", "model", "base_url"):
                if k in body:
                    v = str(body[k] or "").strip()
                    st.set_setting(f"llm.{k}", v) if v else st.unset_setting(f"llm.{k}")
            if body.get("clear_key"):
                llm.clear_key(st, who["name"])
            if body.get("api_key"):
                llm.set_key(st, str(body["api_key"]), who["name"])
            jv = body.get("jev") or {}
            for k in ("provider", "model"):
                if k in jv:
                    v = str(jv[k] or "").strip()
                    st.set_setting(f"jev.{k}", v) if v else st.unset_setting(f"jev.{k}")
            if "enabled" in jv:
                st.set_setting("jev.enabled", bool(jv["enabled"]))
            if jv.get("clear_key"):
                jev.clear_key(st, who["name"])
            if jv.get("api_key"):
                jev.set_key(st, str(jv["api_key"]), who["name"])
            st.audit(who["name"], "llm_config", {"provider": st.get_setting("llm.provider"), "model": st.get_setting("llm.model"),
                                                 "jev": st.get_setting("jev.provider")})
            return h._json(200, {"llm": llm.status(st), "jev": jev.status(st)})
        if parts[1] == "llm" and sub in ("test", "models"):
            cfg = llm.settings(st)
            for k in ("provider", "model", "base_url"):
                if body.get(k):
                    cfg[k] = str(body[k]).rstrip("/") if k == "base_url" else str(body[k])
            if cfg["provider"] in llm.PROVIDERS and not body.get("base_url") and not st.get_setting("llm.base_url"):
                cfg["base_url"] = llm.PROVIDERS[cfg["provider"]]["base"]
            if not cfg.get("model") and cfg["provider"] in llm.PROVIDERS:
                cfg["model"] = llm.PROVIDERS[cfg["provider"]]["model"]
            key = str(body["api_key"]) if body.get("api_key") else None
            if sub == "models":
                return h._json(200, {"models": llm.list_models(st, cfg, key)})
            r = llm.chat(st, "Reply with the single word: ok", "ping", cfg=cfg, key=key)
            return h._json(200, {"ok": True, "text": r["text"][:80], "seconds": r["seconds"], "model": r["model"]})
        if parts[1] == "jev" and sub == "test":
            cfg = jev.settings(st)
            for k in ("provider", "model"):
                if body.get(k):
                    cfg[k] = str(body[k])
            if cfg["provider"] in jev.PROVIDERS:
                cfg["base_url"] = cfg.get("base_url") or jev.PROVIDERS[cfg["provider"]]["base"]
                cfg["model"] = cfg.get("model") or jev.PROVIDERS[cfg["provider"]]["model"]
            r = jev.decide(st, {"text": "Column removed from the base model; 3 downstream models read this column."},
                           {"breaks_consumers": jev.noul("Does this change break consumers?")}, cfg=cfg,
                           key=str(body["api_key"]) if body.get("api_key") else None)
            return h._json(200, {"ok": True, "p": r["answers"]["breaks_consumers"]["p"], "seconds": r["seconds"], "model": r["model"]})
    except (llm.LLMError, jev.JevError) as e:
        return h._json(400, {"error": str(e)})
    return h._json(404, {"error": "not found"})


FIX_ACTIONS = ("fix", "fix-ai", "ai-status", "owner", "unique-key", "triage")


def _changes_in_scope(who, project: str, role, p: dict) -> bool:
    """Write scope over every file a fix changes (a rename also edits downstream models, not just the finding's file)."""
    pats = (who.get("scopes") or {}).get(project)
    if role == "admin" or not pats:
        return True
    import fnmatch
    root = Path(p.get("project_root") or ".").resolve()
    for c in p.get("changes") or []:
        f = Path(c["file"]).resolve()
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        if not any(fnmatch.fnmatch(x, pat) for x in (rel, f.name, f.stem) for pat in pats):
            return False
    return True


def _project_or_fail(st: Store):
    from intentflow import dbt_loader
    target = st.get_setting("dbt.target")
    if not target:
        raise ValueError(tr("configure dbt.target before creating fixes"))
    return dbt_loader.load(target, st.get_setting("dbt.root") or Path(target).parent)


def _fix_action(h, prim: Store, fid: str, act: str, body: dict):
    from . import proposals
    who = h._need(prim, "fix_create")
    st = h._data(prim, who)
    actor = who["name"]
    frec = st.finding(fid)
    if not scope_ok(who, h._project(), frec, auth.role_in(who, h._project())):
        return h._json(403, {"error": tr("this finding is outside your scope (allowed folders/models)")})
    job_key = (st.path, fid)
    if act == "ai-status":
        with _AI_JOBS_LOCK:
            job_status = dict(_AI_JOBS.get(job_key) or {"state": "none"})
        return h._json(200, job_status)
    if act in ("fix", "owner", "unique-key"):
        proj = _project_or_fail(st)
        if not frec:
            raise ValueError(tr("finding not found"))
        role = auth.role_in(who, h._project())

        def guard(p):
            if not _changes_in_scope(who, h._project(), role, p):
                raise PermissionError(tr("this fix changes files outside your scope (allowed folders/models)"))
        try:
            if act == "fix":
                pr = proposals.create(st, proj, frec, actor, guard=guard)
            elif act == "owner":
                pr = proposals.create_owner(st, proj, frec, str(body.get("owner", "")), actor, guard=guard)
            else:
                pr = proposals.create_unique_key(st, proj, frec, str(body.get("key", "")), actor, guard=guard)
        except PermissionError as e:
            return h._json(403, {"error": str(e)})
        return h._json(200, {"proposal": pr["id"]})
    if act == "fix-ai":
        if not frec:
            raise ValueError(tr("finding not found"))
        spath, force = st.path, bool(body.get("force"))
        proj_name, role = h._project(), auth.role_in(who, h._project())

        def guard(p):
            if not _changes_in_scope(who, proj_name, role, p):
                raise PermissionError(tr("this fix changes files outside your scope (allowed folders/models)"))
        with _AI_JOBS_LOCK:
            for k, v in list(_AI_JOBS.items()):
                if v.get("finished", float("inf")) < time.time() - 3600:
                    del _AI_JOBS[k]
            job = _AI_JOBS.get(job_key)
            if job and job.get("state") == "running":
                return h._json(200, job)
            if sum(v.get("state") == "running" for v in _AI_JOBS.values()) >= 4:
                return h._json(429, {"error": tr("limit of concurrent suggestions reached")})
            if len(_AI_JOBS) >= 128:
                done = [k for k, v in _AI_JOBS.items() if v.get("state") != "running"]
                if done:
                    del _AI_JOBS[min(done, key=lambda k: _AI_JOBS[k].get("finished", 0))]
            _AI_JOBS[job_key] = {"state": "running", "started": time.time()}
        from intentflow import i18n
        code = i18n.lang()

        def ai_job():
            from . import llm_fix
            i18n.set_request_lang(code)
            s2 = None
            try:
                s2 = Store(spath)          # inside the try: a failure here must still end the job (it counts toward the cap)
                proj = _project_or_fail(s2)
                f2 = s2.finding(fid)
                if not f2:
                    raise ValueError(tr("finding not found"))
                result = llm_fix.suggest(s2, proj, f2, actor, force_llm=force, guard=guard)
                with _AI_JOBS_LOCK:
                    _AI_JOBS[job_key] = {"state": "done", "result": result, "finished": time.time()}
            except Exception as e:      # the UI shows the error until someone dismisses it
                with _AI_JOBS_LOCK:
                    _AI_JOBS[job_key] = {"state": "error", "error": f"{type(e).__name__}: {e}"[:600], "finished": time.time()}
            finally:
                if s2 is not None:
                    s2.close()
        threading.Thread(target=ai_job, daemon=True).start()
        with _AI_JOBS_LOCK:
            job_status = dict(_AI_JOBS[job_key])
        return h._json(200, job_status)
    # triage
    from . import jev, llm
    if not frec:
        raise ValueError(tr("finding not found"))
    target = st.get_setting("dbt.target")
    from intentflow import dbt_loader
    proj = dbt_loader.load(target, st.get_setting("dbt.root") or Path(target).parent) if target else None
    try:
        t = jev.triage(st, frec, proj)
        st.set_setting(f"state.jev.{fid}", t)
        return h._json(200, {"triage": t})
    except (llm.LLMError, jev.JevError) as e:
        return h._json(400, {"error": str(e)})


def _proposal_action(h, prim: Store, pid: str, act: str, query: dict):
    from . import proposals
    need = {"approve": "fix_approve", "reject": "fix_reject", "validate": "fix_validate", "apply": "fix_apply", "rollback": "fix_rollback", "recover": "fix_apply"}.get(act)
    if need is None:
        return h._json(404, {"error": "not found"})
    who = h._need(prim, need)
    st = h._data(prim, who)
    actor = who["name"]
    pr_ = st.proposal(pid)
    pf = st.finding((pr_ or {}).get("finding_id", "")) if pr_ else None
    role = auth.role_in(who, h._project())
    if pr_ and not (scope_ok(who, h._project(), pf, role) and _changes_in_scope(who, h._project(), role, pr_)):
        return h._json(403, {"error": tr("this fix is outside your scope (allowed folders/models)")})
    fn = {"approve": lambda: proposals.approve(st, pid, actor, enforce_separation=len(prim.users()) > 1),
          "reject": lambda: proposals.reject(st, pid, actor),
          "validate": lambda: proposals.validate_and_store(st, pid, query.get("mode", "parse"), actor),
          "apply": lambda: proposals.apply(st, pid, actor), "rollback": lambda: proposals.rollback(st, pid, actor),
          "recover": lambda: proposals.recover(st, pid, actor)}[act]
    if act == "validate" and os.environ.get("INTENTFLOW_VALIDATION") == "remote":
        # this process has no Docker (slim container): queue it for `intentflow validate-worker` on a host that does
        proposals.request_validation(st, pid, query.get("mode", "parse"), actor)
        return h._json(202, {"status": st.proposal(pid)["status"], "queued": True})
    r = fn()
    return h._json(200, {"status": st.proposal(pid)["status"], "validation": r.get("validation")})
