"""Jev (TypeSafe): a DECISION model that returns a choice/score/yes-no with a calibrated probability, not text.

Here it does what an LLM does badly and expensively: triage of findings (real? false positive? urgency? which fix path?) and the risk gate
before applying an LLM-generated fix. Python code + a person still decide: Jev only scores.

Access (per Jev's public docs; formats may change during early access):
  * `openrouter`: `POST https://openrouter.ai/api/alpha/decisions` with `OPENROUTER_API_KEY` (model `typesafe/jev-1.13`);
  * `typesafe`  : the official `typesafe-sdk` (`pip install typesafe-sdk`), `TYPESAFE_API_KEY`.
Verified only against a simulated server with that format; never against the real service.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from intentflow.i18n import N_, tr

PROVIDERS = {
    "openrouter": {"label": "OpenRouter (typesafe/jev)", "env": "OPENROUTER_API_KEY", "base": "https://openrouter.ai", "model": "typesafe/jev-1.13"},
    "typesafe": {"label": "TypeSafe direct (SDK)", "env": "TYPESAFE_API_KEY", "base": "https://api.typesafe.ai", "model": "jev-latest"},
}
KEY_SETTING = "secret.jev_api_key"


class JevError(RuntimeError):
    pass


def settings(store) -> dict:
    prov = store.get_setting("jev.provider") or ""
    meta = PROVIDERS.get(prov, {})
    return {"provider": prov, "model": store.get_setting("jev.model") or meta.get("model", ""),
            "base_url": (store.get_setting("jev.base_url") or meta.get("base", "")).rstrip("/"), "timeout": float(store.get_setting("jev.timeout") or 30)}


def api_key(store, provider: str | None = None, base_url: str | None = None) -> tuple[str | None, str]:
    """(key, source). Environment keys only go to the provider's default endpoint (see `llm.api_key`)."""
    prov = provider or store.get_setting("jev.provider") or ""
    if base_url is None:
        base_url = store.get_setting("jev.base_url")
    env = PROVIDERS.get(prov, {}).get("env")
    default = not base_url or base_url.rstrip("/") == PROVIDERS.get(prov, {}).get("base", "").rstrip("/")
    if env and default and os.environ.get(env):
        return os.environ[env], "env"
    stored = store.get_setting(KEY_SETTING)
    if stored:
        return store.box.dec(stored), "stored"
    return None, "none"


def set_key(store, key: str, actor: str = "system") -> None:
    if not store.box.enabled:
        raise JevError(tr("no encryption key: set INTENTFLOW_SECRET_KEY before storing the key, or use the provider's environment variable"))
    store.set_setting(KEY_SETTING, store.box.enc(key.strip()))
    store.audit(actor, "jev_key_set", {})


def clear_key(store, actor: str = "system") -> None:
    store.unset_setting(KEY_SETTING)
    store.audit(actor, "jev_key_cleared", {})


def status(store) -> dict:
    s = settings(store)
    key, src = api_key(store)
    return {"provider": s["provider"], "model": s["model"], "key_source": src, "configured": bool(s["provider"] and key),
            "enabled": bool(store.get_setting("jev.enabled")), "can_store_key": store.box.enabled,
            "providers": {k: {"label": v["label"], "default_model": v["model"], "env": v["env"]} for k, v in PROVIDERS.items()}}


# ---- questions
def noul(instructions: str, yes: str = "Yes.", no: str = "No.") -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def choice(instructions: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": levels}


def _normalize(answers: dict) -> dict:
    out = {}
    for name, a in (answers or {}).items():
        t = a.get("type")
        if t == "noul":
            out[name] = {"type": "noul", "p": float(a.get("noul", a.get("probability", 0.0)))}
        elif t == "choice":
            out[name] = {"type": "choice", "choice": a.get("choice"), "confidence": a.get("confidence"), "probabilities": a.get("probabilities", {})}
        elif t == "score":
            out[name] = {"type": "score", "score": a.get("score"), "confidence": a.get("confidence"), "probabilities": a.get("probabilities", {}),
                         "legend": a.get("legend", {})}
    return out


def decide(store, state: dict, questions: dict[str, dict], *, cfg: dict | None = None, key: str | None = None, post=None) -> dict:
    s = cfg or settings(store)
    if s["provider"] not in PROVIDERS:
        raise JevError(tr("Jev not configured (Settings → Decisions with Jev)"))
    if key is None:
        key, _ = api_key(store, s["provider"], s["base_url"])
    if not key:
        raise JevError(tr("missing key: set {env} or store the key in Settings", env=PROVIDERS[s["provider"]]["env"]))
    t0 = time.time()
    if s["provider"] == "openrouter":
        body = {"model": s["model"], "state": state, "questions": questions}
        req = urllib.request.Request(f"{s['base_url']}/api/alpha/decisions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=s["timeout"]) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise JevError(tr("HTTP {code} from Jev ({url})", code=e.code, url=s["base_url"])) from None
        except urllib.error.URLError as e:
            raise JevError(tr("no connection to Jev: {reason}", reason=e.reason)) from None
        res = {"answers": _normalize(d.get("answers")), "model": d.get("model", s["model"]), "usage": d.get("usage", {})}
    else:
        try:
            from typesafe_sdk import Choice, Noul, Score, TypeSafeClient  # type: ignore
        except ImportError:
            raise JevError(tr("install the SDK: pip install typesafe-sdk (or use the openrouter provider)")) from None
        qs = {}
        for n, q in questions.items():
            if q["type"] == "noul":
                qs[n] = Noul(instructions=q["instructions"], criteria=q.get("criteria"))
            elif q["type"] == "choice":
                qs[n] = Choice(instructions=q["instructions"], criteria=q["criteria"])
            else:
                qs[n] = Score(instructions=q["instructions"], criteria=q["criteria"])
        try:
            with TypeSafeClient(api_key=key, base_url=s["base_url"], model=s["model"]) as c:
                r = c.system_one(state=state, questions=qs)
        except Exception as e:
            raise JevError(tr("Jev SDK failure: {error}", error=f"{type(e).__name__}: {str(e)[:150]}")) from None
        ans = {}
        for n, v in getattr(r, "nouls", {}).items():
            ans[n] = {"type": "noul", "p": float(v.noul)}
        for n, v in getattr(r, "choices", {}).items():
            ans[n] = {"type": "choice", "choice": v.choice, "confidence": getattr(v, "confidence", None), "probabilities": dict(getattr(v, "probabilities", {}) or {})}
        for n, v in getattr(r, "scores", {}).items():
            ans[n] = {"type": "score", "score": v.score, "confidence": getattr(v, "confidence", None), "probabilities": dict(getattr(v, "probabilities", {}) or {}),
                      "legend": dict(getattr(v, "legend", {}) or {})}
        res = {"answers": ans, "model": getattr(r, "model", s["model"]), "usage": {}}
    res["seconds"] = round(time.time() - t0, 2)
    return res


# ---- finding triage
FIX_PATHS = {
    "deterministic_rename": "Rename the column in the affected models (mechanical substitution).",
    "expand_star": "Replace SELECT * with the column list.",
    "llm_edit": "Requires editing the SQL with reasoning (rewriting logic, type, join, filter).",
    "metadata_or_test": "Missing metadata or test (owner, description, unique/not_null, contract).",
    "investigate_data": "The cause is in the data/upstream; it is not a code edit.",
    "no_action": "Nothing to do (noise, expected or already fixed).",
}
URGENCY = [N_("can wait"), N_("next sprint"), N_("this week"), N_("today")]


def triage_state(finding: dict, project=None) -> dict:
    ev = {k: v for k, v in (finding.get("evidence") or {}).items() if k not in ("sample", "samples", "rows", "examples")}
    st = {"check": finding.get("check_id"), "severity": finding.get("severity"), "title": finding.get("title"),
          "detail": (finding.get("detail") or "")[:600], "evidence": json.loads(json.dumps(ev, default=str))[:20] if isinstance(ev, list) else
          {k: (v if len(json.dumps(v, default=str)) < 300 else "…") for k, v in list(ev.items())[:12]},
          "has_deterministic_fix": bool((finding.get("fix") or {}).get("kind"))}
    if project is not None:
        for uid in (finding.get("nodes") or [])[:1]:
            n = project.nodes.get(uid)
            if n:
                st["model"] = {"name": n.name, "type": n.resource_type, "materialized": n.materialized, "description": (n.description or "")[:200],
                               "downstream_models": len(project.downstream(uid)), "tests": len(n.tests), "owner": n.meta.get("owner")}
    return st


def triage(store, finding: dict, project=None, **kw) -> dict:
    qs = {
        "real_issue": noul("Does this finding describe a real problem the data team should fix?",
                           "Real and actionable problem.", "Noise, expected or irrelevant for this model."),
        "likely_false_positive": noul("Is this finding probably a false positive of the automatic check?"),
        "urgency": score("How urgent is it to act on this finding?", URGENCY),
        "fix_path": choice("Which path resolves this finding?", FIX_PATHS),
    }
    r = decide(store, triage_state(finding, project), qs, **kw)
    a = r["answers"]
    out = {"real_issue": a.get("real_issue", {}).get("p"), "likely_false_positive": a.get("likely_false_positive", {}).get("p"),
           "urgency": a.get("urgency", {}).get("score"), "urgency_label": None,
           "fix_path": a.get("fix_path", {}).get("choice"), "fix_path_confidence": a.get("fix_path", {}).get("confidence"),
           "fix_path_probabilities": a.get("fix_path", {}).get("probabilities", {}), "model": r["model"], "ts": time.time(), "seconds": r["seconds"]}
    if out["urgency"] is not None:
        out["urgency_label"] = URGENCY[max(0, min(len(URGENCY) - 1, round(float(out["urgency"]))))]
    return out


def needs_generative_model(t: dict, threshold: float = 0.5) -> bool:
    """REFLEX: only call the LLM when the fast decision says the fix requires a SQL edit."""
    return t.get("fix_path") == "llm_edit" or (t.get("fix_path_probabilities", {}).get("llm_edit", 0) >= threshold)


def risk_gate(store, summary: str, diff: str, kind: str, **kw) -> dict:
    """Score the risk of a proposed change. Code decides what to do with the number (see `policy_for_risk`)."""
    qs = {
        "changes_semantics": noul("Does the change alter the meaning/result of the data (not just form or name)?"),
        "risk": score("What is the risk of this change in production?", ["low", "medium", "high"]),
        "touches_unrelated_logic": noul("Does the change touch logic unrelated to the described problem?"),
    }
    r = decide(store, {"summary": summary[:500], "kind": kind, "diff": diff[:6000]}, qs, **kw)
    a = r["answers"]
    return {"changes_semantics": a.get("changes_semantics", {}).get("p"), "risk": a.get("risk", {}).get("score"),
            "touches_unrelated_logic": a.get("touches_unrelated_logic", {}).get("p"), "model": r["model"], "ts": time.time()}


def policy_for_risk(g: dict) -> dict:
    """Deterministic rules over the score: stronger validation and a warning; never applies on its own."""
    risk = g.get("risk")
    high = (risk is not None and float(risk) >= 1.2) or (g.get("changes_semantics") or 0) >= 0.7 or (g.get("touches_unrelated_logic") or 0) >= 0.5
    return {"validate_mode": "build" if high else "parse", "needs_extra_review": bool(high),
            "reason": tr("high risk, changes semantics or touches unrelated logic") if high else tr("low-risk change according to Jev")}
