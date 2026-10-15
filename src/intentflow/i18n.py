"""Translations. Source strings are English; `locale/<lang>.json` maps each English string to its translation.

Language, in order: the current request (UI sends `X-IntentFlow-Lang`), `INTENTFLOW_LANG`, then the locale variables
(`LC_ALL`, `LC_MESSAGES`, `LANG`). Anything starting with `pt` selects Portuguese; everything else is English.
Findings are stored already rendered, in the language of the process that ran the check.
"""
from __future__ import annotations

import contextvars
import json
import os
from functools import cache
from pathlib import Path

LANGS = ("en", "pt")
_request_lang: contextvars.ContextVar[str | None] = contextvars.ContextVar("intentflow_lang", default=None)


def normalize(value: str | None) -> str:
    return "pt" if (value or "").strip().lower().startswith("pt") else "en"


def lang() -> str:
    req = _request_lang.get()
    if req:
        return req
    for var in ("INTENTFLOW_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var):
            return normalize(os.environ[var])
    return "en"


def set_request_lang(value: str | None) -> None:
    _request_lang.set(normalize(value) if value else None)


@cache
def catalog(code: str) -> dict[str, str]:
    """Core catalog plus `locale/<code>.json` next to each extension module."""
    from . import extensions
    out: dict[str, str] = {}
    for base in [Path(__file__).parent] + [Path(m.__file__).parent for m in extensions.loaded()]:
        p = base / "locale" / f"{code}.json"
        if p.exists():
            out.update(json.loads(p.read_text(encoding="utf-8")))
    return out


class Msg(str):
    """Translated text that remembers its English rendering, so finding ids do not depend on the language."""
    en: str


def tr(msg: str, /, **kw) -> Msg:
    """Translate `msg` and fill `{placeholders}` from `kw`."""
    en = msg.format(**kw) if kw else msg
    code = lang()
    if code == "en":
        out = Msg(en)
    else:
        s = catalog(code).get(msg, msg)
        out = Msg(s.format(**kw) if kw else s)
    out.en = en
    return out


def N_(msg: str) -> str:
    """Mark a constant for translation without translating it yet (translate where it is used: `tr(CONST)`)."""
    return msg


def cat(*parts: str) -> Msg:
    """Concatenate translated pieces keeping the English rendering."""
    out = Msg("".join(parts))
    out.en = "".join(getattr(p, "en", p) for p in parts)
    return out
