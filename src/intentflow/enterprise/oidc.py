"""OpenID Connect SSO (authorization code + PKCE). Validates the RS256 id_token with the provider's JWKS.

Client secret only through an environment variable (`sso.client_secret_env`). Providers tested: a local simulated IdP and Keycloak.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
import time
import urllib.parse
import urllib.request


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):      # a validated endpoint cannot be diverted (302) to an internal host
        return None


def _addr_internal(ip: ipaddress._BaseAddress) -> bool:
    """An address that is NOT on the public internet. Classifies the resolved address (not the host text): no parser differential."""
    if isinstance(ip, ipaddress.IPv6Address):
        emb = ip.ipv4_mapped or (ip.sixtofour if ip.sixtofour else None) or (ip.teredo[1] if ip.teredo else None)
        if emb is None and ip in ipaddress.ip_network("64:ff9b::/96"):       # NAT64: the IPv4 sits in the last 32 bits
            emb = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if emb is not None and _addr_internal(emb):
            return True
    return not ip.is_global or ip.is_multicast


def _resolve(host: str, port: int) -> list[ipaddress._BaseAddress]:
    out = []
    for fam, _t, _p, _c, sa in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if fam in (socket.AF_INET, socket.AF_INET6):
            out.append(ipaddress.ip_address(sa[0].split("%")[0]))
    return out


def _guarded_connect(host: str, port: int, timeout, allow_internal: bool) -> socket.socket:
    """Resolve ONCE, reject if ANY address is internal (unless the issuer is local) and connect to THAT IP:
    no second resolution, so there is no DNS rebinding window."""
    addrs = _resolve(host, port)
    if not addrs:
        raise SSOError(f"could not resolve {host}")
    if not allow_internal and any(_addr_internal(a) for a in addrs):
        raise SSOError(f"{host} resolves to an internal address; blocked")
    last: Exception | None = None
    for a in addrs:
        try:
            return socket.create_connection((str(a), port), timeout=timeout)
        except OSError as e:
            last = e
    raise last or OSError("no address")


class _GuardedHTTP(http.client.HTTPConnection):
    allow_internal = False

    def connect(self):
        self.sock = _guarded_connect(self.host, self.port, self.timeout, self.allow_internal)


class _GuardedHTTPS(http.client.HTTPSConnection):
    allow_internal = False

    def connect(self):
        sock = _guarded_connect(self.host, self.port, self.timeout, self.allow_internal)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)       # SNI/certificate by name, connection to the validated IP


def _opener(allow_internal: bool):
    http_c = type("H", (_GuardedHTTP,), {"allow_internal": allow_internal})
    https_c = type("S", (_GuardedHTTPS,), {"allow_internal": allow_internal})

    class HH(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(http_c, req)

    class SH(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(https_c, req, context=self._context)
    # empty ProxyHandler: an environment proxy would bypass the destination check
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), HH, SH, _NoRedirect)


_OPENERS = {False: _opener(False), True: _opener(True)}


def _http_only(url: str) -> None:
    """build_opener keeps file:, ftp: and data: handlers: refuse every scheme but http(s)."""
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise SSOError("only http(s) URLs are allowed")


def http_get_json(url: str, timeout: float = 10.0, allow_internal: bool = False):
    _http_only(url)
    with _OPENERS[allow_internal].open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read())


def http_post_form(url: str, data: dict, headers: dict | None = None, timeout: float = 10.0, allow_internal: bool = False):
    _http_only(url)
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", **(headers or {})})
    with _OPENERS[allow_internal].open(req, timeout=timeout) as r:
        return json.loads(r.read())


class SSOError(Exception):
    pass


def _local_issuer(host: str) -> bool:
    """A "local" IdP (internal destinations allowed): only LITERAL loopback/`localhost`, or an explicit opt-in for an IdP on a private network.
    A name that merely RESOLVES to an internal address unlocks nothing (DNS is controlled by third parties)."""
    if os.environ.get("INTENTFLOW_SSO_ALLOW_INTERNAL") == "1":
        return True
    h = host.strip("[]").lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _host_internal(host: str) -> bool:
    """To decide whether the issuer is a local (dev) IdP. By well-known name or by the address the resolver returns."""
    h = host.strip("[]").lower().rstrip(".")
    if not h or h == "localhost" or h.endswith((".localhost", ".internal", ".local", ".lan", ".home.arpa")):
        return True
    try:
        return any(_addr_internal(a) for a in _resolve(h, 443))
    except OSError:
        return False


class OIDC:
    def __init__(self, issuer: str, client_id: str, client_secret: str, redirect_uri: str, get=http_get_json, post=http_post_form):
        self.issuer, self.client_id, self.secret, self.redirect = issuer.rstrip("/"), client_id, client_secret, redirect_uri
        import functools
        self.local = _local_issuer(urllib.parse.urlsplit(self.issuer).hostname or "")
        self.get = functools.partial(get, allow_internal=self.local) if get is http_get_json else get
        self.post = functools.partial(post, allow_internal=self.local) if post is http_post_form else post
        self._disc = None
        self._jwks = (0.0, None)

    def _check_endpoint(self, name: str, url: str) -> None:
        """Static check (scheme and host). Blocking internal destinations happens at CONNECT time (`_guarded_connect`),
        on the address actually resolved: parser/DNS cannot diverge between check and use."""
        u, iss = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.issuer)
        if u.scheme != iss.scheme or u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
            raise SSOError(f"provider {name} has an invalid scheme/host")
        if u.scheme == "http" and iss.scheme != "http":
            raise SSOError(f"{name} uses plain http")
        if not self.local and _host_internal(u.hostname):
            raise SSOError(f"{name} points to an internal address ({u.hostname}), but the issuer is public")

    def discovery(self) -> dict:
        if self._disc is None:
            disc = self.get(self.issuer + "/.well-known/openid-configuration")
            if disc.get("issuer", "").rstrip("/") != self.issuer:
                raise SSOError("provider issuer does not match the configuration")
            for k in ("authorization_endpoint", "token_endpoint", "jwks_uri", "end_session_endpoint"):
                if disc.get(k):
                    self._check_endpoint(k, disc[k])
            self._disc = disc                    # cached only once every check passed
        return self._disc

    def start(self) -> tuple[str, dict]:
        """(authorization URL, state to keep: state, nonce, verifier)."""
        st = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24), "verifier": secrets.token_urlsafe(48)}
        challenge = _b64u(hashlib.sha256(st["verifier"].encode()).digest())
        q = urllib.parse.urlencode({"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect,
                                    "scope": "openid email profile", "state": st["state"], "nonce": st["nonce"],
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
        return self.discovery()["authorization_endpoint"] + "?" + q, st

    def _key(self, kid: str):
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        ts, jwks = self._jwks
        fresh = jwks is None or time.time() - ts > 300
        for _attempt in range(2):
            if fresh:
                jwks = self.get(self.discovery()["jwks_uri"])
                self._jwks = (time.time(), jwks)
            for k in jwks.get("keys", []):
                if k.get("kid") == kid and k.get("kty") == "RSA":
                    return RSAPublicNumbers(int.from_bytes(_b64d(k["e"]), "big"), int.from_bytes(_b64d(k["n"]), "big")).public_key()
            # key rotation at the provider: an unknown kid refetches once, at most every 30 s (no fetch per forged token)
            if fresh or time.time() - self._jwks[0] < 30:
                break
            fresh = True
        raise SSOError("unknown signing key")

    def verify(self, id_token: str, nonce: str | None, now: float | None = None, require_exp: bool = True) -> dict:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            h64, p64, s64 = id_token.split(".")
            header, claims = json.loads(_b64d(h64)), json.loads(_b64d(p64))
        except (ValueError, TypeError):
            raise SSOError("malformed id_token") from None
        if header.get("alg") != "RS256":
            raise SSOError("algorithm not allowed (RS256 only)")
        try:
            self._key(header.get("kid", "")).verify(_b64d(s64), f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise SSOError("invalid signature") from None
        now = now if now is not None else time.time()
        aud = claims.get("aud")
        if claims.get("iss", "").rstrip("/") != self.issuer:
            raise SSOError("invalid issuer")
        if self.client_id not in (aud if isinstance(aud, list) else [aud]):
            raise SSOError("invalid audience")
        if (require_exp or "exp" in claims) and claims.get("exp", 0) < now - 30:
            raise SSOError("expired token")
        if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SSOError("invalid nonce")
        return claims

    def verify_logout_token(self, token: str, now: float | None = None) -> dict:
        """Back-channel logout (OIDC): same signature as the id_token + the logout_token rules."""
        claims = self.verify(token, None, now, require_exp=False)
        iat, t = claims.get("iat"), (now if now is not None else time.time())
        if iat is None or not (t - 600 <= float(iat) <= t + 60):      # iat is required by the spec: no timestamp, no replay window
            raise SSOError("logout_token outside the accepted time window")
        ev = claims.get("events") or {}
        if "http://schemas.openid.net/event/backchannel-logout" not in ev:
            raise SSOError("logout_token without the logout event")
        if "nonce" in claims:
            raise SSOError("logout_token must not contain a nonce")
        if not (claims.get("sid") or claims.get("sub")):
            raise SSOError("logout_token without sid/sub")
        return claims

    def refresh(self, refresh_token: str) -> dict:
        tok = self.post(self.discovery()["token_endpoint"], {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                                            "client_id": self.client_id, "client_secret": self.secret})
        if "id_token" not in tok:
            raise SSOError("refresh without id_token")
        claims = self.verify(tok["id_token"], None)
        claims["_raw_id_token"] = tok["id_token"]
        claims["_refresh_token"] = tok.get("refresh_token")
        return claims

    def logout_url(self, id_token: str | None, post_logout_redirect: str) -> str | None:
        """Client-initiated (RP-initiated) logout. None if the provider does not expose `end_session_endpoint`."""
        ep = self.discovery().get("end_session_endpoint")
        if not ep:
            return None
        q = {"post_logout_redirect_uri": post_logout_redirect, "client_id": self.client_id}
        if id_token:
            q["id_token_hint"] = id_token
        return ep + "?" + urllib.parse.urlencode(q)

    def finish(self, code: str, verifier: str, nonce: str) -> dict:
        tok = self.post(self.discovery()["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect, "client_id": self.client_id,
            "client_secret": self.secret, "code_verifier": verifier})
        if "id_token" not in tok:
            raise SSOError("the provider returned no id_token")
        claims = self.verify(tok["id_token"], nonce)
        claims["_raw_id_token"] = tok["id_token"]
        claims["_refresh_token"] = tok.get("refresh_token")
        return claims


def seal(key: bytes, payload: dict) -> str:
    raw = _b64u(json.dumps(payload).encode())
    return raw + "." + _b64u(hmac.new(key, raw.encode(), hashlib.sha256).digest())


def unseal(key: bytes, value: str, max_age: float = 600.0):
    try:
        raw, mac = value.split(".")
        if not hmac.compare_digest(_b64u(hmac.new(key, raw.encode(), hashlib.sha256).digest()), mac):
            return None
        d = json.loads(_b64d(raw))
        return d if isinstance(d, dict) and time.time() - float(d.get("t", 0)) < max_age else None
    except (ValueError, TypeError):
        return None


def map_groups(claims: dict, group_claim: str, group_map: list[dict]) -> tuple[str | None, dict[str, str]]:
    """Provider groups → (highest global role or None, {project: highest role}).

    `group_map`: [{"group": "data-admin", "role": "admin"}, {"group": "crm-eng", "role": "engineer", "project": "crm"}].
    Accepts groups as a list of strings, a single string, or paths (`/data-admin`).
    """
    rank = {"none": 0, "viewer": 1, "engineer": 2, "admin": 3}
    raw = claims.get(group_claim) or []
    if not raw and group_claim in (claims.get("_claim_names") or {}):
        # "group overage" (e.g. Entra ID with >200 groups): the list comes through another API and is NOT fetched here;
        # treating it as no groups is the safe behavior (never grants a role by inference)
        claims["_groups_overage"] = True
    groups = {g.strip().lstrip("/") for g in ([raw] if isinstance(raw, str) else raw) if isinstance(g, str)}
    glob, proj = None, {}
    for m in group_map or []:
        if str(m.get("group", "")).lstrip("/") not in groups:
            continue
        r, p = m.get("role"), m.get("project")
        if r not in rank:
            continue
        if p:
            if rank[r] > rank.get(proj.get(p, "none"), 0) or p not in proj:
                proj[p] = r
        elif glob is None or rank[r] > rank[glob]:
            glob = r
    return glob, proj
