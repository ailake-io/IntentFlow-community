import json, threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

from conftest import new_db
import pytest

from intentflow import lineage_api
from intentflow.store import Store
from intentflow.web import make_handler


def test_model_and_column_graph_and_impact(shop_target):
    proj, res = lineage_api.load(str(shop_target / "target"), str(shop_target))
    ms = {m["name"]: m for m in lineage_api.models(proj)}
    assert ms["stg_customers"]["pii"] and ms["stg_customers"]["downstream"] >= 2
    g = lineage_api.model_graph(proj, "customers", depth=1)
    names = {n["name"]: n["level"] for n in g["nodes"]}
    assert names["customers"] == 0 and names["stg_orders"] == -1 and names["customer_contacts"] == 1
    c = lineage_api.column_graph(proj, res, "customers", "email", depth=2)
    ids = {n["id"].split(".", 2)[-1]: n["level"] for n in c["nodes"]}
    assert ids["customers.email"] == 0 and ids["stg_customers.email"] == -1 and ids["customer_contacts.email"] == 1
    assert any(n["id"].endswith("raw_customers.email") and n["level"] == -2 for n in c["nodes"])
    imp = lineage_api.impact(proj, res, "stg_customers", "email")
    assert [r["name"] for r in imp["affected"]] == ["customers", "customer_contacts"]
    assert imp["affected"][0]["distance"] == 1 and imp["affected"][1]["distance"] == 2
    assert imp["owners"] == ["dados@exemplo.com"]                 # dono de `customers`
    with pytest.raises(ValueError):
        lineage_api.column_graph(proj, res, "customers", "nao_existe")


def test_lineage_cache_invalidates_on_new_artifacts(shop_target):
    import os, time
    t = str(shop_target / "target")
    a = lineage_api.load(t, str(shop_target))
    assert lineage_api.load(t, str(shop_target))[0] is a[0]       # cache
    now = time.time() + 5
    os.utime(shop_target / "target/manifest.json", (now, now))
    assert lineage_api.load(t, str(shop_target))[0] is not a[0]    # recarrega


def test_lineage_http_endpoints(shop_target, tmp_path):
    db = new_db(tmp_path, "l.db")
    st = Store(db)
    st.set_setting("dbt.target", str(shop_target / "target"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def get(path):
            try:
                return json.loads(urllib.request.urlopen(base + path, timeout=10).read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        assert any(m["name"] == "orders" for m in get("/api/lineage/models"))
        d = get("/api/lineage/model?model=customers&depth=1")
        assert d["name"] == "customers" and d["graph"]["center"].endswith("customers")
        assert any(c["name"] == "lifetime_value" for c in d["columns"])
        col = get("/api/lineage/column?model=customers&column=lifetime_value&depth=2")
        assert {n["model"] for n in col["graph"]["nodes"]} >= {"customers", "stg_payments"}
        assert col["impact"]["count"] >= 0
        assert "error" in get("/api/lineage/column?model=customers&column=zzz")
        assert "dbt.base_target" in get("/api/lineage/diff")["error"]
        st.set_setting("dbt.base_target", str(shop_target / "target"))
        assert get("/api/lineage/diff")["added"] == []
    finally:
        srv.shutdown()


def test_model_list_hides_installed_packages_unless_asked(shop_target):
    from pathlib import Path
    from intentflow import dbt_loader, lineage_api
    t = Path(shop_target)
    p = dbt_loader.load(t / "target" if (t / "target").exists() else t)
    uid = next(u for u, n in p.nodes.items() if n.resource_type == "model")
    p.root_package = "outro_projeto"                      # tudo vira "pacote de terceiros"
    assert lineage_api.models(p) == []
    assert any(m["id"] == uid for m in lineage_api.models(p, include_packages=True))


def test_lineage_diff_between_versions(shop_target, capsys):
    import dataclasses
    from intentflow import cli
    t = str(shop_target / "target")
    proj, res = lineage_api.load(t, str(shop_target))
    assert lineage_api.diff((proj, res), (proj, res)) == {"added": [], "removed": [], "changed": [], "models_added": [], "models_removed": []}
    gone, flip = res.edges[0], res.edges[1]
    base = dataclasses.replace(res, edges=[dataclasses.replace(flip, kind="other") if e is flip else e for e in res.edges if e is not gone])
    d = lineage_api.diff((proj, base), (proj, res))
    name = lambda u, c: f"{proj.nodes[u].name}.{c}"
    assert d["added"] == [{"src": name(gone.src, gone.src_col), "dst": name(gone.dst, gone.dst_col), "kind": gone.kind}]
    assert d["changed"][0]["from"] == "other" and not d["removed"]
    assert cli.main(["--store", ":memory:", "lineage", "--target", t, "--root", str(shop_target), "--base", t, "--base-root", str(shop_target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == []
    assert cli.main(["--store", ":memory:", "lineage", "--target", t, "--root", str(shop_target), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == len(res.edges)
