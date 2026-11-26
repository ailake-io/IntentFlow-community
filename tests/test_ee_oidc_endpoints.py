import pytest

from intentflow.enterprise.oidc import OIDC, SSOError


def _oidc(issuer, **ep):
    disc = {"issuer": issuer, "authorization_endpoint": issuer + "/auth", "token_endpoint": issuer + "/token", "jwks_uri": issuer + "/jwks", **ep}
    return OIDC(issuer, "cid", "secret", "https://app/cb", get=lambda url: disc)


def test_well_formed_discovery_is_accepted_including_other_public_hosts():
    o = _oidc("https://idp.example.com", token_endpoint="https://oauth2.other-public.com/token")   # ex.: Google usa outro host
    assert o.discovery()["token_endpoint"].startswith("https://oauth2.other-public.com")


@pytest.mark.parametrize("bad", ["http://idp.example.com/token", "https://169.254.169.254/latest", "https://10.0.0.5/token",
                                 "https://localhost/token", "file:///etc/passwd", "https://svc.internal/token",
                                 "https://2130706433/token", "https://0x7f000001/token", "https://127.1/token", "https://[::ffff:127.0.0.1]/token",
                                 "https://[::1]/token", "https://0.0.0.0/token"])
def test_discovery_cannot_redirect_the_client_secret_to_cleartext_or_internal_hosts(bad):
    with pytest.raises(SSOError):
        _oidc("https://idp.example.com", token_endpoint=bad).discovery()


def test_local_dev_issuer_keeps_working_over_http():
    assert _oidc("http://127.0.0.1:8080/realms/dados").discovery()["token_endpoint"].startswith("http://127.0.0.1")


def _gai(*addrs):
    import socket
    return lambda *a, **k: [(socket.AF_INET6 if ":" in x else socket.AF_INET, 1, 6, "", (x, 443)) for x in addrs]


def test_dns_name_that_resolves_to_internal_address_is_rejected(monkeypatch):
    import socket
    pub, priv = _gai("93.184.216.34"), _gai("10.1.2.3")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: (priv if "sneaky" in host else pub)(host, *a, **k))
    with pytest.raises(SSOError, match="internal"):
        _oidc("https://idp.example.com", token_endpoint="https://sneaky.example.org/token").discovery()


@pytest.mark.parametrize("addr", ["127.0.0.1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1", "100.64.0.1",
                                  "169.254.169.254", "fd00::1", "224.0.0.1", "192.0.2.1", "::1", "0.0.0.0"])
def test_connection_guard_classifies_the_resolved_address_not_the_text(monkeypatch, addr):
    """Even an innocent name: if the resolver returns any non-public address, the connection is never opened."""
    import socket
    from intentflow.enterprise.oidc import _guarded_connect
    monkeypatch.setattr(socket, "getaddrinfo", _gai(addr))
    opened = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: opened.append(a))
    with pytest.raises(SSOError):
        _guarded_connect("idp.example.com", 443, 5, allow_internal=False)
    assert not opened


def test_mixed_public_and_internal_records_are_rejected_and_public_ip_is_pinned(monkeypatch):
    import socket
    from intentflow.enterprise.oidc import _guarded_connect
    monkeypatch.setattr(socket, "getaddrinfo", _gai("93.184.216.34", "10.0.0.9"))
    with pytest.raises(SSOError):
        _guarded_connect("idp.example.com", 443, 5, allow_internal=False)
    seen = []
    monkeypatch.setattr(socket, "getaddrinfo", _gai("93.184.216.34"))
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: seen.append(addr) or "sock")
    assert _guarded_connect("idp.example.com", 443, 5, False) == "sock" and seen == [("93.184.216.34", 443)]   # conecta no IP validado


def test_idp_requests_do_not_follow_redirects():
    import urllib.error
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from intentflow.enterprise.oidc import http_get_json

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/x")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            http_get_json(f"http://127.0.0.1:{srv.server_address[1]}/", allow_internal=True)
        assert e.value.code == 302
    finally:
        srv.shutdown()


def test_public_issuer_cannot_fetch_from_loopback_even_with_a_literal_url():
    from intentflow.enterprise.oidc import http_get_json
    with pytest.raises(SSOError, match="internal"):
        http_get_json("http://127.0.0.1:9/x")            # default: internal destination blocked at connect time


def test_issuer_name_that_merely_resolves_to_loopback_does_not_unlock_internal_access(monkeypatch):
    import socket
    monkeypatch.delenv("INTENTFLOW_SSO_ALLOW_INTERNAL", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _gai("127.0.0.1"))
    assert _oidc("https://idp.example.com").local is False
    assert _oidc("http://127.0.0.1:8080/r").local is True and _oidc("http://localhost:8080/r").local is True
    monkeypatch.setenv("INTENTFLOW_SSO_ALLOW_INTERNAL", "1")          # IdP on a private network: an explicit operator decision
    assert _oidc("https://sso.corp.example").local is True
