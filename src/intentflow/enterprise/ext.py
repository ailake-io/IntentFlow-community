"""Built-in extension: wires the team features (SSO, roles, fixes, AI, sinks) into the core hooks."""
from __future__ import annotations

from . import sinks, web_ext

FEATURES = {"sso", "project_roles", "projects", "fixes", "ai", "enterprise_sinks"}
SINK_TYPES = sinks.SINK_TYPES


def register_cli(sub):
    from . import cli_ext
    cli_ext.register(sub)


web_get = web_ext.web_get
web_post = web_ext.web_post
web_post_raw = web_ext.web_post_raw
refresh_session = web_ext.refresh_session
logout_url = web_ext.logout_url
me = web_ext.me
notify_deliver = sinks.deliver


def integration_cards(store, card) -> None:
    from intentflow.i18n import tr
    from . import jev, llm
    ls, js = llm.status(store), jev.status(store)
    off = tr("Not configured")
    card(tr("AI"), "llm", tr("LLM to suggest fixes"), bool(ls["provider"]), "ok" if ls["configured"] else ("warn" if ls["provider"] else "off"),
         tr("{provider} · {model} · key: {source}", provider=ls["provider"], model=ls["model"] or tr("no model"), source=ls["key_source"])
         if ls["provider"] else tr("Not configured (Settings → AI)"),
         ["llm.provider", "llm.model", "llm.base_url"], {k: ls[k] for k in ("provider", "model", "key_source")})
    card(tr("AI"), "jev", tr("Jev (decisions: triage and risk)"), bool(js["provider"]), "ok" if js["configured"] and js["enabled"] else ("warn" if js["provider"] else "off"),
         tr("{provider} · {model} · key: {source}", provider=js["provider"], model=js["model"], source=js["key_source"])
         + ("" if js["enabled"] else " · " + tr("off")) if js["provider"] else off,
         ["jev.provider", "jev.enabled"])
