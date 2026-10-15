"""Large synthetic dbt project (manifest+catalog) to measure lineage performance."""
import json, random, sys
from pathlib import Path

def make(n_models: int, out: Path, seed=1):
    rnd = random.Random(seed)
    nodes, cat = {}, {"nodes": {}, "sources": {}}
    def reg(uid, name, rt, cols, code="", deps=()):
        rel = f'"db"."main"."{name}"'
        nodes[uid] = {"unique_id": uid, "name": name, "resource_type": rt, "relation_name": rel, "original_file_path": f"models/{name}.sql",
                      "raw_code": code, "compiled_code": code, "config": {"materialized": "table"}, "meta": {}, "tags": [],
                      "depends_on": {"nodes": list(deps)}, "columns": {}}
        cat["nodes"][uid] = {"columns": {c: {"name": c, "type": "VARCHAR", "index": i} for i, c in enumerate(cols)}}
    srcs = []
    for i in range(20):
        cols = [f"c{j}" for j in range(12)]
        reg(f"seed.p.s{i}", f"s{i}", "seed", cols); srcs.append((f"seed.p.s{i}", f"s{i}", cols))
    layer = srcs
    allm = list(srcs)
    for k in range(n_models):
        a, b = rnd.choice(allm[-400:]), rnd.choice(allm[-400:])
        sel = []
        for j in range(10):
            src, alias = (a, "a") if j % 2 == 0 else (b, "b")
            col = rnd.choice(src[2])
            if j % 5 == 0: sel.append(f"sum({alias}.{col}) as m{j}")
            elif j % 3 == 0: sel.append(f"coalesce({alias}.{col}, 'x') as m{j}")
            else: sel.append(f"{alias}.{col} as m{j}")
        grp = " group by " + ",".join(str(i + 1) for i in range(10) if not sel[i].startswith("sum")) if any(s.startswith("sum") for s in sel) else ""
        q = lambda n: '"db"."main"."%s"' % n
        code = f"with x as (select * from {q(a[1])}), y as (select * from {q(b[1])}) select {', '.join(sel)} from x a join y b on a.{a[2][0]} = b.{b[2][0]}{grp}"
        name = f"m{k}"
        cols = [f"m{j}" for j in range(10)]
        reg(f"model.p.{name}", name, "model", cols, code, deps=[a[0], b[0]])
        allm.append((f"model.p.{name}", name, cols))
    (out / "target").mkdir(parents=True, exist_ok=True)
    (out / "target/manifest.json").write_text(json.dumps({"metadata": {"adapter_type": "duckdb"}, "nodes": nodes, "sources": {}}))
    (out / "target/catalog.json").write_text(json.dumps(cat))

if __name__ == "__main__":
    make(int(sys.argv[1]), Path(sys.argv[2]))
