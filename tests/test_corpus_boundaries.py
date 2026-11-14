import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from pdf_fixture import pdf_with_pages, text_stream
from retrofit.contracts import ApiError
from retrofit.corpus import fts_table, verify_decimal_spacing
from test_admission import HEADERS, MANIFEST, local_client, make_pdf, stage
from test_corpus import approve_and_prepare, publish
from test_extraction import extract_pdf
from test_research import search


def test_snapshot_contract_preserves_existing_state_on_missing_reviews_or_index(tmp_path):
    with local_client(tmp_path) as client:
        stable, _ = approve_and_prepare(client, make_pdf("Established retrofit collection."))
        active = publish(client, stable["snapshot_id"])["active_snapshot_id"]
        unreviewed = stage(client, make_pdf("Awaiting approval."), key="unreviewed").json()["job_id"]
        contract = {"schema_version": "1.1", "job_ids": [unreviewed]}
        incomplete = client.post("/snapshots", headers=HEADERS, json=contract)
        assert (incomplete.status_code, incomplete.json()["code"]) == (409, "staging_incomplete")
        premature_review = client.post(f"/ingestions/{unreviewed}/review", headers=HEADERS, json={
            "schema_version": "1.1", "extraction_config_hash": "not-yet-extracted",
            "approved_span_ids": ["not-yet-extracted"], "qa_note": "A review before extraction must be rejected.",
        })
        assert (premature_review.status_code, premature_review.json()["code"]) == (409, "staging_incomplete")
        for update, code in (
            ({"job_ids": [unreviewed, unreviewed]}, "duplicate_edition"),
            ({"chunking": "fixed"}, "invalid_contract"),
            ({"embedding_model": "bge-small"}, "invalid_contract"),
        ):
            assert client.post("/snapshots", headers=HEADERS, json={**contract, **update}).json()["code"] == code

        pending, _ = approve_and_prepare(client, make_pdf("New candidate."), key="pending")
        candidate = pending["snapshot_id"]
        assert client.get(f"/snapshots/{candidate}/evaluation-inventory", headers=HEADERS).json()["code"] == "missing_snapshot"
        assert client.post(f"/snapshots/{candidate}/restore", json={}, headers=HEADERS).json()["code"] == "snapshot_not_published"
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("DELETE FROM snapshot_chunks WHERE snapshot_id=?", (candidate,))
        rejected = client.post(f"/snapshots/{candidate}/publish", json={}, headers=HEADERS)
        assert (rejected.status_code, rejected.json()["code"]) == (409, "incomplete_snapshot")
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] == active
        assert search(client, "established")["result_kind"] == "evidence"


def test_invalid_snapshot_cannot_supply_a_dynamic_sql_identifier():
    with pytest.raises(ApiError, match="invalid_snapshot_identity"):
        fts_table("valid; DROP TABLE snapshots")


@pytest.mark.parametrize("initial_damage", [False, True])
def test_stale_review_cannot_replace_a_concurrently_committed_extraction(tmp_path, initial_damage):
    with local_client(tmp_path) as client, ThreadPoolExecutor(max_workers=1) as review_worker:
        job, record = extract_pdf(client, make_pdf("Reviewed source geometry."))
        extracted = record["extraction"]
        path = f"/ingestions/{job}/review"
        request = {
            "schema_version": "1.1", "extraction_config_hash": extracted["config_hash"],
            "approved_span_ids": [extracted["pages"][0]["spans"][0]["span_id"]],
            "qa_note": "Review of the original artifact.",
        }
        if initial_damage:
            with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
                db.execute("UPDATE ingestions SET extraction=json_set(extraction,'$.pages[0].spans[0].normalization_map',NULL) WHERE id=?", (job,))
        submitted = Event()

        def save():
            submitted.set()
            return client.post(path, headers=HEADERS, json=request)

        current = {**extracted, "verified_fixture_revision": "Restored under the concurrent writer."}
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("BEGIN IMMEDIATE")
            awaiting = review_worker.submit(save)
            assert submitted.wait(2)
            time.sleep(0.25)
            assert not awaiting.done()
            db.execute("UPDATE ingestions SET extraction=? WHERE id=?", (json.dumps(current), job))
        rejected = awaiting.result(timeout=7)
        assert (rejected.status_code, rejected.json()["code"]) == (409, "invalid_span_geometry" if initial_damage else "review_changed")
        restored = client.get(f"/ingestions/{job}", headers=HEADERS).raise_for_status().json()
        assert restored["state"] == "review_required" and restored["extraction"] == current
        assert client.post(path, headers=HEADERS, json=request).raise_for_status().json()["state"] == "ready"


def test_table_review_preserves_required_notes_and_reversible_decimal_geometry(tmp_path):
    pdf = pdf_with_pages(
        text_stream("22 . 2 percent", y=740) + text_stream("Miami school", y=710)
        + text_stream("Energy savings", y=680) + text_stream("Percent", y=650)
        + text_stream("Excludes EBCx", y=620),
        text_stream("Qualifier on the next page."),
    )
    manifest = {**MANIFEST, "coverage_regions": [{"first_page": 1, "last_page": 2, "use_basis": "Synthetic table."}]}
    with local_client(tmp_path) as client:
        job, record = extract_pdf(client, pdf, manifest=manifest)
        data = record["extraction"]
        first, second = data["pages"]
        cell, row, column, unit, footnote = (span["span_id"] for span in first["spans"])
        next_page = second["spans"][0]["span_id"]
        links = {
            "cell_span_ids": [cell], "row_header_span_ids": [row], "column_header_span_ids": [column],
            "unit_span_ids": [unit], "mandatory_note_span_ids": [footnote],
        }
        review = {
            "schema_version": "1.1", "extraction_config_hash": data["config_hash"],
            "approved_span_ids": [cell, row, column, unit, footnote, next_page],
            "table_rows": [links], "qa_note": "Verified synthetic value and qualifier.",
        }
        path = f"/ingestions/{job}/review"
        for update, code in (
            ({"extraction_config_hash": "stale"}, "extraction_changed"),
            ({"approved_span_ids": [cell, cell]}, "invalid_review_spans"),
            ({"approved_span_ids": [cell, row, column, unit]}, "invalid_table_links"),
            ({"table_rows": [links, links]}, "invalid_table_links"),
            ({"table_rows": [{**links, "mandatory_note_span_ids": [next_page]}]}, "cross_page_table"),
            ({"table_rows": [{**links, "mandatory_note_span_ids": []}]}, "invalid_contract"),
            ({"normalized_cell_ids": [row]}, "normalization_outside_table"),
        ):
            rejected = client.post(path, headers=HEADERS, json={**review, **update})
            assert rejected.json()["code"] == code
        assert client.get(f"/ingestions/{job}", headers=HEADERS).json()["state"] == "review_required"

        client.post(path, headers=HEADERS, json={**review, "normalized_cell_ids": [cell]}).raise_for_status()
        prepared = client.post("/snapshots", headers=HEADERS, json={
            "schema_version": "1.1", "job_ids": [job],
        }).raise_for_status().json()
        snapshot = publish(client, prepared["snapshot_id"])["active_snapshot_id"]
        inventory = client.get(f"/snapshots/{snapshot}/evaluation-inventory", headers=HEADERS).raise_for_status().json()
        assert len(inventory["table_rows"]) == 1
        run = search(client, "22.2")
        evidence = next(bundle for bundle in run["bundles"] if bundle["table_row"])
        value = evidence["spans"][0]
        assert value["normalized_text"] == "22.2 percent"
        assert value["verified_normalization"]["original_span_id"] == cell
        assert value["bounding_boxes"] == first["spans"][0]["bounding_boxes"]
        assert footnote in evidence["mandatory_qualifier_span_ids"]


def test_collection_page_limit_uses_complete_original_page_counts(tmp_path):
    with local_client(tmp_path) as client:
        jobs = []
        for identity in ("Alpha", "Beta", "Gamma"):
            pdf = pdf_with_pages(text_stream(f"{identity} selected page."), *([""] * 333))
            candidate, job = approve_and_prepare(client, pdf, key=identity)
            assert candidate["page_count"] == 334
            jobs.append(job)
        rejected = client.post("/snapshots", headers=HEADERS, json={"schema_version": "1.1", "job_ids": jobs})
        assert (rejected.status_code, rejected.json()["code"]) == (413, "corpus_page_limit")
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] is None


def test_snapshot_manifest_cannot_be_rewritten_in_place(tmp_path):
    with local_client(tmp_path) as client:
        manifest, _ = approve_and_prepare(client, make_pdf("The original frozen manifest remains recoverable."))
        snapshot = publish(client, manifest["snapshot_id"])["active_snapshot_id"]
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            raw, = db.execute("SELECT manifest FROM snapshots WHERE id=?", (snapshot,)).fetchone()
            changed = json.loads(raw)
            changed["retrieval_settings"]["candidate_limit"] = 99
            db.execute("UPDATE snapshots SET manifest=? WHERE id=?", (json.dumps(changed), snapshot))
        for response in (
            client.get(f"/snapshots/{snapshot}/evaluation-inventory", headers=HEADERS),
            client.post(f"/snapshots/{snapshot}/restore", headers=HEADERS, json={}),
            client.post("/queries", headers=HEADERS, json={"schema_version": "1.1", "question": "frozen", "mode": "lexical"}),
        ):
            assert (response.status_code, response.json()["code"]) == (409, "invalid_snapshot_identity")
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE snapshots SET manifest=? WHERE id=?", (raw, snapshot))
        assert search(client, "frozen")["result_kind"] == "evidence"


def test_ambiguous_grid_cannot_merge_unrelated_value_rows(tmp_path):
    grid = (
        "50 580 260 120 re S\n50 640 m 310 640 l S\n180 580 m 180 700 l S\n"
        + text_stream("School", x=60, y=668) + text_stream("Savings", x=190, y=668)
        + text_stream("Miami", x=60, y=610) + text_stream("22.2%", x=190, y=610)
    )
    with local_client(tmp_path) as client:
        job, record = extract_pdf(client, pdf_with_pages(grid))
        data = record["extraction"]
        ids = {span["normalized_text"]: span["span_id"] for span in data["pages"][0]["spans"]}
        review = {
            "schema_version": "1.1", "extraction_config_hash": data["config_hash"],
            "approved_span_ids": list(ids.values()), "qa_note": "Grid fixture.",
        }
        path = f"/ingestions/{job}/review"
        assert client.post(path, headers=HEADERS, json=review).json()["code"] == "ambiguous_span"
        row = {
            "cell_span_ids": [ids["Savings"], ids["22.2%"]], "row_header_span_ids": [ids["Miami"]],
            "column_header_span_ids": [ids["Savings"]], "unit_span_ids": [ids["22.2%"]],
            "mandatory_note_span_ids": [], "notes_absent_reason": "The synthetic table has no notes.",
        }
        assert client.post(path, headers=HEADERS, json={**review, "table_rows": [row]}).json()["code"] == "unrelated_table_cells"
        row["cell_span_ids"] = [ids["22.2%"]]
        row["row_header_span_ids"].append(ids["School"])
        reviewed = client.post(path, headers=HEADERS, json={**review, "table_rows": [row]}).raise_for_status().json()
        assert reviewed["table_row_count"] == 1


def test_decimal_normalization_does_not_merge_distinct_punctuation():
    original = {
        "span_id": "original", "normalized_text": "2..3", "normalization_map": [0, 1, 2, 3],
        "raw_character_boxes": [[0, 1, 2, 3], [2, 1, 3, 3], [2, 1, 3, 3], [4, 1, 6, 3]],
    }
    verified = verify_decimal_spacing(original)
    assert verified["normalized_text"] == "2.3" and verified["normalization_map"] == [0, 1, 3]
    for changes in (
        {"normalization_map": [0, 1, None, 3]},
        {"raw_character_boxes": [[0, 1, 2, 3], [2, 1, 3, 3], [3, 1, 4, 3], [4, 1, 6, 3]]},
        {"normalized_text": "2  3"},
    ):
        with pytest.raises(ApiError, match="no_decimal_spacing_to_normalize"):
            verify_decimal_spacing({**original, **changes})


def test_snapshot_chunk_limit_uses_real_verified_pdf_lines(tmp_path):
    with local_client(tmp_path) as client:
        jobs = []
        for identity in ("A", "B", "C"):
            pdf = pdf_with_pages(*[
                "".join(text_stream(f"{identity}{page:03d}{line:02d}", y=745 - 18 * line) for line in range(36))
                for page in range(100)
            ])
            manifest = {**MANIFEST, "coverage_regions": [{"first_page": 1, "last_page": 100, "use_basis": "Capacity fixture."}]}
            job, record = extract_pdf(client, pdf, manifest=manifest, key=identity)
            extraction = record["extraction"]
            spans = [span["span_id"] for page in extraction["pages"] for span in page["spans"]]
            assert len(spans) == 3600
            client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
                "schema_version": "1.1", "extraction_config_hash": extraction["config_hash"],
                "approved_span_ids": spans, "qa_note": "Synthetic chunk-capacity fixture.",
            }).raise_for_status()
            jobs.append(job)
        rejected = client.post("/snapshots", headers=HEADERS, json={"schema_version": "1.1", "job_ids": jobs})
        assert (rejected.status_code, rejected.json()["code"]) == (413, "corpus_chunk_limit")
        assert client.get("/snapshots", headers=HEADERS).json()["snapshots"] == []
