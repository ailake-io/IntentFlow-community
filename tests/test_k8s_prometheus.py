"""Kubernetes real (k3d: INTENTFLOW_K8S_CONTEXT, namespace `itk` com oom-pod/idle-pod) e Prometheus real (INTENTFLOW_PROMETHEUS_URL)."""
import json, os

import pytest

from intentflow import monitoring
from intentflow.checks import Context, run_checks

CTX, PROM = os.environ.get("INTENTFLOW_K8S_CONTEXT"), os.environ.get("INTENTFLOW_PROMETHEUS_URL")


def test_quantities():
    q = monitoring.k8s_quantity
    assert q("512Mi") == 512 * 1024**2 and q("1G") == 1e9 and q("129e6") == 129e6 and q("2Gi") == 2 * 1024**3 and q(None) is None and q("x") is None


class _Run:
    def __init__(self, pods, top):
        self.pods, self.top = pods, top

    def __call__(self, cmd, **kw):
        class R:
            returncode = 0
        r = R()
        r.stdout = json.dumps(self.pods) if "get" in cmd else self.top
        return r


def test_oom_and_limit_parsing_with_fake_kubectl():
    pods = {"items": [{"metadata": {"namespace": "data", "name": "etl-1"}, "spec": {"containers": [{"name": "main", "resources": {"limits": {"memory": "1Gi"}}}]},
                       "status": {"containerStatuses": [{"name": "main", "lastState": {"terminated": {"reason": "OOMKilled"}}}]}}]}
    top = "data  etl-1  main  250m  900Mi\n"
    ss = monitoring.kubernetes_pods(run=_Run(pods, top))
    m = {(s.metric, s.scope): s for s in ss}
    assert ("oom_killed", "data/etl-1/main") in m
    mem = m[("memory_bytes", "data/etl-1/main")]
    assert mem.value == 900 * 1024**2 and mem.limit == 1024**3
    assert any(f.check == "CLU003" for f in run_checks(Context(samples=ss), only=["CLU003"]))
    # with a namespace, `kubectl top` has no NAMESPACE column
    ss2 = monitoring.kubernetes_pods(run=_Run(pods, "etl-1  main  250m  900Mi\n"), namespace="data")
    assert any(s.metric == "memory_bytes" and s.value == 900 * 1024**2 for s in ss2)


def test_prometheus_vectors_and_limits_with_fake_api():
    def fetch(url, h):
        if "limit" in url or "spec_memory" in url:
            return {"status": "success", "data": {"result": [{"metric": {"pod": "a"}, "value": [0, "1000"]}]}}
        return {"status": "success", "data": {"result": [{"metric": {"pod": "a"}, "value": [0, "900"]}, {"metric": {"pod": "b"}, "value": [0, "NaN"]}]}}
    ss = monitoring.prometheus_samples("http://p", [{"query": "m", "labels": ["pod"], "limit_query": "spec_memory", "source": "prom"}], fetch=fetch)
    assert [(s.scope, s.value, s.limit) for s in ss] == [("a", 900.0, 1000.0)]               # NaN descartado
    with pytest.raises(RuntimeError, match="refused"):
        monitoring.prometheus_samples("http://p", [{"query": "x"}], fetch=lambda u, h: {"status": "error", "error": "bad query"})


@pytest.mark.skipif(not CTX, reason="real Kubernetes not configured")
def test_real_cluster_oom_and_memory_vs_limit():
    ss = monitoring.kubernetes_pods(context=CTX, namespace="itk")
    by = {(s.metric, s.scope): s for s in ss}
    assert ("oom_killed", "itk/oom-pod/eater") in by
    mem = by[("memory_bytes", "itk/idle-pod/sleeper")]
    assert mem.limit == 256 * 1024**2 and 0 < mem.value < mem.limit
    f = run_checks(Context(samples=ss), only=["CLU003"])
    assert any("itk/oom-pod/eater" in x.title for x in f) and f[0].severity == "critical"
    allns = monitoring.kubernetes_pods(context=CTX)                                          # -A: coluna NAMESPACE
    assert ("memory_bytes", "itk/idle-pod/sleeper") in {(s.metric, s.scope) for s in allns}


@pytest.mark.skipif(not PROM, reason="real Prometheus not configured")
def test_real_prometheus_series_with_limit():
    ss = monitoring.prometheus_samples(PROM, [{"query": 'process_resident_memory_bytes{job="self"}', "labels": ["job"],
                                               "limit_query": 'go_memstats_sys_bytes{job="self"}', "metric": "memory_bytes", "source": "prom"}])
    assert len(ss) == 1 and ss[0].scope == "self" and ss[0].value > 1e6 and ss[0].limit and ss[0].limit > 1e6
    node = monitoring.prometheus_samples(PROM)                  # default queries: without cAdvisor/node_exporter → empty, no error
    assert node == []
