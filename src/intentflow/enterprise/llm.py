"""LLM clients (Anthropic, OpenAI, Gemini and local OpenAI-compatible models) over HTTP, without SDKs.

Key: the provider's environment variable (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, LLM_API_KEY) **or** stored encrypted in the store
(`secrets_box`, requires INTENTFLOW_SECRET_KEY/key file). The key is never returned by the API nor written to logs/audit.
Local models (Ollama, LM Studio, vLLM, llama.cpp, Docker Model Runner) speak the OpenAI protocol at `<base_url>/chat/completions`.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from intentflow.i18n import tr

# No default model: the person picks one (the "List models" button / `intentflow llm models` asks the provider),
# so a retired or unexpectedly priced model is never chosen on their behalf.
PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "env": "ANTHROPIC_API_KEY", "base": "https://api.anthropic.com", "model": "", "key": True},
    "openai": {"label": "OpenAI", "env": "OPENAI_API_KEY", "base": "https://api.openai.com/v1", "model": "", "key": True},
    "gemini": {"label": "Google Gemini", "env": "GEMINI_API_KEY", "base": "https://generativelanguage.googleapis.com/v1beta",
               "model": "", "key": True},
    "local": {"label": "Local model (OpenAI-compatible)", "env": "LLM_API_KEY", "base": "http://127.0.0.1:11434/v1", "model": "", "key": False},
}
KEY_SETTING = "secret.llm_api_key"


class LLMError(RuntimeError):
    pass


def settings(store) -> dict:
    prov = store.get_setting("llm.provider") or ""
    meta = PROVIDERS.get(prov, {})
    return {"provider": prov, "model": store.get_setting("llm.model") or meta.get("model", ""),
            "base_url": (store.get_setting("llm.base_url") or meta.get("base", "")).rstrip("/"),
            "max_tokens": int(store.get_setting("llm.max_tokens") or 2000), "timeout": float(store.get_setting("llm.timeout") or (600 if prov == "local" else 120))}


def default_base(provider: str, base_url: str | None) -> bool:
    """True when `base_url` is the provider's own endpoint (or not given)."""
    meta = PROVIDERS.get(provider, {})
    return not base_url or base_url.rstrip("/") == meta.get("base", "").rstrip("/")


def api_key(store, provider: str | None = None, base_url: str | None = None) -> tuple[str | None, str]:
    """(key, source): env | stored | none.

    Server-wide environment keys are only sent to the provider's default endpoint: whoever can change `base_url` in a project
    must not be able to point the server's key at a host they control. A custom endpoint only gets the key stored in that store."""
    prov = provider or store.get_setting("llm.provider") or ""
    if base_url is None:
        base_url = store.get_setting("llm.base_url")
    if default_base(prov, base_url):
        for name in (PROVIDERS.get(prov, {}).get("env"), "LLM_API_KEY"):
            if name and os.environ.get(name):
                return os.environ[name], "env"
    stored = store.get_setting(KEY_SETTING)
    if stored:
        return store.box.dec(stored), "stored"
    return None, "none"


def set_key(store, key: str, actor: str = "system") -> None:
    if not store.box.enabled:
        raise LLMError(tr("no encryption key: set INTENTFLOW_SECRET_KEY (or use a SQLite file) before storing the key here; "
                             "alternatively export the provider key as an environment variable"))
    store.set_setting(KEY_SETTING, store.box.enc(key.strip()))
    store.audit(actor, "llm_key_set", {})            # never the value


def clear_key(store, actor: str = "system") -> None:
    store.unset_setting(KEY_SETTING)
    store.audit(actor, "llm_key_cleared", {})


def status(store) -> dict:
    s = settings(store)
    key, src = api_key(store)
    meta = PROVIDERS.get(s["provider"], {})
    return {"provider": s["provider"], "model": s["model"], "base_url": s["base_url"], "key_source": src,
            "key_required": bool(meta.get("key", True)), "configured": bool(s["provider"] and s["model"] and (key or not meta.get("key", True))),
            "can_store_key": store.box.enabled,
            "providers": {k: {"label": v["label"], "default_model": v["model"], "default_base_url": v["base"], "needs_key": v["key"], "env": v["env"]}
                          for k, v in PROVIDERS.items()}}


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", {})
            detail = detail.get("message", "") if isinstance(detail, dict) else str(detail)
        except Exception:
            pass
        raise LLMError(tr("HTTP {code} from {url}: {detail}", code=e.code, url=url.split("?")[0], detail=detail[:200])) from None
    except urllib.error.URLError as e:
        raise LLMError(tr("no connection to {url}: {reason}", url=url.split("?")[0], reason=e.reason)) from None
    except TimeoutError:
        raise LLMError(tr("timed out ({s}s) at {url}", s=f"{timeout:g}", url=url.split("?")[0])) from None


def chat(store, system: str, user: str, *, json_mode: bool = False, cfg: dict | None = None, key: str | None = None) -> dict:
    """One call. Returns {text, model, usage:{input,output}, seconds}."""
    s = cfg or settings(store)
    prov = s["provider"]
    if prov not in PROVIDERS:
        raise LLMError(tr("AI provider not configured (Settings → AI to suggest fixes)"))
    if not s["model"]:
        raise LLMError(tr("set the model"))      # e.g. `intentflow llm models`, then `intentflow llm set --model ...`
    if key is None:
        key, _ = api_key(store, prov, s["base_url"])
    if PROVIDERS[prov]["key"] and not key:
        raise LLMError(tr("missing API key: set {env} or store the key in Settings", env=PROVIDERS[prov]["env"]))
    t0 = time.time()
    base, to = s["base_url"], s["timeout"]
    if prov == "anthropic":
        r = _post(f"{base}/v1/messages", {"model": s["model"], "max_tokens": s["max_tokens"], "system": system,
                                          "messages": [{"role": "user", "content": user}]},
                  {"x-api-key": key, "anthropic-version": "2023-06-01"}, to)
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        usage = {"input": (r.get("usage") or {}).get("input_tokens"), "output": (r.get("usage") or {}).get("output_tokens")}
    elif prov == "gemini":
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": s["max_tokens"], **({"responseMimeType": "application/json"} if json_mode else {})}}
        r = _post(f"{base}/models/{s['model']}:generateContent", body, {"x-goog-api-key": key}, to)
        cands = r.get("candidates") or []
        text = "".join(p.get("text", "") for p in ((cands[0].get("content") or {}).get("parts") or [])) if cands else ""
        um = r.get("usageMetadata") or {}
        usage = {"input": um.get("promptTokenCount"), "output": um.get("candidatesTokenCount")}
    else:                                              # openai and local
        body = {"model": s["model"], "max_tokens": s["max_tokens"],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode and prov == "openai":
            body["response_format"] = {"type": "json_object"}
        r = _post(f"{base}/chat/completions", body, {"Authorization": f"Bearer {key}"} if key else {}, to)
        ch = (r.get("choices") or [{}])[0]
        text = ((ch.get("message") or {}).get("content")) or ""
        u = r.get("usage") or {}
        usage = {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens")}
    if not text.strip():
        raise LLMError(tr("the model returned an empty response"))
    out = {"text": text, "model": s["model"], "usage": usage, "seconds": round(time.time() - t0, 2)}
    store.audit("llm", "llm_call", {"provider": prov, "model": s["model"], **usage, "seconds": out["seconds"]})   # usage, never content
    return out


def list_models(store, cfg: dict | None = None, key: str | None = None) -> list[str]:
    """Models available on the server (useful for local ones). Anthropic/OpenAI/Gemini answer too."""
    s = cfg or settings(store)
    prov = s["provider"]
    if key is None:
        key, _ = api_key(store, prov, s["base_url"])
    base = s["base_url"]
    from intentflow.orchestrators import http_json
    try:
        if prov == "anthropic":
            d = http_json(f"{base}/v1/models", {"x-api-key": key or "", "anthropic-version": "2023-06-01"})
            return sorted(m["id"] for m in d.get("data", []))
        if prov == "gemini":
            d = http_json(f"{base}/models", {"x-goog-api-key": key or ""})
            return sorted(m["name"].split("/")[-1] for m in d.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", []))
        d = http_json(f"{base}/models", {"Authorization": f"Bearer {key}"} if key else {})
        return sorted(m["id"] for m in d.get("data", []))
    except RuntimeError as e:
        raise LLMError(str(e)) from None
