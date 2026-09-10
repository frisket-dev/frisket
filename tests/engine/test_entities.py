"""Cluster/resolve operations and the Entities sheet.

Unit + black-box coverage for the OpenRefine-style cluster panel backend:
fingerprint clustering groups near-duplicate surface forms, v1 cluster.values
surfaces them as receipt evidence, and typed resolve.entities materialises
canonical entities into the Entities sheet.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ops.cluster_fingerprint import compute_clusters, fingerprint
from frisket.server.app import create_app
from frisket.engine.store import Project
from http_test_helpers import (
    post_cluster_values_as_v1_action,
    post_resolve_entities_as_v1_action,
)


def test_fingerprint_collides_reorderings_and_case():
    assert fingerprint("Jon Smith") == fingerprint("smith, jon")
    assert fingerprint("Jon  Smith") == fingerprint("Jon Smith")
    assert fingerprint("José") == fingerprint("jose")
    assert fingerprint("Jon Smith") != fingerprint("Jane Smith")


def test_compute_clusters_groups_variants(tmp_path):
    p = Project.create(tmp_path / "c.frisket", name="c")
    sid = p.add_sheet("people")
    cid = p.add_column(sid, "name")
    p.add_rows(
        sid,
        [
            {"name": v}
            for v in [
                "Jon Smith",
                "John Smith",
                "Smith, Jon",
                "Acme Corp",
                "Acme  Corp",
            ]
        ],
        {"name": cid},
    )
    clusters = compute_clusters(p, sid, "name")
    keys = {c["key"]: c for c in clusters}
    # 'Jon Smith'/'John Smith' have different fingerprints (jon != john); the
    # 'Jon Smith'/'Smith, Jon' pair must collide into one cluster of 2 rows.
    jon = keys[fingerprint("Jon Smith")]
    assert jon["size"] == 2
    assert {v["value"] for v in jon["values"]} == {"Jon Smith", "Smith, Jon"}
    # singletons are dropped at min_size=2
    assert all(c["size"] >= 2 for c in clusters)
    p.close()


def _client(tmp_path):
    router = ModelRouter(cache=None, cache_mode="off")
    return TestClient(create_app(tmp_path / "ws", router=router))


def _completed_action(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "completed"
    return body


def _cluster_fact(client, project_id, result: dict):
    assert result["action"]["kind"] == "cluster.values"
    assert isinstance(result["run_id"], int)
    # the commit writes one map op (the {input}_canonical column)
    assert len(result["op_ids"]) == 1
    assert result["receipt_id"]
    response = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{result['receipt_id']}"
    )
    assert response.status_code == 200, response.text
    return next(
        item["ref"]
        for item in response.json()["evidence"]
        if item["ref"].get("kind") == "value_clusters"
    )


def _resolve_row_ids(result: dict):
    assert result["action"]["kind"] == "resolve.entities"
    assert len(result["op_ids"]) == 1
    assert [output["kind"] for output in result["outputs"]] == [
        "sheet",
        *["column"] * 5,
        "rows",
    ]
    sheet = result["outputs"][0]
    assert sheet["name"] == "Entities"
    assert set(sheet["ref"]["columns"]) == {
        "entity",
        "key",
        "variants",
        "mentions",
        "source_variants",
    }
    output = next(item for item in result["outputs"] if item["kind"] == "rows")
    assert output["sheet_id"] == sheet["sheet_id"]
    assert output["ref"]["kind"] == "materialized_rows"
    return output["row_ids"]


def test_cluster_then_resolve_creates_entities(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "ent"}).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "c.csv",
                'name\nJon Smith\n"Smith, Jon"\nJon Smith\nJane Doe\n',
                "text/csv",
            )
        },
    ).json()["sheet_id"]

    # cluster
    cluster_result = _completed_action(
        post_cluster_values_as_v1_action(client, pid, sid, "name")
    )
    preview = _cluster_fact(client, pid, cluster_result)
    assert len(preview["clusters"]) == 1
    cluster = preview["clusters"][0]
    assert cluster["size"] == 3  # 3 rows are Jon Smith variants
    assert cluster["canonical"] == "Jon Smith"  # most frequent surface form

    # entities sheet is empty before resolve
    assert client.get(f"/api/projects/{pid}/entities").json() == []

    # resolve all clusters -> Entities sheet
    resolve_result = _completed_action(
        post_resolve_entities_as_v1_action(client, pid, cluster_result["receipt_id"])
    )
    assert len(_resolve_row_ids(resolve_result)) == 1

    ents = client.get(f"/api/projects/{pid}/entities").json()
    assert len(ents) == 1
    ent = ents[0]
    assert ent["entity"] == "Jon Smith"
    assert ent["mentions"] == 3
    assert "Smith, Jon" in ent["variants"]


def test_cluster_by_key_commit_merges_original_forms(tmp_path):
    # end-to-end: a before:" of " cluster key collapses each "President of X"
    # form to its stem so the fingerprint method merges them, while the WRITTEN
    # canonical column carries the original-form canonical and "Vice President"
    # stays its own group (subset-containment was rejected — the key, not raw
    # containment).
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "roles"}).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "r.csv",
                "role\nPresident of Honduras\nPresident of France\nPresident\n"
                "Vice President of Guatemala\nVice President\n",
                "text/csv",
            )
        },
    ).json()["sheet_id"]

    result = _completed_action(
        post_cluster_values_as_v1_action(
            client, pid, sid, "role", key_template='{{value|before:" of "}}'
        )
    )
    preview = _cluster_fact(client, pid, result)
    assert len(preview["clusters"]) == 2  # President family + Vice President family
    assert preview["options"]["key_template"] == '{{value|before:" of "}}'
    families = {
        frozenset(v["value"] for v in c["values"]): c["canonical"]
        for c in preview["clusters"]
    }
    president = frozenset({"President of Honduras", "President of France", "President"})
    vice = frozenset({"Vice President of Guatemala", "Vice President"})
    assert president in families and vice in families
    assert families[president] == "President"  # most-frequent -> shortest stem
    # the preview ref IS the receipt evidence, and the executor guarantees
    # receipt canonicals == the written {col}_canonical column (invariant pinned
    # in test_cluster_values_executor), so a stem canonical here is the written
    # merge — the original President forms all resolve to "President".
    stem_cluster = next(c for c in preview["clusters"] if c["canonical"] == "President")
    assert stem_cluster["size"] == 3  # all three President rows merged


def test_resolve_single_cluster_with_explicit_canonical(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "ent2"}).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("c.csv", "name\nAcme Corp\nAcme  Corp\n", "text/csv")},
    ).json()["sheet_id"]
    key = fingerprint("Acme Corp")
    cluster_result = _completed_action(
        post_cluster_values_as_v1_action(client, pid, sid, "name")
    )
    resolve_result = _completed_action(
        post_resolve_entities_as_v1_action(
            client,
            pid,
            cluster_result["receipt_id"],
            cluster_keys=[key],
            canonical_overrides={key: "Acme Corporation"},
        )
    )
    assert len(_resolve_row_ids(resolve_result)) == 1
    ents = client.get(f"/api/projects/{pid}/entities").json()
    assert len(ents) == 1
    assert ents[0]["entity"] == "Acme Corporation"


def test_cluster_unknown_column_is_400(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "ent3"}).json()["id"]
    sid = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("c.csv", "name\nx\n", "text/csv")},
    ).json()["sheet_id"]
    r = post_cluster_values_as_v1_action(client, pid, sid, "nope")
    assert r.status_code == 400
    body = r.json()
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "invalid_params"
    assert "source column" in body["errors"][0]["details"]["errors"][0]["message"]
