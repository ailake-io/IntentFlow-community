"""Real Airbyte OSS (abctl, Airbyte 1.15): faker source → End-to-End destination, one finished and one cancelled sync.
Skipped without INTENTFLOW_AIRBYTE_URL. Credentials: AIRBYTE_CLIENT_ID / AIRBYTE_CLIENT_SECRET (`abctl local credentials`)."""
import os

import pytest

from intentflow.orchestrators.ingestion import AirbyteClient

URL = os.environ.get("INTENTFLOW_AIRBYTE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="real Airbyte not configured")


def test_airbyte_real_jobs_with_client_credentials(monkeypatch):
    monkeypatch.delenv("AIRBYTE_TOKEN", raising=False)
    rs = {r.run_id: r for r in AirbyteClient(URL).runs()}
    assert {r.state for r in rs.values()} >= {"success", "failed"}                      # succeeded + cancelled
    ok = next(r for r in rs.values() if r.state == "success")
    assert ok.pipeline == "it-faker-to-e2e:sync" and ok.duration and ok.duration > 10
    assert ok.start and ok.end and ok.end > ok.start


def test_wrong_client_secret_gives_clear_error(monkeypatch):
    monkeypatch.delenv("AIRBYTE_TOKEN", raising=False)
    monkeypatch.setenv("AIRBYTE_CLIENT_SECRET", "errado")
    with pytest.raises(RuntimeError, match="login no Airbyte falhou"):
        AirbyteClient(URL).runs()
