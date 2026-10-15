"""Sampling of cluster memory/resources (local and orchestrators) and exhaustion forecast."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from .i18n import tr

UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


@dataclass
class Sample:
    ts: float
    source: str       # host | docker | duckdb | airflow | warehouse
    scope: str        # container/pool/query name
    metric: str       # memory_bytes | oom_killed | queued | spilled_bytes
    value: float
    limit: float | None = None


def parse_size(s: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", s)
    return float(m.group(1)) * UNITS.get(m.group(2).upper(), 1) if m else 0.0


def host_memory(now=None) -> list[Sample]:
    now = now or time.time()
    try:
        kv = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo") if ":" in l}
    except OSError:
        return []
    total, avail = kv.get("MemTotal", 0), kv.get("MemAvailable", 0)
    return [Sample(now, "host", "host", "memory_bytes", float(total - avail), float(total))]


def docker_containers(now=None, run=subprocess.run) -> list[Sample]:
    now = now or time.time()
    out: list[Sample] = []
    try:
        r = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return out
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
            used, _, lim = d["MemUsage"].partition("/")
            out.append(Sample(now, "docker", d["Name"], "memory_bytes", parse_size(used), parse_size(lim) or None))
        except (ValueError, KeyError):
            continue
    try:
        ids = run(["docker", "ps", "-aq"], capture_output=True, text=True, timeout=15).stdout.split()
        if ids:
            r = run(["docker", "inspect", "-f", "{{.Name}} {{.State.OOMKilled}}", *ids], capture_output=True, text=True, timeout=15)
            for line in r.stdout.splitlines():
                name, _, killed = line.strip().partition(" ")
                if killed == "true":
                    out.append(Sample(now, "docker", name.lstrip("/"), "oom_killed", 1.0))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


def airflow_pools(client, now=None) -> list[Sample]:
    now = now or time.time()
    out = []
    for p in client.pools():
        slots = float(p.get("slots", 0) or 0)
        used = float(p.get("running_slots", p.get("occupied_slots", 0)) or 0)
        out.append(Sample(now, "airflow", p.get("name", "pool"), "pool_slots_used", used, slots or None))
        out.append(Sample(now, "airflow", p.get("name", "pool"), "queued", float(p.get("queued_slots", 0) or 0), slots or None))
    return out


def warehouse_spill(wh, hours=24, now=None) -> list[Sample]:
    now = now or time.time()
    return [Sample(now, "warehouse", f"{r['source']}:{r['scope']}", "spilled_bytes", float(r["spilled_bytes"])) for r in wh.spill_report(hours)]


def warehouse_memory_failures(wh, hours=24, now=None) -> list[Sample]:
    now = now or time.time()
    return [Sample(now, "warehouse", f"{r['source']}:{r['scope']}", "oom_killed", 1.0) for r in wh.memory_failures(hours)]


def time_to_exhaustion(points: list[tuple[float, float]], limit: float) -> float | None:
    """Simple linear regression; returns the seconds until `limit` is reached, or None if it is not growing."""
    if len(points) < 4 or limit <= 0:
        return None
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    if den == 0:
        return None
    slope = sum((t - mt) * (v - mv) for t, v in points) / den
    if slope <= 0:
        return None
    last_t, last_v = points[-1]
    return max(0.0, (limit - last_v) / slope)


# ---------------------------------------------------------------- Kubernetes and Prometheus
_K8S = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "k": 1e3, "m": 1e-3}


def k8s_quantity(q) -> float | None:
    """'512Mi' / '1G' / '123456' / '129e6' → bytes."""
    m = re.fullmatch(r"\s*([\d.]+(?:[eE][+-]?\d+)?)\s*([A-Za-z]{0,2})\s*", str(q or ""))
    if not m:
        return None
    return float(m.group(1)) * (_K8S.get(m.group(2), 1) if m.group(2) else 1)


def kubernetes_pods(now=None, run=subprocess.run, context: str | None = None, namespace: str | None = None) -> list[Sample]:
    """Pods through `kubectl`: OOMKilled (the container's last state) and memory use vs limit (`kubectl top`, needs the metrics-server)."""
    now = now or time.time()
    base = ["kubectl"] + (["--context", context] if context else [])
    ns = ["-n", namespace] if namespace else ["-A"]
    out: list[Sample] = []
    try:
        r = run([*base, "get", "pods", *ns, "-o", "json"], capture_output=True, text=True, timeout=60)
        pods = json.loads(r.stdout or "{}").get("items", []) if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return out
    limits: dict[tuple, float | None] = {}
    for p in pods:
        md, spec, st = p.get("metadata", {}), p.get("spec", {}), p.get("status", {})
        for c in spec.get("containers", []):
            lim = ((c.get("resources") or {}).get("limits") or {}).get("memory")
            limits[(md.get("namespace"), md.get("name"), c.get("name"))] = k8s_quantity(lim)
        for cs in st.get("containerStatuses", []) or []:
            scope = f"{md.get('namespace')}/{md.get('name')}/{cs.get('name')}"
            for state in (cs.get("lastState") or {}, cs.get("state") or {}):
                if (state.get("terminated") or {}).get("reason") == "OOMKilled":
                    out.append(Sample(now, "kubernetes", scope, "oom_killed", 1.0))
                    break
    try:
        r = run([*base, "top", "pods", *ns, "--containers", "--no-headers"], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                cols = line.split()
                if len(cols) < (4 if namespace else 5):          # with -n there is no NAMESPACE column
                    continue
                # with -A: NAMESPACE POD NAME CPU MEM ; with -n: POD NAME CPU MEM
                nsn, pod, cont, mem = (cols[0], cols[1], cols[2], cols[4]) if namespace is None else (namespace, cols[0], cols[1], cols[3])
                used = k8s_quantity(mem)
                if used is not None:
                    out.append(Sample(now, "kubernetes", f"{nsn}/{pod}/{cont}", "memory_bytes", used, limits.get((nsn, pod, cont))))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


DEFAULT_PROM_QUERIES = [
    {"query": 'container_memory_working_set_bytes{container!="",container!="POD"}', "labels": ["namespace", "pod", "container"],
     "limit_query": 'container_spec_memory_limit_bytes{container!="",container!="POD"} > 0', "metric": "memory_bytes", "source": "prometheus"},
    {"query": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "labels": ["instance"],
     "limit_query": "node_memory_MemTotal_bytes", "metric": "memory_bytes", "source": "prometheus-node"},
]


def prometheus_samples(url: str, queries=None, now=None, fetch=None) -> list[Sample]:
    """Instant queries (`/api/v1/query`). Each series becomes a sample; `limit_query` matches the series by the same labels."""
    from urllib.parse import quote
    from .orchestrators import http_json
    fetch = fetch or http_json
    now = now or time.time()
    base = url.rstrip("/")

    def vec(q: str) -> list[dict]:
        d = fetch(f"{base}/api/v1/query?query={quote(q)}", {})
        if not isinstance(d, dict) or d.get("status") != "success":
            raise RuntimeError(tr("Prometheus refused the query: {error}", error=str((d or {}).get("error", ""))[:100]))
        return d["data"].get("result", [])
    out: list[Sample] = []
    for spec in queries or DEFAULT_PROM_QUERIES:
        labels = spec.get("labels") or []
        lim = {}
        if spec.get("limit_query"):
            try:
                lim = {tuple(s["metric"].get(k, "") for k in labels): float(s["value"][1]) for s in vec(spec["limit_query"])}
            except (RuntimeError, KeyError, ValueError):
                lim = {}
        for s in vec(spec["query"]):
            key = tuple(s["metric"].get(k, "") for k in labels)
            try:
                v = float(s["value"][1])
            except (KeyError, ValueError, IndexError):
                continue
            if v != v:       # NaN
                continue
            scope = "/".join(x for x in key if x) or s["metric"].get("__name__", "series")
            out.append(Sample(now, spec.get("source", "prometheus"), scope, spec.get("metric", "memory_bytes"), v, lim.get(key)))
    return out
