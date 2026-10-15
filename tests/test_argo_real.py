"""Real Argo Workflows (k3d + Argo 3.4.17, `argo-server --auth-mode=server --secure=false`, port-forward). Skipped without INTENTFLOW_ARGO_URL.
Prerequisite: workflows `it-argo-ok-*` (Succeeded) and `it-argo-fail-*` (Failed) in the `argo` namespace."""
import os

import pytest

from intentflow.orchestrators.more import ArgoClient

URL = os.environ.get("INTENTFLOW_ARGO_URL")
pytestmark = pytest.mark.skipif(not URL, reason="real Argo not configured")


def test_argo_real_workflows_are_normalized():
    rs = ArgoClient(URL, "argo").runs()
    by = {r.pipeline: r for r in rs}
    assert by["it-argo-ok"].state == "success" and by["it-argo-fail"].state == "failed"
    assert all(r.duration is not None and 0 < r.duration < 600 for r in rs if r.state in ("success", "failed"))
    assert by["it-argo-ok"].url.endswith(by["it-argo-ok"].run_id)


def test_wrong_namespace_is_empty_not_an_error():
    assert ArgoClient(URL, "inexistente").runs() == []
