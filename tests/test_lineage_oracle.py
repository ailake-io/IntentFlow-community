"""The dynamic oracle over the sample project: it measures lineage and MUST detect a sabotaged lineage (otherwise it is no measure)."""
import copy
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from intentflow import dbt_loader, lineage, lineage_oracle  # noqa: E402


def _db(root: Path) -> str:
    found = sorted(root.rglob("*.duckdb"))
    assert found, "o dbt deveria ter criado o arquivo DuckDB do projeto"
    return str(found[0])


def test_oracle_measures_clean_lineage_and_catches_a_sabotaged_one(shop_target):
    root = Path(shop_target)
    proj = dbt_loader.load(root / "target" if (root / "target").exists() else root)
    res = lineage.build(proj)
    rep = lineage_oracle.run(proj, res, _db(root), package="shop")
    assert rep["models"] >= 5 and rep["source_columns_tested"] >= 5
    assert rep["direct"]["lost"] == 0 and rep["direct"]["precision_lower_bound"] >= 0.9 and rep["direct"]["tp"] > 5
    assert rep["model_level"]["fn"] == 0
    # sabotage: remove every direct edge leaving a source column → the oracle must report losses
    bad = copy.copy(res)
    victim = next(e for e in res.edges if proj.nodes[e.src].resource_type in ("seed", "source"))
    bad.edges = [e for e in res.edges if (e.src, e.src_col) != (victim.src, victim.src_col)]
    bad.indirect = [x for x in res.indirect if (x[0], x[1]) != (victim.src, victim.src_col)]
    rep2 = lineage_oracle.run(proj, bad, _db(root), package="shop")
    assert rep2["direct"]["lost"] + rep2["model_level"]["fn"] > 0


def test_static_closure_follows_direct_edges_then_expands_indirect_to_all_model_columns():
    from intentflow.model import Edge
    edges = [Edge("a", "x", "b", "y", "direct", False), Edge("c", "k", "d", "z", "direct", False)]
    ind = [("b", "y", "m", "join")]
    got = lineage_oracle.static_closure(edges, ("a", "x"), ind, {"m": ["p", "q"], "d": ["z"]})
    assert got == {("b", "y"), ("m", "p"), ("m", "q")}
    assert lineage_oracle.static_closure(edges, ("a", "x"), ind, {"m": ["p"]}, include_indirect=False) == {("b", "y")}


def test_sample_rows_shrinks_sources_in_the_copy_only_and_keeps_joins(shop_target):
    root = Path(shop_target)
    proj = dbt_loader.load(root / "target" if (root / "target").exists() else root)
    res = lineage.build(proj)
    db = _db(root)
    rows = lambda: duckdb.connect(db, read_only=True).execute("select count(*) from raw_orders").fetchone()[0]
    before = rows()
    rep = lineage_oracle.run(proj, res, db, package="shop", sample_rows=2)
    assert rep["sampled_sources"], "alguma origem tinha mais de 2 linhas e deveria ser amostrada"
    assert all(after < n for n, after in rep["sampled_sources"].values())
    assert rep["model_level"]["fn"] == 0                       # a sample must not invent impact false negatives
    assert rows() == before
