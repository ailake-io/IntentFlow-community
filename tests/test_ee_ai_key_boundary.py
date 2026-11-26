"""Server-wide AI keys never go to a custom endpoint chosen inside a project (credential exfiltration / SSRF)."""
from intentflow.store import Store
from intentflow.enterprise import jev, llm


def test_env_keys_only_reach_the_default_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-server")
    st = Store()
    st.set_setting("llm.provider", "openai")
    assert llm.api_key(st, "openai") == ("sk-server", "env")
    st.set_setting("llm.base_url", "https://attacker.example/v1")
    assert llm.api_key(st, "openai") == (None, "none")
    assert llm.api_key(st, "openai", "https://api.openai.com/v1/") == ("sk-server", "env")
    st.set_setting("jev.provider", "openrouter")
    st.set_setting("jev.base_url", "https://attacker.example")
    assert jev.api_key(st) == (None, "none")


def test_generic_settings_endpoint_refuses_ai_keys(tmp_path):
    import json, threading, urllib.request
    from http.server import ThreadingHTTPServer
    from intentflow.web import make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(str(tmp_path / "s.db")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/settings", method="POST",
                                     data=json.dumps({"key": "llm.base_url", "value": "http://169.254.169.254"}).encode(),
                                     headers={"X-IntentFlow": "1", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.shutdown()
