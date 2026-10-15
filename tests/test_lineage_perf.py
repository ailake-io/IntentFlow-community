"""Performance/correctness regression of the lineage cache on a synthetic project."""
import time
from pathlib import Path

from intentflow import dbt_loader, lineage
from intentflow.store import Store

import synth_project  # tests/synth_project.py


def test_cache_is_exact_and_much_faster(tmp_path):
    synth_project.make(60, tmp_path)
    p = dbt_loader.load(tmp_path / "target")
    t = time.time()
    plain = lineage.build(p)
    cold = time.time() - t
    st = Store()
    lineage.build(p, cache=st)
    st.flush()
    t = time.time()
    warm_res = lineage.build(p, cache=st)
    warm = time.time() - t
    assert plain.precision_report["resolved_ratio"] == 1.0 and plain.columns_total == 600
    assert set(warm_res.edges) == set(plain.edges)           # the cache does not change the result
    assert warm_res.cache_hits == 60
    assert warm < cold / 5
    assert cold / plain.columns_total < 0.02                   # < 20 ms/coluna (medido ~2,4 ms)


def test_cache_invalidates_when_sql_changes(tmp_path):
    synth_project.make(10, tmp_path)
    st = Store()
    p = dbt_loader.load(tmp_path / "target")
    lineage.build(p, cache=st)
    uid = next(u for u, n in p.nodes.items() if n.resource_type == "model")
    p.nodes[uid].compiled_code = p.nodes[uid].compiled_code + " "      # SQL mudou
    r = lineage.build(p, cache=st)
    assert r.cache_hits == 9
