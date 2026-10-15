"""Every translatable string has a Portuguese entry with the same placeholders; ids do not depend on the language."""
import ast
import json
import re
import string
from pathlib import Path

from intentflow import i18n
from intentflow.model import Finding

SRC = Path(__file__).resolve().parent.parent / "src" / "intentflow"


def _placeholders(s: str) -> list:
    return sorted(f for _, f, _, _ in string.Formatter().parse(s) if f)


def _python_strings():
    for p in SRC.rglob("*.py"):
        for n in ast.walk(ast.parse(p.read_text())):
            if not isinstance(n, ast.Call):
                continue
            fn = getattr(n.func, "id", getattr(n.func, "attr", None))
            idx = 0 if fn in ("tr", "N_") else 1 if fn == "check" else None
            if idx is not None and len(n.args) > idx and isinstance(n.args[idx], ast.Constant) and isinstance(n.args[idx].value, str):
                yield p.name, n.args[idx].value


def _ui_strings():
    html = (SRC / "web.html").read_text()
    for m in re.finditer(r'\bT\("((?:[^"\\]|\\.)*)"', html):
        yield "web.html", json.loads('"' + m.group(1) + '"')


def test_every_string_has_a_portuguese_translation_with_the_same_placeholders():
    pt = json.loads((SRC / "locale" / "pt.json").read_text())
    missing = sorted({f"{f}: {s}" for f, s in [*_python_strings(), *_ui_strings()] if s not in pt})
    assert not missing, "\n".join(missing[:40])
    bad = [k for k, v in pt.items() if _placeholders(k) != _placeholders(v)]
    assert not bad, bad


def test_language_selection_and_rendering(monkeypatch):
    monkeypatch.setenv("INTENTFLOW_LANG", "pt_BR")
    assert i18n.lang() == "pt" and i18n.tr("{n} sample(s)", n=3) == "3 amostra(s)"
    monkeypatch.setenv("INTENTFLOW_LANG", "en")
    assert i18n.tr("{n} sample(s)", n=3) == "3 sample(s)"
    i18n.set_request_lang("pt")
    try:
        assert i18n.tr("No differences.") == "Sem diferenças."      # the request header wins over the process language
    finally:
        i18n.set_request_lang(None)


def test_finding_id_does_not_depend_on_the_language(monkeypatch):
    def make():
        return Finding("DBT003", "low", i18n.tr("{model}: no owner", model="orders"), nodes=["model.p.orders"])
    monkeypatch.setenv("INTENTFLOW_LANG", "en")
    en = make()
    monkeypatch.setenv("INTENTFLOW_LANG", "pt")
    pt = make()
    assert en.title != pt.title and en.id == pt.id
    assert i18n.cat(pt.title, "!").en == en.title + "!"
