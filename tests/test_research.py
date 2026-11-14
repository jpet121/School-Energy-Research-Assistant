import json
import sqlite3
import time

import pytest

from retrofit.contracts import fingerprint
from test_admission import HEADERS, local_client, make_pdf
from test_corpus import approve_and_prepare, publish


def search(client, question, *, mode="lexical", **options):
    created = client.post("/queries", json={"schema_version": "1.1", "question": question, "mode": mode, **options}, headers=HEADERS)
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    for _ in range(160):
        response = client.get(f"/queries/{run_id}", headers=HEADERS)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] != "pending":
            return result
        time.sleep(0.04)
    raise AssertionError("The local query did not complete")


def test_lexical_query_opens_the_exact_row_and_required_note(tmp_path):
    with local_client(tmp_path) as client:
        manifest, _ = approve_and_prepare(client, make_pdf(
            "22.2% and 21.3%", "Miami high school", "EUI and cost reduction", "Percent", "Excludes EBCx",
        ), row=True)
        publish(client, manifest["snapshot_id"])
        run = search(client, "22.2")
        assert run["status"] == "result" and run["mode_used"] == "lexical"
        assert run["snapshot_id"] == manifest["snapshot_id"]
        bundle_id = run["final_bundle_ids"][0]
        path = f"/runs/{run['run_id']}/evidence/{bundle_id}"
        evidence = client.get(path, headers=HEADERS).json()
        assert evidence["document"]["publication_date"] == "2025-01-01"
        assert evidence["spans"][0]["pdf_page_1based"] == 1
        note = evidence["mandatory_qualifier_span_ids"][-1]
        assert next(span["normalized_text"] for span in evidence["spans"] if span["span_id"] == note) == "Excludes EBCx"
        assert client.get(f"{path}/pages/{note}", headers=HEADERS).headers["content-type"] == "image/png"
        assert search(client, "unfindablefixtureterm")["result_kind"] == "no_evidence"
        assert client.post("/queries", json={
            "schema_version": "1.1", "question": "x" * 2001, "mode": "lexical",
        }, headers=HEADERS).status_code == 400


def test_query_denials_filters_and_empty_inputs_preserve_the_published_snapshot(tmp_path):
    with local_client(tmp_path) as client:
        request = {"schema_version": "1.1", "question": "lighting", "mode": "lexical"}
        assert client.post("/queries", json=request, headers=HEADERS).json()["code"] == "no_published_snapshot"
        manifest, _ = approve_and_prepare(client, make_pdf(*(f"Lighting evidence number {n}." for n in range(12))))
        snapshot = manifest["snapshot_id"]
        version = manifest["documents"][0]["version_id"]
        assert client.post("/queries", json={**request, "snapshot_id": snapshot}, headers=HEADERS).json()["code"] == "snapshot_unavailable"
        publish(client, snapshot)
        for changes, code in (
            ({"snapshot_id": "absent"}, "snapshot_unavailable"),
            ({"version_ids": ["another-edition"]}, "invalid_filter"),
            ({"version_ids": [version, version]}, "invalid_filter"),
        ):
            assert client.post("/queries", json={**request, **changes}, headers=HEADERS).json()["code"] == code
        assert search(client, "?! ___")["result_kind"] == "no_evidence"
        dense = search(client, "lighting", mode="dense")
        assert dense["status"] == "error" and dense["failure_code"] == "embedding_unavailable"
        run = search(client, "lighting", snapshot_id=snapshot, version_ids=[version])
        assert len(run["ranked_candidates"]) == 12 and len(run["bundles"]) == 8
        assert all(bundle["version_id"] == version for bundle in run["bundles"])
        assert client.get("/queries/unknown", headers=HEADERS).json()["code"] == "missing_run"
        assert client.post("/queries/unknown/cancel", json={}, headers=HEADERS).json()["code"] == "missing_run"
        assert client.post(f"/queries/{run['run_id']}/cancel", json={}, headers=HEADERS).json()["state"] == "already_completed"
        assert client.get(f"/runs/{run['run_id']}/evidence/absent", headers=HEADERS).json()["code"] == "missing_bundle"
        bundle = run["final_bundle_ids"][0]
        denied = client.get(f"/runs/{run['run_id']}/evidence/{bundle}/pages/absent", headers=HEADERS)
        assert (denied.status_code, denied.json()["code"]) == (404, "missing_span")
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] == snapshot


def test_incomplete_persisted_table_bundle_cannot_reach_answer_context(tmp_path):
    with local_client(tmp_path) as client:
        manifest, _ = approve_and_prepare(client, make_pdf(
            "Table value 22.2 percent", "Miami school", "Modeled savings", "Percent", "Excludes EBCx",
        ), row=True)
        snapshot = manifest["snapshot_id"]
        publish(client, snapshot)
        first = search(client, "22.2")
        bundle = next(bundle for bundle in first["bundles"] if bundle["table_row"])
        chunk_id = bundle["anchor_chunk_id"]
        missing = bundle["table_row"]["mandatory_note_span_ids"][0]
        db_path = tmp_path / "evidence.sqlite3"
        with sqlite3.connect(db_path) as db:
            record = db.execute("SELECT payload FROM snapshot_chunks WHERE snapshot_id=? AND chunk_id=?",
                                (snapshot, chunk_id)).fetchone()
            chunk = json.loads(record[0])
        for damaged in (
            {**chunk, "spans": [span for span in chunk["spans"] if span["span_id"] != missing]},
            {**chunk, "rendered_context": "Altered context under the original identity."},
            {**chunk, "spans": None},
        ):
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE snapshot_chunks SET payload=? WHERE snapshot_id=? AND chunk_id=?",
                           (json.dumps(damaged), snapshot, chunk_id))
            result = search(client, "22.2")
            assert result["status"] == "result" and result["result_kind"] == "no_evidence"
            assert result["incomplete_coverage"] is True and result["bundles"] == []
            saved = client.get(f"/queries/{first['run_id']}", headers=HEADERS).raise_for_status().json()
            assert saved["incomplete_coverage"] and saved["bundles"] == []
            rejected = client.get(f"/snapshots/{snapshot}/evaluation-inventory", headers=HEADERS)
            assert (rejected.status_code, rejected.json().get("code")) == (409, "incomplete_snapshot")
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE snapshot_chunks SET chunk_id=?,payload=? WHERE snapshot_id=? AND chunk_id=?",
                       ("replacement-chunk", json.dumps(chunk), snapshot, chunk_id))
        rejected = search(client, "22.2")
        assert (rejected["status"], rejected["failure_code"], rejected["bundles"]) == ("error", "incomplete_snapshot", [])
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] == snapshot


def test_saved_query_rejects_detached_references_and_missing_snapshot(tmp_path):
    with local_client(tmp_path) as client:
        manifest, _ = approve_and_prepare(client, make_pdf("Lighting study retained in a frozen snapshot."))
        snapshot = manifest["snapshot_id"]
        publish(client, snapshot)
        run = search(client, "lighting")
        path = f"/queries/{run['run_id']}"
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            raw, = db.execute("SELECT payload FROM runs WHERE id=?", (run["run_id"],)).fetchone()
            saved = json.loads(raw)
        detached = {**saved["bundles"][0], "anchor_chunk_id": "missing-chunk"}
        for bundle_list in ([detached], None, []):
            with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
                db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps({**saved, "bundles": bundle_list}), run["run_id"]))
            rejected = client.get(path, headers=HEADERS).raise_for_status().json()
            assert rejected["bundles"] == rejected["final_bundle_ids"] == []
            assert rejected["incomplete_coverage"]
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE runs SET payload=? WHERE id=?", (raw, run["run_id"]))
            db.execute("DELETE FROM snapshots WHERE id=?", (snapshot,))
        unavailable = client.get(path, headers=HEADERS)
        assert (unavailable.status_code, unavailable.json()["code"]) == (409, "snapshot_unavailable")


@pytest.mark.parametrize(("format_name", "expected"), [
    ("legacy", "evidence"), ("legacy-broken-context", "no_evidence"), ("unknown", "no_evidence"),
])
def test_saved_collection_enforces_snapshot_identity_format_and_context(tmp_path, format_name, expected):
    with local_client(tmp_path) as client:
        base, _ = approve_and_prepare(client, make_pdf("Retained lighting evidence in the snapshot."))
        baseline = base["snapshot_id"]
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            version, payload = db.execute(
                "SELECT version_id,payload FROM snapshot_chunks WHERE snapshot_id=?", (baseline,),
            ).fetchone()
            manifest, = db.execute("SELECT manifest FROM snapshots WHERE id=?", (baseline,)).fetchone()
            manifest = json.loads(manifest)
            manifest.pop("snapshot_id")
            chunk = json.loads(payload)
            if format_name.startswith("legacy"):
                manifest.pop("chunk_payload_identity")
                chunk["chunk_id"] = fingerprint([
                    version, chunk["source_span_ids"], chunk["table_row"], chunk["chunk_config_hash"], chunk["normalized_text"],
                ])
                if format_name == "legacy-broken-context":
                    chunk["rendered_context"] = "Unexpected replacement."
            else:
                manifest["chunk_payload_identity"] = "unknown-format"
            manifest["chunk_set_hash"] = fingerprint([chunk["chunk_id"]])
            snapshot = fingerprint(manifest)
            db.execute("INSERT INTO snapshots VALUES (?,?,?)", (
                snapshot, json.dumps({**manifest, "snapshot_id": snapshot}), "2026-01-01",
            ))
            db.execute("INSERT INTO snapshot_chunks VALUES (?,?,?,?)", (snapshot, chunk["chunk_id"], version, json.dumps(chunk)))
            db.execute(f"CREATE VIRTUAL TABLE fts_{snapshot} USING fts5(chunk_id UNINDEXED,body)")
            db.execute(f"INSERT INTO fts_{snapshot} VALUES (?,?)", (chunk["chunk_id"], chunk["normalized_text"]))
        found = search(client, "lighting", snapshot_id=snapshot)
        assert found["result_kind"] == expected
        assert found["incomplete_coverage"] == (expected == "no_evidence")
        if expected == "evidence":
            assert found["bundles"][0]["rendered_context"] == "Retained lighting evidence in the snapshot."
