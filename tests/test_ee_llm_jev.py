"""LLM (Anthropic/OpenAI/Gemini/local) and Jev: each API's format against a local server; model edits are never applied without validation."""
import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import new_db
from intentflow.enterprise import jev, llm, llm_fix, proposals
from intentflow.store import Store


class Fake:
    """Servidor que registra o pedido e responde conforme o caminho."""
    def __init__(self, routes):
        self.log, routes_ = [], routes

        class H(BaseHTTPRequestHandler):
            def _go(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                outer.log.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "json": body})
                r = routes_.get(self.path.split("?")[0])
                r = r(body) if callable(r) else r
                if r is None:
                    self.send_response(404); self.end_headers(); self.wfile.write(b'{"error":{"message":"nao existe"}}'); return
                code, payload = (r if isinstance(r, tuple) else (200, r))
                self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(payload).encode())
            do_GET = do_POST = _go

            def log_message(self, *a):
                pass
        outer = self
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def store(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "INTENTFLOW_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    return Store(new_db(tmp_path, "l.db", "sqlite"))


def _cfg(st, provider, base, model="m"):
    st.set_setting("llm.provider", provider); st.set_setting("llm.base_url", base); st.set_setting("llm.model", model)


def test_anthropic_request_and_response_shape(store, monkeypatch):
    f = Fake({"/v1/messages": {"content": [{"type": "text", "text": '{"ok":1}'}], "usage": {"input_tokens": 11, "output_tokens": 3}}})
    store.set_setting("secret.llm_api_key", "sk-ant-x")      # a custom endpoint only gets the store key
    _cfg(store, "anthropic", f.url, "claude-sonnet-5-5")
    r = llm.chat(store, "sistema", "usuario")
    f.close()
    h, b = f.log[0]["headers"], f.log[0]["json"]
    assert h["x-api-key"] == "sk-ant-x" and h["anthropic-version"] == "2023-06-01"
    assert b["system"] == "sistema" and b["messages"] == [{"role": "user", "content": "usuario"}] and b["model"] == "claude-sonnet-5-5"
    assert r["text"] == '{"ok":1}' and r["usage"] == {"input": 11, "output": 3}
    ev = [e for e in store.audit_log() if e["event"] == "llm_call"][-1]
    assert '"input": 11' in ev["detail"] and "usuario" not in ev["detail"] and "sk-ant" not in ev["detail"]


def test_openai_and_local_use_chat_completions(store, monkeypatch):
    f = Fake({"/chat/completions": {"choices": [{"message": {"content": "olá"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, "/models": {"data": [{"id": "b"}, {"id": "a"}]}})
    store.set_setting("secret.llm_api_key", "sk-o")      # a custom endpoint only gets the store key
    _cfg(store, "openai", f.url, "gpt-4o-mini")
    r = llm.chat(store, "s", "u", json_mode=True)
    assert f.log[0]["headers"]["authorization"] == "Bearer sk-o" and f.log[0]["json"]["response_format"] == {"type": "json_object"} and r["text"] == "olá"
    store.unset_setting("secret.llm_api_key")
    _cfg(store, "local", f.url, "qwen")
    llm.chat(store, "s", "u", json_mode=True)                       # no key and no response_format (not every local server accepts it)
    assert "authorization" not in f.log[1]["headers"] and "response_format" not in f.log[1]["json"]
    assert llm.list_models(store) == ["a", "b"]
    f.close()


def test_gemini_request_shape(store, monkeypatch):
    f = Fake({"/models/gemini-2.0-flash:generateContent": {"candidates": [{"content": {"parts": [{"text": "x"}, {"text": "y"}]}}],
                                                           "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}}})
    store.set_setting("secret.llm_api_key", "g-key")      # a custom endpoint only gets the store key
    _cfg(store, "gemini", f.url, "gemini-2.0-flash")
    r = llm.chat(store, "s", "u", json_mode=True)
    f.close()
    assert f.log[0]["headers"]["x-goog-api-key"] == "g-key" and f.log[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert f.log[0]["json"]["systemInstruction"]["parts"][0]["text"] == "s" and r["text"] == "xy" and r["usage"]["input"] == 7


def test_missing_key_and_http_errors_are_clear(store):
    _cfg(store, "anthropic", "http://127.0.0.1:9", "m")
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.chat(store, "s", "u")
    f = Fake({"/v1/messages": (401, {"error": {"message": "invalid x-api-key"}})})
    _cfg(store, "anthropic", f.url, "m")
    with pytest.raises(llm.LLMError, match="401.*invalid x-api-key"):
        llm.chat(store, "s", "u", key="ruim")
    f.close()


def test_key_is_stored_encrypted_never_returned_and_needs_a_cipher_key(store, tmp_path, monkeypatch):
    llm.set_key(store, "sk-segredo-123")                            # SQLite file: the key file exists
    raw = json.loads(store.con.execute("select value from settings where key=?", (llm.KEY_SETTING,)).fetchone()[0])
    assert raw.startswith("enc:v1:") and "sk-segredo" not in raw
    store.set_setting("llm.provider", "openai")
    assert llm.api_key(store) == ("sk-segredo-123", "stored")
    assert "sk-segredo" not in json.dumps(llm.status(store)) and llm.status(store)["key_source"] == "stored"
    assert all("sk-segredo" not in json.dumps(a) for a in store.audit_log())
    mem = Store(":memory:")
    with pytest.raises(llm.LLMError, match="INTENTFLOW_SECRET_KEY"):
        llm.set_key(mem, "k")


# ---------------------------------------------------------------- Jev
JEV = lambda body: {"id": "x", "model": "typesafe/jev-1.13", "answers": {
    "real_issue": {"type": "noul", "noul": 0.9}, "likely_false_positive": {"type": "noul", "noul": 0.1},
    "urgency": {"type": "score", "score": 2.4, "confidence": 0.8, "probabilities": {"2": 0.6}, "legend": {"0": "pode esperar"}},
    "fix_path": {"type": "choice", "choice": "llm_edit", "confidence": 0.77, "probabilities": {"llm_edit": 0.77, "no_action": 0.1}}},
    "usage": {"input_tokens": 100, "output_tokens": 0, "cost": 0.000004}}


def test_jev_openrouter_request_shape_and_triage(store, monkeypatch):
    f = Fake({"/api/alpha/decisions": JEV})
    store.set_setting("secret.jev_api_key", "or-key")
    store.set_setting("jev.provider", "openrouter"); store.set_setting("jev.base_url", f.url)
    finding = {"id": "SQL001-a", "check_id": "SQL001", "severity": "medium", "title": "m: SELECT *", "detail": "d", "evidence": {"sample": [1, 2], "file": "a.sql"}, "fix": None}
    t = jev.triage(store, finding)
    f.close()
    req = f.log[0]
    assert req["headers"]["authorization"] == "Bearer or-key" and req["json"]["model"] == "typesafe/jev-1.13"
    assert set(req["json"]["questions"]) == {"real_issue", "likely_false_positive", "urgency", "fix_path"}
    assert req["json"]["questions"]["fix_path"]["type"] == "choice" and req["json"]["questions"]["urgency"]["criteria"] == jev.URGENCY
    assert "sample" not in json.dumps(req["json"]["state"])           # data samples never leave
    assert t["real_issue"] == 0.9 and t["fix_path"] == "llm_edit" and t["urgency_label"] == "this week" and jev.needs_generative_model(t)


def test_risk_policy_is_deterministic_python():
    assert jev.policy_for_risk({"risk": 0.2, "changes_semantics": 0.1, "touches_unrelated_logic": 0.0}) == \
        {"validate_mode": "parse", "needs_extra_review": False, "reason": "low-risk change according to Jev"}
    assert jev.policy_for_risk({"risk": 0.5, "changes_semantics": 0.9, "touches_unrelated_logic": 0.0})["validate_mode"] == "build"
    assert jev.policy_for_risk({"risk": 1.6})["needs_extra_review"]


def test_jev_without_key_or_provider_fails_clearly(store):
    with pytest.raises(jev.JevError, match="not configured"):
        jev.decide(store, {}, {})
    store.set_setting("jev.provider", "openrouter")
    with pytest.raises(jev.JevError, match="OPENROUTER_API_KEY"):
        jev.decide(store, {}, {})


# ---------------------------------------------------------------- fix generation
@pytest.fixture
def shop(shop_target, tmp_path):
    from intentflow import dbt_loader
    root = Path(shop_target)
    work = tmp_path / "proj"
    import shutil
    shutil.copytree(root, work, ignore=shutil.ignore_patterns("logs"))
    return dbt_loader.load(work / "target", work), work


def _finding(proj):
    n = next(x for x in proj.nodes.values() if x.name == "stg_orders")
    uid = n.unique_id
    return {"id": "SQL001-test", "check_id": "SQL001", "severity": "medium", "title": "stg_orders: sem filtro", "detail": "faltou algo", "nodes": [uid], "evidence": {}, "fix": None}


def test_valid_edit_becomes_a_pending_proposal_with_diff(store, shop):
    proj, work = shop
    f = _finding(proj)
    src = next(x for x in proj.nodes.values() if x.name == "stg_orders").path
    text = (work / src).read_text()
    assert text.count("order_id") >= 1
    edit = {"explanation": "Troca simples.", "confidence": 0.8, "edits": [{"file": src, "find": text.splitlines()[0], "replace": text.splitlines()[0] + " -- revisado"}]}
    r = llm_fix.suggest(store, proj, f, "ana", chat=lambda s, u: {"text": "```json\n" + json.dumps(edit) + "\n```", "model": "fake", "usage": {}, "seconds": 0.1})
    p = store.proposal(r["proposal"])
    assert r["source"] == "llm" and p["status"] == "pending" and p["kind"] == "llm_edit" and p["origin"] == "llm" and p["created_by"] == "ana"
    assert "-- revisado" in p["changes"][0]["after"] and "+" in p["changes"][0]["diff"] and (work / src).read_text() == text      # arquivo intacto
    assert p["llm"]["model"] == "fake"
    with pytest.raises(ValueError, match="approved by a human"):      # no apply without approval+validation
        proposals.apply(store, p["id"])


@pytest.mark.parametrize("bad,msg", [
    ({"edits": [{"file": "outro/arquivo.sql", "find": "x", "replace": "y"}]}, "outside the finding"),
    ({"edits": [{"file": "SRC", "find": "texto que nao existe no arquivo", "replace": "y"}]}, "0 time"),
    ({"edits": [{"file": "SRC", "find": "", "replace": "y"}]}, "empty"),
    ({"edits": []}, "no edit"),
    ({"edits": [{"file": "SRC", "find": "order", "replace": "ORDER"}]}, "time"),      # `order` shows up on several lines → ambiguous
    ({"edits": "isto não é uma lista"}, "no edit"),
])
def test_unsafe_or_ambiguous_edits_are_rejected(store, shop, bad, msg):
    proj, work = shop
    src = next(x for x in proj.nodes.values() if x.name == "stg_orders").path
    bad = json.loads(json.dumps(bad).replace("SRC", src))
    with pytest.raises(ValueError, match=msg):
        llm_fix.suggest(store, proj, _finding(proj), "ana", chat=lambda s, u: {"text": json.dumps(bad), "model": "m", "usage": {}, "seconds": 0})
    assert store.proposals() == []


def test_non_json_answer_is_a_clean_error(store, shop):
    proj, _ = shop
    with pytest.raises(ValueError, match="JSON"):
        llm_fix.suggest(store, proj, _finding(proj), "ana", chat=lambda s, u: {"text": "Claro! Eis a correção: troque a coluna.", "model": "m", "usage": {}, "seconds": 0})


def test_prompt_never_contains_data_samples_or_secrets(store, shop):
    proj, _ = shop
    f = _finding(proj)
    f["evidence"] = {"sample": [{"email": "joao@x.com"}], "rows": [1], "observed": {"v": 9}, "owner": "dados@x.com"}
    ctx = llm_fix.build_context(proj, f)
    prompt = llm_fix.build_prompt(ctx)
    assert "joao@x.com" not in prompt and "sample" not in prompt and "dados@x.com" in prompt and "stg_orders" in prompt


def test_jev_routes_mechanical_fixes_without_calling_the_llm(store, shop, monkeypatch):
    proj, work = shop
    store.set_setting("secret.jev_api_key", "k")
    fx = Fake({"/api/alpha/decisions": lambda b: {"model": "typesafe/jev-1.13", "answers": {
        "real_issue": {"type": "noul", "noul": 0.9}, "likely_false_positive": {"type": "noul", "noul": 0.05},
        "urgency": {"type": "score", "score": 1.0, "confidence": 0.9, "probabilities": {}, "legend": {}},
        "fix_path": {"type": "choice", "choice": "deterministic_rename", "confidence": 0.95, "probabilities": {"deterministic_rename": 0.95, "llm_edit": 0.02}}}}})
    store.set_setting("jev.provider", "openrouter"); store.set_setting("jev.base_url", fx.url); store.set_setting("jev.enabled", True)
    orders = next(x for x in proj.nodes.values() if x.name == "orders")
    f = {**_finding(proj), "nodes": [orders.unique_id],
         "fix": {"kind": "rename_column", "node": "stg_orders", "old": "order_id", "new": "order_id_x", "targets": ["orders"]}}
    called = []
    r = llm_fix.suggest(store, proj, f, "ana", chat=lambda s, u: called.append(1))
    fx.close()
    assert r["source"] == "rule" and not called and r["triage"]["fix_path"] == "deterministic_rename"
    assert store.get_setting("state.jev.SQL001-test")["fix_path"] == "deterministic_rename"


def test_jev_can_say_no_action_and_force_overrides(store, shop, monkeypatch):
    proj, work = shop
    store.set_setting("secret.jev_api_key", "k")
    fx = Fake({"/api/alpha/decisions": lambda b: {"model": "j", "answers": {
        "real_issue": {"type": "noul", "noul": 0.1}, "likely_false_positive": {"type": "noul", "noul": 0.9},
        "urgency": {"type": "score", "score": 0.0, "confidence": 0.9, "probabilities": {}, "legend": {}},
        "fix_path": {"type": "choice", "choice": "no_action", "confidence": 0.93, "probabilities": {"no_action": 0.93}}}}})
    store.set_setting("jev.provider", "openrouter"); store.set_setting("jev.base_url", fx.url); store.set_setting("jev.enabled", True)
    src = next(x for x in proj.nodes.values() if x.name == "stg_orders").path
    first = (work / src).read_text().splitlines()[0]
    edit = {"explanation": "e", "confidence": 0.5, "edits": [{"file": src, "find": first, "replace": first + " "}]}
    chat = lambda s, u: {"text": json.dumps(edit), "model": "m", "usage": {}, "seconds": 0}
    r = llm_fix.suggest(store, proj, _finding(proj), "ana", chat=chat)
    assert r["source"] == "skipped" and "no_action" not in r.get("proposal", "")
    r2 = llm_fix.suggest(store, proj, _finding(proj), "ana", force_llm=True, chat=chat)
    fx.close()
    p2 = store.proposal(r2["proposal"])
    assert r2["source"] == "llm" and p2["triage"]["fix_path"] == "no_action" and p2["created_by"] == "ana"


# ---------------------------------------------------------------- modelo local real (Ollama)
OLLAMA = os.environ.get("INTENTFLOW_OLLAMA_URL")


@pytest.mark.skipif(not OLLAMA, reason="local Ollama not configured (INTENTFLOW_OLLAMA_URL=http://127.0.0.1:11434/v1)")
def test_real_local_model_chat_models_and_fix_pipeline_stays_safe(store, shop):
    _cfg(store, "local", OLLAMA, os.environ.get("INTENTFLOW_OLLAMA_MODEL", "qwen2.5:3b"))
    assert os.environ.get("INTENTFLOW_OLLAMA_MODEL", "qwen2.5:3b") in llm.list_models(store)
    r = llm.chat(store, "Responda só com JSON.", 'Devolva {"ok": true}', json_mode=True)
    assert "ok" in r["text"] and r["seconds"] > 0
    proj, work = shop
    src = next(x for x in proj.nodes.values() if x.name == "stg_orders").path
    before = (work / src).read_text()
    try:
        out = llm_fix.suggest(store, proj, _finding(proj), "ana")
        p = store.proposal(out["proposal"])
        assert p["status"] == "pending" and p["changes"] and p["llm"]["provider"] == "local"
    except ValueError as e:                    # a small model may get the format wrong: it must be a clean error, never an applied edit
        assert any(w in str(e) for w in ("JSON", "edit", "time", "scope", "empty", "file"))
    assert (work / src).read_text() == before and all(x["status"] != "applied" for x in store.proposals())


def test_guards_reject_ref_removal_and_warn_on_dropped_macros_or_big_edits(store, shop):
    proj, work = shop
    src = next(x for x in proj.nodes.values() if x.name == "stg_orders").path
    text = (work / src).read_text()
    ref_line = next(l for l in text.splitlines() if "ref(" in l)
    bad = {"edits": [{"file": src, "find": ref_line, "replace": "from raw.orders"}]}
    with pytest.raises(ValueError, match="ref\\(\\)"):
        llm_fix.suggest(store, proj, _finding(proj), "ana", chat=lambda s, u: {"text": json.dumps(bad), "model": "m", "usage": {}, "seconds": 0})
    assert llm_fix.guards("a.sql", "select {{ macro(x) }} as y from {{ ref('t') }}", "select x as y from {{ ref('t') }}") == \
        ([], ["a.sql: the edit removes 1 Jinja/macro expression(s); confirm that is part of the fix"])
    r, w = llm_fix.guards("a.sql", "\n".join(f"c{i}" for i in range(40)), "\n".join(f"d{i}" for i in range(40)))
    assert r == [] and any("80 lines" in x for x in w)


def test_dropped_jinja_forces_build_validation_and_extra_review(store, shop):
    proj, work = shop
    node = next(x for x in proj.nodes.values() if x.name == "daily_revenue")
    text = (work / node.path).read_text()
    assert "{% if is_incremental() %}" in text
    f = {**_finding(proj), "nodes": [node.unique_id]}
    edit = {"explanation": "x", "confidence": 0.99, "edits": [{"file": node.path, "find": "{% if is_incremental() %}", "replace": ""}]}
    r = llm_fix.suggest(store, proj, f, "ana", chat=lambda s, u: {"text": json.dumps(edit), "model": "m", "usage": {}, "seconds": 0})
    p = store.proposal(r["proposal"])
    assert p["policy"]["needs_extra_review"] and p["policy"]["validate_mode"] == "build" and any("Jinja" in w for w in p["warnings"])


def test_extract_json_tolerates_trailing_commas():
    from intentflow.enterprise import llm_fix
    assert llm_fix.extract_json('```json\n{"edits": [{"a": 1,},], "confidence": 0.5,}\n```') == {"edits": [{"a": 1}], "confidence": 0.5}
