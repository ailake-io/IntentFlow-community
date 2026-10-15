"""Outputs: Slack/webhook, e-mail (SMTP) and pull request comment (GitHub). Secrets only through environment variables.
Extensions add other sink types (`notify_deliver` hook).

Configuration (in the store): `notify.sinks` = list of {name, type, url_env|..., min_severity} and
`notify.routes` = list of {sink, owner?, check_prefix?, min_severity?}. Managed through the CLI/UI.
"""
from __future__ import annotations

import hashlib
import json
import os
import smtplib
import time
import urllib.request
from email.message import EmailMessage

from . import extensions
from .i18n import tr
from .model import SEVERITIES
from .store import Store

COOLDOWN_S = 24 * 3600
CORE_TYPES = ("slack", "webhook", "email", "github")
ENTERPRISE_TYPES = ("gitlab", "bitbucket", "azure_devops", "pagerduty", "opsgenie", "jira", "teams")


def sink_types() -> list[str]:
    return list(CORE_TYPES) + [t for m in extensions.loaded() for t in getattr(m, "SINK_TYPES", ())]


def render_markdown(incidents, title: str | None = None, limit: int = 15) -> str:
    title = title or tr("IntentFlow: problems found before production")
    if not incidents:
        return f"### {title}\n\n" + tr("No open problems.") + " ✅"
    L = [f"### {title}", "", tr("{n} incident(s) grouped by root cause:", n=len(incidents)), ""]
    icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪", "info": "ℹ️"}
    for inc in incidents[:limit]:
        L.append(f"- {icon.get(inc.severity, '')} **{inc.severity}** — {inc.title}")
        for f in inc.findings[:5]:
            L.append(f"  - `{f['check_id'] if 'check_id' in f else f.get('check')}` {f['title']}")
        if len(inc.findings) > 5:
            L.append("  - " + tr("… and {n} more", n=len(inc.findings) - 5))
        if inc.owners:
            L.append("  - " + tr("owner: {owners}", owners=", ".join(inc.owners)))
    if len(incidents) > limit:
        L.append("\n" + tr("… and {n} more incident(s).", n=len(incidents) - limit))
    return "\n".join(L)


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 10.0) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def env(sink: dict, key: str, default: str) -> str:
    v = os.environ.get(sink.get(key, default))
    if not v:
        raise RuntimeError(tr("variable {name} is not set", name=sink.get(key, default)))
    return v


def _send_mail(sink: dict, host: str, port: int, m: EmailMessage) -> None:
    """SMTP with opportunistic STARTTLS (`tls`: auto|starttls|ssl|none), login with SMTP_USER/SMTP_PASSWORD and certificate checks.
    Refuses to authenticate over an unencrypted channel unless `allow_insecure_auth: true` (development only)."""
    import ssl
    mode = sink.get("tls", "auto")
    ctx = ssl.create_default_context()
    if sink.get("tls_verify") is False:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    user, pw = os.environ.get(sink.get("user_env", "SMTP_USER")), os.environ.get(sink.get("password_env", "SMTP_PASSWORD"))
    cls = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    kw = {"context": ctx} if mode == "ssl" else {}
    with cls(host, port, timeout=10, **kw) as s:
        s.ehlo()
        encrypted = mode == "ssl"
        if mode in ("auto", "starttls") and s.has_extn("starttls"):
            s.starttls(context=ctx)
            s.ehlo()
            encrypted = True
        elif mode == "starttls":
            raise RuntimeError(tr("SMTP server does not offer STARTTLS"))
        if user and pw:
            if not encrypted and not sink.get("allow_insecure_auth"):
                raise RuntimeError(tr("SMTP login refused: channel without TLS (use tls: starttls/ssl or allow_insecure_auth)"))
            s.login(user, pw)
        s.send_message(m)


def deliver(sink: dict, subject: str, body: str, meta: dict | None = None) -> None:
    """`meta` (optional): {id, severity, title} of the incident, used by sinks that deduplicate or prioritize."""
    meta = meta or {}
    t = sink["type"]
    if t in ("slack", "webhook"):
        url = os.environ.get(sink["url_env"])
        if not url:
            raise RuntimeError(tr("variable {name} is not set", name=sink["url_env"]))
        post_json(url, {"text": f"*{subject}*\n{body}"} if t == "slack" else {"subject": subject, "body": body})
    elif t == "email":
        host, port = os.environ.get(sink.get("host_env", "SMTP_HOST"), "localhost"), int(os.environ.get(sink.get("port_env", "SMTP_PORT"), "25"))
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = subject, sink.get("from", "intentflow@localhost"), sink["to"]
        m.set_content(body)
        _send_mail(sink, host, port, m)
    elif t == "github":
        tok = os.environ.get(sink.get("token_env", "GITHUB_TOKEN"))
        if not tok:
            raise RuntimeError(tr("GITHUB_TOKEN is not set"))
        api = sink.get("api", "https://api.github.com").rstrip("/")
        post_json(f"{api}/repos/{sink['repo']}/issues/{sink['pr']}/comments", {"body": body},
                  {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    elif not extensions.first("notify_deliver", sink, subject, body, meta):
        raise ValueError(tr("sink type {type} needs IntentFlow Enterprise", type=t) if t in ENTERPRISE_TYPES else
                         tr("unknown sink type: {type}", type=t))


def _matches(route: dict, inc) -> bool:
    if route.get("owner") and route["owner"] not in inc.owners:
        return False
    if route.get("check_prefix") and not any((f.get("check_id") or f.get("check", "")).startswith(route["check_prefix"]) for f in inc.findings):
        return False
    ms = route.get("min_severity")
    return not ms or SEVERITIES.index(inc.severity) <= SEVERITIES.index(ms)


def send(store: Store, incidents, dry_run: bool = False, now: float | None = None) -> list[dict]:
    """Send incidents through the routes. Anti-spam: the same incident with the same severity is only resent after 24 h."""
    now = now or time.time()
    sinks = {s["name"]: s for s in store.get_setting("notify.sinks", [])}
    routes = store.get_setting("notify.routes", [])
    log = []
    for inc in incidents:
        sig = hashlib.sha1(f"{inc.id}|{inc.severity}|{len(inc.findings)}".encode()).hexdigest()[:12]
        targets = {r["sink"] for r in routes if _matches(r, inc) and r["sink"] in sinks}
        for name in sorted(targets):
            key = f"state.notified.{name}.{sig}"
            last = store.get_setting(key)
            if last and now - last < COOLDOWN_S:
                log.append({"incident": inc.id, "sink": name, "status": "suppressed"})
                continue
            if dry_run:
                log.append({"incident": inc.id, "sink": name, "status": "dry-run"})
                continue
            try:
                deliver(sinks[name], f"[{inc.severity}] {inc.title}", render_markdown([inc]), {"id": inc.id, "severity": inc.severity, "title": inc.title})
                store.set_setting(key, now)
                store.audit("system", "notified", {"incident": inc.id, "sink": name})
                log.append({"incident": inc.id, "sink": name, "status": "sent"})
            except Exception as e:
                log.append({"incident": inc.id, "sink": name, "status": f"error: {str(e)[:120]}"})
    return log
