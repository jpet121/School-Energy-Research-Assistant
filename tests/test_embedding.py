import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess

import numpy as np
import onnxruntime as ort
from onnxruntime.datasets import get_example
import pytest

from retrofit.contracts import ApiError, fingerprint
from retrofit.embeddings import Embeddings, infer_embedding
from pdf_fixture import pdf_with_pages, text_stream
from processes import python_launcher
from test_admission import HEADERS, MANIFEST, local_client, make_pdf
from test_corpus import approve_and_prepare, publish
from test_extraction import extract_pdf
from test_research import search


@pytest.fixture
def models():
    directory = os.environ.get("RETROFIT_TEST_MODELS")
    if not directory:
        pytest.skip("Set RETROFIT_TEST_MODELS to run the real pinned model checks.")
    return Path(directory)


def test_fixed_and_structure_chunks_are_reproducible_and_token_bounded(tmp_path, models):
    with local_client(tmp_path, models=models) as client:
        line = "School HVAC setback and cooling targets depend on schedules, filters, occupancy, and weather."
        _, job = approve_and_prepare(client, make_pdf(*([line] * 40)))
        base = {"schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small", "include_vectors": False}
        fixed = client.post("/snapshots", json={**base, "chunking": "fixed"}, headers=HEADERS)
        assert fixed.status_code == 201, fixed.text
        manifest = fixed.json()
        assert manifest["chunking"]["strategy"] == "fixed"
        assert 0 < manifest["chunking"]["maximum_input_tokens"] <= 480
        assert client.post("/snapshots", json={**base, "chunking": "fixed"}, headers=HEADERS).json()["snapshot_id"] == manifest["snapshot_id"]
        structured = client.post("/snapshots", json={**base, "chunking": "structure"}, headers=HEADERS).json()
        assert structured["chunk_set_hash"] != manifest["chunk_set_hash"]


def test_pinned_bge_models_enable_dense_and_hybrid_without_breaking_lexical(tmp_path, models):
    with local_client(tmp_path, models=models) as client:
        _, job = approve_and_prepare(client, make_pdf("Lighting schedule reduces after hours energy.", "HVAC temperatures reset overnight."))
        for name, dimension in (("bge-small", 384), ("bge-base", 768)):
            prepared = client.post("/snapshots", json={
                "schema_version": "1.1", "job_ids": [job], "embedding_model": name,
                "chunking": "structure", "include_vectors": True,
            }, headers=HEADERS)
            assert prepared.status_code == 201, prepared.text
            manifest = prepared.json()
            assert manifest["embedding"]["dimension"] == dimension
            publish(client, manifest["snapshot_id"])
            for mode in ("dense", "hybrid", "lexical"):
                answer = search(client, "lighting schedule", mode=mode)
                assert answer["status"] == "result", answer
                assert answer["mode_used"] == mode and answer["bundles"]
            oversize = search(client, "x " * 550, mode="dense")
            assert oversize["failure_code"] == "embedding_input_too_long"
            assert search(client, "lighting schedule", mode="lexical")["status"] == "result"
        edition = manifest["documents"][0]["version_id"]
        client.post(f"/editions/{edition}/withdraw", headers=HEADERS, json={
            "schema_version": "1.1", "reason": "Retire the only indexed edition.",
        }).raise_for_status()
        assert search(client, "lighting schedule", mode="dense")["result_kind"] == "no_evidence"


def test_pinned_embedding_accepts_480_content_tokens_with_specials(models):
    local = Embeddings(models)
    text = "x " * 480
    assert len(local.tokenizer("bge-small").encode(text).ids) == 482
    assert len(local.encode("bge-small", text)) == 384 * 4
    with pytest.raises(ApiError, match="embedding_input_too_long"):
        local.encode("bge-small", "x " * 481)


def test_vector_corruption_and_model_availability_leave_lexical_search_usable(tmp_path, models):
    private_assets = tmp_path / "models"
    installed = private_assets / "bge-small-en-v1.5"
    installed.mkdir(parents=True)
    source = models / installed.name
    with local_client(tmp_path / "app", models=private_assets) as client:
        lexical, job = approve_and_prepare(client, make_pdf("Keep lexical lighting evidence available."))
        publish(client, lexical["snapshot_id"])
        assert client.get("/embedding-models", headers=HEADERS).json() == {"models": []}
        request = {"schema_version": "1.1", "job_ids": [job], "chunking": "fixed",
                   "embedding_model": "bge-small", "include_vectors": True}
        absent = client.post("/snapshots", json=request, headers=HEADERS)
        assert (absent.status_code, absent.json()["code"]) == (503, "embedding_unavailable")
        (installed / "tokenizer.json").write_text("Corrupt tokenizer fixture")
        wrong = client.post("/snapshots", json=request, headers=HEADERS)
        assert (wrong.status_code, wrong.json()["code"]) == (409, "embedding_config_mismatch")
        (installed / "tokenizer.json").unlink()
        for name in ("tokenizer.json", "model.onnx"):
            (installed / name).symlink_to((source / name).resolve())
        assert client.get("/embedding-models", headers=HEADERS).json() == {"models": ["bge-small"]}
        vector = client.post("/snapshots", json=request, headers=HEADERS).raise_for_status().json()
        snapshot = vector["snapshot_id"]
        publish(client, snapshot)
        edition = vector["documents"][0]["version_id"]
        assert search(client, "lighting", mode="dense", version_ids=[edition])["result_kind"] == "evidence"
        obsolete = {key: value for key, value in vector.items() if key not in {"snapshot_id", "published_at"}}
        obsolete["embedding"] = {**obsolete["embedding"], "runtime": "older-runtime"}
        obsolete_id = fingerprint(obsolete)
        with sqlite3.connect(tmp_path / "app" / "evidence.sqlite3") as db:
            db.execute("INSERT INTO snapshots VALUES (?,?,?)", (
                obsolete_id, json.dumps({**obsolete, "snapshot_id": obsolete_id}), "2026-01-01",
            ))
        denied = search(client, "lighting", mode="dense", snapshot_id=obsolete_id)
        assert (denied["status"], denied["failure_code"]) == ("error", "embedding_config_mismatch")
        for invalid in (b"wrong length", bytes(384 * 4), struct.pack("<384f", 2.0, *([0.0] * 383))):
            with sqlite3.connect(tmp_path / "app" / "evidence.sqlite3") as db:
                db.execute("UPDATE snapshot_vectors SET vector=? WHERE snapshot_id=?", (invalid, snapshot))
            assert search(client, "lighting", mode="dense")["failure_code"] == "embedding_config_mismatch"
            assert search(client, "lighting")["result_kind"] == "evidence"
            rejected = client.post(f"/snapshots/{snapshot}/restore", headers=HEADERS, json={})
            assert (rejected.status_code, rejected.json()["code"]) == (409, "embedding_config_mismatch")


def test_chunking_shrinks_windows_and_refuses_an_over_budget_metadata_prefix(tmp_path, models):
    with local_client(tmp_path, models=models) as client:
        lines = ["Lighting operating schedules depend on occupancy and building hours."] * 35
        for count, key in ((125, "fits-after-shrinking"), (249, "too-large")):
            manifest = {**MANIFEST, "title": "a " * count, "scope": "b " * count,
                        "source_url": f"https://example.org/{key}.pdf"}
            job, record = extract_pdf(client, make_pdf(*lines), manifest=manifest, key=key)
            extraction = record["extraction"]
            client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
                "schema_version": "1.1", "extraction_config_hash": extraction["config_hash"],
                "approved_span_ids": [span["span_id"] for span in extraction["pages"][0]["spans"]],
                "qa_note": "Verified fixture.",
            }).raise_for_status()
            response = client.post("/snapshots", headers=HEADERS, json={
                "schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small", "chunking": "fixed",
            })
            if count == 125:
                data = response.raise_for_status().json()
                assert data["chunk_count"] > 1 and data["chunking"]["maximum_input_tokens"] <= 482
            else:
                assert (response.status_code, response.json()["code"]) == (400, "chunk_prefix_too_large")


def test_real_onnx_startup_failure_is_reported_at_the_model_boundary(models):
    script = Path(__file__).with_name("embedding_kernel_probe.py")
    result = subprocess.run([*python_launcher(), str(script), str(models.resolve())],
                            text=True, capture_output=True, timeout=25)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "embedding_unavailable"


def test_real_onnx_inference_rejects_an_incompatible_model_input():
    session = ort.InferenceSession(get_example("sigmoid.onnx"), providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name
    with pytest.raises(ApiError, match="embedding_failed") as failed:
        infer_embedding(session, {name: np.ones((1, 3), dtype=np.int64)}, 3)
    assert "Unexpected input data type" in str(failed.value.__cause__)


def test_unconfigured_embedding_service_preserves_lexical_search(tmp_path):
    with local_client(tmp_path) as client:
        baseline, job = approve_and_prepare(client, make_pdf("Reviewable lighting evidence."))
        publish(client, baseline["snapshot_id"])
        assert client.get("/embedding-models", headers=HEADERS).raise_for_status().json() == {"models": []}
        candidate = client.post("/snapshots", headers=HEADERS, json={
            "schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small", "chunking": "fixed",
        })
        assert (candidate.status_code, candidate.json()["code"]) == (503, "embedding_unavailable")
        assert search(client, "lighting")["result_kind"] == "evidence"


def test_overlapping_model_windows_do_not_duplicate_identical_source_evidence(tmp_path, models):
    pdf = pdf_with_pages("BT /F1 12 Tf 50 700 Td (" + "lighting " * 420 + ") Tj ET", width=24_000)
    with local_client(tmp_path, models=models) as client:
        _, job = approve_and_prepare(client, pdf)
        prepared = client.post("/snapshots", headers=HEADERS, json={
            "schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small", "chunking": "fixed",
        }).raise_for_status().json()
        assert prepared["chunk_count"] == 2
        publish(client, prepared["snapshot_id"])
        found = search(client, "lighting")
        assert len(found["ranked_candidates"]) == 2 and len(found["bundles"]) == 1
        assert len(found["bundles"][0]["ordered_span_ids"]) == 1


def test_table_bundle_rejects_complete_overlength_prefix_without_stripping_context(tmp_path, models):
    with local_client(tmp_path, models=models) as client:
        pdf = make_pdf("22.2 percent", "Miami school", "Annual savings", "Percent", "Modeled savings")
        manifest = {**MANIFEST, "title": "a " * 249, "scope": "b " * 249}
        job, record = extract_pdf(client, pdf, manifest=manifest)
        extraction = record["extraction"]
        ids = [span["span_id"] for span in extraction["pages"][0]["spans"]]
        client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
            "schema_version": "1.1", "extraction_config_hash": extraction["config_hash"], "approved_span_ids": ids,
            "qa_note": "Verified each table qualifier.",
            "table_rows": [dict(zip(
                ("cell_span_ids", "row_header_span_ids", "column_header_span_ids", "unit_span_ids", "mandatory_note_span_ids"),
                ([span_id] for span_id in ids), strict=True,
            ))],
        }).raise_for_status()
        rejected = client.post("/snapshots", headers=HEADERS, json={
            "schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small", "chunking": "fixed",
        })
        assert (rejected.status_code, rejected.json()["code"]) == (400, "chunk_token_limit")
        assert client.get("/snapshots", headers=HEADERS).json()["snapshots"] == []


def test_table_only_index_uses_verified_cells_when_there_are_no_body_windows(tmp_path, models):
    grid = (
        "50 580 300 120 re S\n50 640 m 350 640 l S\n150 580 m 150 700 l S\n250 580 m 250 700 l S\n"
        + text_stream("School", x=60, y=668) + text_stream("Savings", x=160, y=668) + text_stream("Annual", x=260, y=668)
        + text_stream("Miami", x=60, y=610) + text_stream("22.2%", x=160, y=610) + text_stream("Modeled", x=260, y=610)
    )
    with local_client(tmp_path, models=models) as client:
        job, record = extract_pdf(client, pdf_with_pages(grid))
        extraction = record["extraction"]
        spans = {span["normalized_text"]: span["span_id"] for span in extraction["pages"][0]["spans"] if span.get("table_candidate")}
        links = {key: [spans[text]] for key, text in (
            ("cell_span_ids", "22.2%"), ("row_header_span_ids", "Miami"), ("column_header_span_ids", "Savings"),
            ("unit_span_ids", "Annual"), ("mandatory_note_span_ids", "Modeled"),
        )}
        client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
            "schema_version": "1.1", "extraction_config_hash": extraction["config_hash"],
            "approved_span_ids": [ids[0] for ids in links.values()], "table_rows": [links], "qa_note": "Verified table and qualifier.",
        }).raise_for_status()
        prepared = client.post("/snapshots", headers=HEADERS, json={
            "schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small", "chunking": "fixed",
        }).raise_for_status().json()
        assert prepared["chunk_count"] == prepared["table_row_count"] == 1
        publish(client, prepared["snapshot_id"])
        found = search(client, "22.2")
        assert len(found["bundles"]) == 1 and "Modeled" in found["bundles"][0]["rendered_context"]
