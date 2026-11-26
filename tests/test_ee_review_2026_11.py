"""Regressions from the 2026-11 review: SSO key rotation, sealed-cookie robustness, refresh lock bookkeeping."""
import base64
import threading
import time

from cryptography.hazmat.primitives.asymmetric import rsa

from intentflow.enterprise import oidc, web_ext


def _jwk(kid):
    n = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()
    b = lambda i: base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
    return {"kid": kid, "kty": "RSA", "e": b(n.e), "n": b(n.n)}


def test_unknown_kid_refetches_jwks_once():
    keys = [[_jwk("old")]]
    calls = []

    def get(url):
        calls.append(url)
        if url.endswith("openid-configuration"):
            return {"issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks"}
        return {"keys": keys[0]}
    o = oidc.OIDC("https://idp.example", "c", "s", "https://app/cb", get=get)
    assert o._key("old")
    keys[0] = [_jwk("old"), _jwk("new")]                     # provider rotated its signing key
    o._jwks = (time.time() - 60, o._jwks[1])                   # cache older than the 30 s refetch floor
    assert o._key("new")
    o._jwks = (time.time(), o._jwks[1])
    n = len(calls)
    try:
        o._key("forged")
    except oidc.SSOError:
        pass
    assert len(calls) == n                                     # a fresh cache never refetches for an unknown kid


def test_unseal_non_object_is_rejected():
    key = b"k" * 32
    assert oidc.unseal(key, oidc.seal(key, {"t": time.time()})) is not None
    raw = oidc._b64u(b"[1, 2]")
    import hashlib, hmac
    forged = raw + "." + oidc._b64u(hmac.new(key, raw.encode(), hashlib.sha256).digest())
    assert oidc.unseal(key, forged) is None


def test_refresh_lock_is_shared_while_waiting(monkeypatch):
    started, release = threading.Event(), threading.Event()
    running, peak = [0], [0]

    def slow(h, prim, sid, name):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        started.set()
        release.wait(2)
        running[0] -= 1
        return True
    monkeypatch.setattr(web_ext, "_refresh_session", slow)
    ts = [threading.Thread(target=web_ext.refresh_session, args=(None, None, "sid1", "u")) for _ in range(3)]
    for t in ts:
        t.start()
    started.wait(2)
    time.sleep(0.2)
    release.set()
    for t in ts:
        t.join(3)
    assert peak[0] == 1 and "sid1" not in web_ext._REFRESH_LOCKS


def test_llm_has_no_default_model(tmp_path):
    import pytest
    from intentflow.store import Store
    from intentflow.enterprise import llm
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("llm.provider", "openai")
    assert llm.settings(st)["model"] == "" and not llm.status(st)["configured"]
    with pytest.raises(llm.LLMError):
        llm.chat(st, "s", "u", key="k")


