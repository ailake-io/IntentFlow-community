"""Real Kafka + Schema Registry (Redpanda in a container: INTENTFLOW_KAFKA_BOOTSTRAP and INTENTFLOW_SCHEMA_REGISTRY) and simulated versions."""
import json, os, time
import urllib.request

import pytest

from intentflow import sqlmesh_loader, streaming
from intentflow.checks import Context, run_checks
from intentflow.monitoring import Sample
from intentflow.store import Store

BOOT = os.environ.get("INTENTFLOW_KAFKA_BOOTSTRAP")
REG = os.environ.get("INTENTFLOW_SCHEMA_REGISTRY")
V1 = {"type": "record", "name": "Order", "fields": [{"name": "order_id", "type": "long"}, {"name": "amount", "type": "double"}, {"name": "note", "type": ["null", "string"]}]}
V2 = {"type": "record", "name": "Order", "fields": [{"name": "order_id", "type": "string"}, {"name": "note", "type": ["null", "string"]}]}


def test_avro_fields_handles_unions_and_logical_types():
    s = {"type": "record", "name": "x", "fields": [{"name": "a", "type": "long"}, {"name": "b", "type": ["null", "string"]},
                                                  {"name": "c", "type": {"type": "long", "logicalType": "timestamp-millis"}}]}
    assert streaming.avro_fields(json.dumps(s)) == {"a": "long", "b": "null|string", "c": "timestamp-millis"}
    assert streaming.avro_fields("não é json") == {} and streaming.avro_fields({"type": "string"}) == {}


def test_lag_check_needs_high_and_rising_values():
    now = time.time()
    s = [Sample(now - 120, "kafka", "g:orders", "consumer_lag", 20000), Sample(now - 60, "kafka", "g:orders", "consumer_lag", 50000),
         Sample(now - 120, "kafka", "g:ok", "consumer_lag", 20000), Sample(now - 60, "kafka", "g:ok", "consumer_lag", 100),
         Sample(now, "kafka", "g:small", "consumer_lag", 5)]
    f = run_checks(Context(samples=s), only=["KAF001"])
    assert [x.title.split(":")[0] + ":" + x.title.split(":")[1].split()[0] for x in f] == ["g:orders"] and f[0].severity == "medium"
    assert run_checks(Context(samples=s, policies={"kafka_lag_max": 1000}), only=["KAF001"])[0].evidence["limit"] == 1000


def test_schema_break_is_critical_when_topic_maps_to_a_model(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("state.kafka_schemas", {"fct_orders-value": [
        {"version": 1, "fields": {"order_id": "long", "amount": "double"}}, {"version": 2, "fields": {"order_id": "string"}}]})
    p = sqlmesh_loader.from_export(json.loads((__import__("pathlib").Path(__file__).parent / "fixtures/sqlmesh_shop_export.json").read_text()))
    f = run_checks(Context(project=p, store=st), only=["KAF002"])
    assert len(f) == 1 and f[0].severity == "critical" and f[0].evidence["removed"] == ["amount"] and f[0].evidence["retyped"] == ["order_id"]
    assert f[0].nodes == ["model.sqlmesh.fct_orders"]


@pytest.mark.skipif(not (BOOT and REG), reason="real Kafka/Schema Registry not configured")
def test_real_redpanda_lag_and_schema_versions(tmp_path):
    from kafka import KafkaConsumer, KafkaProducer
    topic, group = f"orders_{int(time.time())}", f"etl-loader-{int(time.time())}"
    prod = KafkaProducer(bootstrap_servers=BOOT.split(","))
    for i in range(50):
        prod.send(topic, f"m{i}".encode())
    prod.flush()
    c = KafkaConsumer(topic, bootstrap_servers=BOOT.split(","), group_id=group, auto_offset_reset="earliest", enable_auto_commit=False, consumer_timeout_ms=8000)
    n = 0
    for _ in c:
        n += 1
        if n == 10:
            break
    c.commit()
    c.close()
    for v in (V1, V2):
        req = urllib.request.Request(f"{REG}/subjects/{topic}-value/versions", method="POST", data=json.dumps({"schema": json.dumps(v)}).encode(),
                                     headers={"Content-Type": "application/vnd.schemaregistry.v1+json"})
        # v2 is incompatible: the registry may refuse it; turn compatibility off to register the change a producer would make
        if v is V2:
            urllib.request.urlopen(urllib.request.Request(f"{REG}/config/{topic}-value", method="PUT", data=b'{"compatibility":"NONE"}',
                                                          headers={"Content-Type": "application/vnd.schemaregistry.v1+json"})).read()
        urllib.request.urlopen(req).read()
    st = Store(str(tmp_path / "s.db"))
    st.set_setting("kafka.bootstrap", BOOT)
    st.set_setting("kafka.schema_registry", REG)
    st.set_setting("kafka.groups", [group])
    res = streaming.collect(st)
    assert res["errors"] == [] and res["lag"] == 1 and res["subjects"] >= 1
    lag = next(s for s in st.recent_samples(0) if s.metric == "consumer_lag")
    assert lag.scope == f"{group}:{topic}" and lag.value == 40
    f = run_checks(Context(store=st, samples=st.recent_samples(0), policies={"kafka_lag_max": 30}), only=["KAF001", "KAF002"])
    kinds = {x.check for x in f}
    assert kinds == {"KAF001", "KAF002"}
    k2 = next(x for x in f if x.check == "KAF002")
    assert k2.evidence["removed"] == ["amount"] and k2.evidence["retyped"] == ["order_id"]
