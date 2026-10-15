"""Extension point: packages register a module under the `intentflow.extensions` entry-point group.

A module may define any of these hooks (all optional):
  FEATURES: set[str]                                  shown to the UI in /api/me
  register_cli(sub)                                   add argparse subcommands
  web_get(h, prim, path, q) -> bool                   handle a GET before core routing (True = handled)
  web_post(h, prim, parts, body, query) -> bool       handle a POST before core routing
  refresh_session(h, prim, sid, user) -> bool         keep an SSO session alive (False ends it)
  logout_url(h, prim, info) -> str | None             provider logout URL for an SSO session
  me(h, prim, who) -> dict                            extra fields for /api/me
  notify_deliver(sink, subject, body, meta) -> bool   deliver to a sink type the core does not know
  integration_cards(store, card) -> None              add cards to the Integrations page
"""
from __future__ import annotations

import sys
from functools import cache
from importlib.metadata import entry_points


@cache
def loaded() -> tuple:
    out = []
    for ep in entry_points(group="intentflow.extensions"):
        try:
            out.append(ep.load())
        except Exception as e:      # a broken extension must not take the core down
            print(f"warning: extension {ep.name} failed to load: {e}", file=sys.stderr)
    return tuple(out)


def features() -> list[str]:
    return sorted({f for m in loaded() for f in getattr(m, "FEATURES", ())})


def first(hook: str, *a, **k):
    """Result of the first extension that implements `hook` and returns something other than None."""
    for m in loaded():
        fn = getattr(m, hook, None)
        if fn is not None:
            r = fn(*a, **k)
            if r is not None:
                return r
    return None


def each(hook: str, *a, **k) -> None:
    for m in loaded():
        fn = getattr(m, hook, None)
        if fn is not None:
            fn(*a, **k)
