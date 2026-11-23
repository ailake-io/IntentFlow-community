"""Browser end-to-end: the page renders under its CSP (no violations, no JS errors) in local and team mode.
Needs `pip install playwright && playwright install chromium`; skipped otherwise (the CI `e2e` job runs it)."""
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from intentflow.store import Store  # noqa: E402
from intentflow.web import build_server  # noqa: E402


@pytest.fixture
def ui(tmp_path):
    db = str(tmp_path / "s.db")
    Store(db).close()
    srv = build_server(db, 0, "127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield db, f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()
    srv.server_close()


def _open(url):
    p = sync_api.sync_playwright().start()
    try:
        b = p.chromium.launch()
    except Exception as e:  # browser binaries not installed
        p.stop()
        pytest.skip(f"chromium unavailable: {e}")
    page = b.new_page()
    problems = []
    page.on("console", lambda m: m.type == "error" and problems.append(m.text))
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.goto(url)
    page.wait_for_load_state("networkidle")
    return p, b, page, problems


def test_local_mode_renders_without_csp_or_js_errors(ui):
    _, url = ui
    p, b, page, problems = _open(url)
    try:
        assert "default-src" in page.request.get(url).headers["content-security-policy"]
        assert page.locator("body").inner_text().strip()
        assert problems == []
    finally:
        b.close()
        p.stop()


def test_team_mode_shows_login_and_accepts_token(ui):
    db, url = ui
    with Store(db) as st:
        token = st.add_user("ana", "admin")
    p, b, page, problems = _open(url)
    try:
        page.locator("input[type=password]").first.fill(token)
        page.keyboard.press("Enter")
        page.locator("aside").wait_for(state="visible", timeout=10000)
        me = page.evaluate("fetch('/api/me', {headers: {'X-IntentFlow': '1'}}).then(r => r.json())")
        user = me.get("user")
        assert (user.get("name") if isinstance(user, dict) else user) == "ana"
        assert problems == []
    finally:
        b.close()
        p.stop()
