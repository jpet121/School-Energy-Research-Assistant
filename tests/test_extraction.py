import json
import sqlite3
import time

from pdf_fixture import pdf_with_pages, text_stream
from retrofit.contracts import PDF_BYTES
from test_admission import HEADERS, MANIFEST, local_client, make_pdf, stage


def wait_for_job(client, job_id):
    for _ in range(240):
        result = client.get(f"/ingestions/{job_id}", headers=HEADERS).json()
        if result["state"] not in {"extracting", "cancel_requested"}:
            return result
        time.sleep(0.05)
    raise AssertionError("The bounded extraction job did not reach a terminal state.")


def extract_pdf(client, pdf, **kwargs):
    job_id = stage(client, pdf, **kwargs).json()["job_id"]
    response = client.post(f"/ingestions/{job_id}/extract", json={}, headers=HEADERS)
    assert response.status_code == 202, response.text
    return job_id, wait_for_job(client, job_id)


def test_original_pdf_is_extracted_by_the_bounded_worker(tmp_path):
    with local_client(tmp_path) as client:
        profile = client.get("/parser", headers=HEADERS).json()
        assert profile["enabled"] and profile["network_denied"]
        assert profile["memory_bytes"] > 0 and profile["cpu_seconds"] > 0 and profile["wall_seconds"] > 0
        job_id, result = extract_pdf(client, make_pdf("School HVAC night setback."))
        assert result["state"] == "review_required", result
        extraction = result["extraction"]
        assert extraction["page_count"] == 1
        span = extraction["pages"][0]["spans"][0]
        assert span["normalized_text"] == "School HVAC night setback."
        assert span["pdf_page_1based"] == 1
        assert 49 < span["bounding_boxes"][0][0] < 51
        assert len(span["normalization_map"]) == len(span["normalized_text"])
        preview = client.get(f"/ingestions/{job_id}/pages/1", headers=HEADERS)
        assert preview.headers["content-type"] == "image/png"
        assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
        replay = client.post(f"/ingestions/{job_id}/extract", headers=HEADERS, json={})
        assert replay.json() == {"job_id": job_id, "state": "review_required"}
        assert client.get(f"/ingestions/{job_id}", headers=HEADERS).json()["extraction"] == extraction


def test_parser_failure_and_cancel_are_recoverable(tmp_path):
    with local_client(tmp_path) as client:
        profile = client.get("/parser", headers=HEADERS).json()
        assert profile["child_creation_denied"]
        _, invalid = extract_pdf(client, b"%PDF-invalid")
        assert invalid["state"] == "failed"
        assert invalid["failure_code"] == "parser_rejected_pdf"
        assert invalid["extraction"] is None
        pdf = make_pdf("Retain the original and retry safely.")
        job_id = stage(client, pdf, key="cancel").json()["job_id"]
        assert client.post(f"/ingestions/{job_id}/cancel", json={}, headers=HEADERS).json()["state"] == "canceled"
        assert client.post(f"/ingestions/{job_id}/cancel", json={}, headers=HEADERS).json()["state"] == "canceled"
        resumed, result = extract_pdf(client, pdf, key="cancel")
        assert resumed == job_id
        assert result["state"] == "review_required"
        assert client.post(f"/ingestions/{job_id}/cancel", json={}, headers=HEADERS).json()["state"] == "already_completed"


def test_staging_only_previews_the_pages_selected_for_extraction(tmp_path):
    pdf = pdf_with_pages(text_stream("Outside the selected page range."), text_stream("Approved local sample."))
    manifest = {**MANIFEST, "coverage_regions": [{"first_page": 2, "last_page": 2, "use_basis": "Fixture page 2"}]}
    with local_client(tmp_path) as client:
        job, result = extract_pdf(client, pdf, manifest=manifest)
        assert result["extraction"]["page_count"] == 2
        assert [page["pdf_page_1based"] for page in result["extraction"]["pages"]] == [2]
        assert client.get(f"/ingestions/{job}/pages/2", headers=HEADERS).headers["content-type"] == "image/png"
        outside = client.get(f"/ingestions/{job}/pages/1", headers=HEADERS)
        assert (outside.status_code, outside.json()["code"]) == (404, "missing_page")


def test_incomplete_quote_mapping_cannot_produce_a_partial_highlight(tmp_path):
    with local_client(tmp_path) as client:
        job, record = extract_pdf(client, make_pdf("Preserve the exact evidence location."))
        artifact = record["extraction"]
        span = artifact["pages"][0]["spans"][0]
        del span["normalization_map"]
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as persisted:
            persisted.execute("UPDATE ingestions SET extraction=? WHERE id=?", (json.dumps(artifact), job))
        response = client.post(f"/ingestions/{job}/review", json={
            "schema_version": "1.1", "extraction_config_hash": artifact["config_hash"],
            "approved_span_ids": [span["span_id"]], "qa_note": "Fault injected at the persisted artifact boundary.",
        }, headers=HEADERS)
        assert (response.status_code, response.json()["code"]) == (409, "invalid_span_geometry")
        rejected = client.get(f"/ingestions/{job}", headers=HEADERS).json()
        assert (rejected["state"], rejected["failure_code"], rejected["extraction"]) == ("failed", "invalid_span_geometry", None)
        assert client.get("/snapshots", headers=HEADERS).json()["snapshots"] == []
        client.post(f"/ingestions/{job}/extract", json={}, headers=HEADERS).raise_for_status()
        restored = wait_for_job(client, job)
        assert restored["state"] == "review_required"
        assert restored["extraction"]["pages"][0]["spans"][0]["normalization_map"]


def test_missing_or_damaged_original_fails_extraction_and_can_be_restored(tmp_path):
    pdf = make_pdf("Recover the exact saved original.")
    with local_client(tmp_path) as client:
        job = stage(client, pdf).json()["job_id"]
        source = client.get(f"/ingestions/{job}", headers=HEADERS).json()["document"]
        saved = tmp_path / source["original_path"]
        saved.unlink()
        original_url = f"/editions/{source['version_id']}/original"
        start_url = f"/ingestions/{job}/extract"
        assert client.get(original_url, headers=HEADERS).json()["code"] == "missing_original"
        client.post(start_url, json={}, headers=HEADERS).raise_for_status()
        assert wait_for_job(client, job)["failure_code"] == "missing_original"
        saved.write_bytes(make_pdf("Unrelated edition."))
        assert client.get(original_url, headers=HEADERS).json()["code"] == "source_hash_mismatch"
        assert stage(client, pdf, key="repair-original").json()["code"] == "source_hash_mismatch"
        with saved.open("r+b") as stream:
            stream.truncate(PDF_BYTES + 1)
        assert stage(client, pdf).json()["code"] == "source_hash_mismatch"
        saved.unlink()
        assert stage(client, pdf).json()["job_id"] == job
        assert saved.read_bytes() == pdf
        client.post(start_url, json={}, headers=HEADERS).raise_for_status()
        recovered = wait_for_job(client, job)
        assert recovered["state"] == "review_required"
        assert client.get(original_url, headers=HEADERS).content == pdf


def test_off_page_text_is_quarantined_while_visible_text_can_publish(tmp_path):
    pdf = pdf_with_pages(text_stream("Off-page evidence", x=-1000, y=740) + text_stream("Visible evidence", y=680))
    with local_client(tmp_path) as client:
        job, record = extract_pdf(client, pdf)
        extraction = record["extraction"]
        by_text = {span["normalized_text"]: span for span in extraction["pages"][0]["spans"]}
        hidden = by_text["Off-page evidence"]
        assert hidden["bounding_boxes"][0][2] < 0 and "outside" in hidden.get("excluded_reason", "")
        review = {"schema_version": "1.1", "extraction_config_hash": extraction["config_hash"],
                  "approved_span_ids": [hidden["span_id"]], "qa_note": "Checked the rendered page."}
        path = f"/ingestions/{job}/review"
        invalid = client.post(path, json=review, headers=HEADERS)
        assert (invalid.status_code, invalid.json()["code"]) == (400, "invalid_review_spans")
        review["approved_span_ids"] = [by_text["Visible evidence"]["span_id"]]
        client.post(path, json=review, headers=HEADERS).raise_for_status()
        snapshot = client.post("/snapshots", json={"schema_version": "1.1", "job_ids": [job]}, headers=HEADERS)
        prepared = snapshot.raise_for_status().json()
        assert prepared["chunk_count"] == 1
        published = client.post(f"/snapshots/{prepared['snapshot_id']}/publish", json={}, headers=HEADERS)
        assert published.raise_for_status().json()["active_snapshot_id"] == prepared["snapshot_id"]
