"""Kafka: consumer lag (Admin API through kafka-python) and Schema Registry (REST, compatible with Confluent/Redpanda).

`collect(store)` records `consumer_lag` samples and a snapshot of the schema versions; the KAF* checks only read the store.
Config: `kafka.bootstrap` (host:port[,..]) and `kafka.schema_registry` (URL). SASL credentials: KAFKA_USER/KAFKA_PASSWORD (SASL_PLAIN).
"""
from __future__ import annotations

import json
import os
import time

from .monitoring import Sample
from .orchestrators import http_json
from .i18n import tr


class SchemaRegistry:
    def __init__(self, url: str, fetch=http_json):
        self.base, self.fetch = url.rstrip("/"), fetch

    def _h(self) -> dict:
        from .orchestrators import basic
        return basic("SCHEMA_REGISTRY_USER", "SCHEMA_REGISTRY_PASSWORD")

    def subjects(self) -> list[str]:
        return self.fetch(f"{self.base}/subjects", self._h()) or []

    def versions(self, subject: str) -> list[int]:
        return self.fetch(f"{self.base}/subjects/{subject}/versions", self._h()) or []

    def version(self, subject: str, v: int | str) -> dict:
        return self.fetch(f"{self.base}/subjects/{subject}/versions/{v}", self._h())

    def snapshot(self, keep: int = 5) -> dict[str, list[dict]]:
        out = {}
        for s in self.subjects():
            vs = sorted(self.versions(s))[-keep:]
            out[s] = [{"version": v, "fields": avro_fields(self.version(s, v).get("schema")),
                       "type": self.version(s, v).get("schemaType", "AVRO")} for v in vs]
        return out


def avro_fields(schema) -> dict[str, str]:
    """{field: type} of a top-level Avro schema (record). Other formats → {}."""
    try:
        doc = json.loads(schema) if isinstance(schema, str) else schema
    except (TypeError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("type") != "record":
        return {}
    out = {}
    for f in doc.get("fields", []):
        t = f.get("type")
        if isinstance(t, list):                                  # union: ["null","string"]
            t = "|".join(sorted(str(x if not isinstance(x, dict) else x.get("type", "complex")) for x in t))
        elif isinstance(t, dict):
            t = t.get("logicalType") or t.get("type", "complex")
        out[f["name"]] = str(t)
    return out


def consumer_lag(bootstrap: str, groups: list[str] | None = None) -> list[tuple[str, str, int]]:
    """[(group, topic, lag)] summing the partitions. Requires `kafka-python-ng`."""
    try:
        from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition
    except ImportError as e:
        raise RuntimeError(tr("install `kafka-python-ng` (pip install intentflow[kafka]) to measure lag")) from e
    cfg: dict = {"bootstrap_servers": bootstrap.split(","), "request_timeout_ms": 15000}
    if os.environ.get("KAFKA_USER"):
        cfg.update(security_protocol="SASL_PLAINTEXT", sasl_mechanism="PLAIN", sasl_plain_username=os.environ["KAFKA_USER"],
                   sasl_plain_password=os.environ.get("KAFKA_PASSWORD", ""))
    admin = KafkaAdminClient(**cfg)
    try:
        names = groups or [g[0] for g in admin.list_consumer_groups()]
        out = []
        consumer = KafkaConsumer(**cfg)
        try:
            for g in names:
                offs = admin.list_consumer_group_offsets(g)
                if not offs:
                    continue
                ends = consumer.end_offsets(list(offs))
                per_topic: dict[str, int] = {}
                for tp, om in offs.items():
                    per_topic[tp.topic] = per_topic.get(tp.topic, 0) + max(0, ends[tp] - om.offset)
                out += [(g, t, lag) for t, lag in per_topic.items()]
        finally:
            consumer.close()
        return out
    finally:
        admin.close()


def collect(store, now: float | None = None, lag_fn=consumer_lag, registry=SchemaRegistry) -> dict:
    now = now or time.time()
    res = {"lag": 0, "subjects": 0, "errors": []}
    boot, reg = store.get_setting("kafka.bootstrap"), store.get_setting("kafka.schema_registry")
    if boot:
        try:
            groups = store.get_setting("kafka.groups") or None
            if isinstance(groups, str):                  # coming from the UI as text "g1, g2"
                groups = [g.strip() for g in groups.split(",") if g.strip()] or None
            rows = lag_fn(boot, groups)
            store.add_samples([Sample(now, "kafka", f"{g}:{t}", "consumer_lag", float(lag)) for g, t, lag in rows])
            res["lag"] = len(rows)
        except Exception as e:                       # a broker that is down does not take the cycle down
            res["errors"].append(f"kafka: {str(e)[:150]}")
    if reg:
        try:
            snap = registry(reg).snapshot()
            store.set_setting("state.kafka_schemas", snap)
            res["subjects"] = len(snap)
        except Exception as e:
            res["errors"].append(f"schema registry: {str(e)[:150]}")
    return res
