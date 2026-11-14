import sqlite3

from pdf_fixture import pdf_with_pages, text_stream
from test_admission import HEADERS, MANIFEST, local_client, make_pdf
from test_extraction import extract_pdf


def approve_and_prepare(client, pdf, *, key="fixture-1", row=False):
    job_id, extracted = extract_pdf(client, pdf, key=key)
    data = extracted["extraction"]
    spans = data["pages"][0]["spans"]
    rows = []
    if row:
        rows.append({
            "cell_span_ids": [spans[0]["span_id"]],
            "row_header_span_ids": [spans[1]["span_id"]],
            "column_header_span_ids": [spans[2]["span_id"]],
            "unit_span_ids": [spans[3]["span_id"]],
            "mandatory_note_span_ids": [spans[4]["span_id"]],
        })
    review = {
        "schema_version": "1.1",
        "extraction_config_hash": data["config_hash"],
        "approved_span_ids": [span["span_id"] for span in spans],
        "table_rows": rows,
        "qa_note": "Compared each fixture line with the original page.",
    }
    result = client.post(f"/ingestions/{job_id}/review", json=review, headers=HEADERS)
    assert result.status_code == 200, result.text
    response = client.post("/snapshots", json={"schema_version": "1.1", "job_ids": [job_id]}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json(), job_id


def publish(client, snapshot_id):
    response = client.post(f"/snapshots/{snapshot_id}/publish", json={}, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_complete_lexical_manifest_publishes_atomically_and_keeps_the_table(tmp_path):
    pdf = make_pdf("22.2% and 21.3%", "Miami high school", "EUI and cost reduction", "Percent", "Excludes EBCx")
    with local_client(tmp_path) as client:
        manifest, _ = approve_and_prepare(client, pdf, row=True)
        snapshot_id = manifest["snapshot_id"]
        assert manifest["enabled_modes"] == ["lexical"]
        assert manifest["table_row_count"] == 1
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] is None
        assert publish(client, snapshot_id)["active_snapshot_id"] == snapshot_id
        assert client.post("/snapshots/incomplete/publish", json={}, headers=HEADERS).status_code == 404

    with local_client(tmp_path) as restarted:
        snapshots = restarted.get("/snapshots", headers=HEADERS).json()
        assert snapshots["active_snapshot_id"] == snapshot_id
        assert snapshots["snapshots"][0]["chunk_set_hash"] == manifest["chunk_set_hash"]


def test_reviewed_printed_page_label_keeps_pdf_index_and_snapshot_identity(tmp_path):
    from test_research import search

    with local_client(tmp_path) as client:
        pdf = pdf_with_pages(text_stream("Outside scope."), text_stream("Commissioning fixture.") + text_stream("7.2", y=14))
        manifest = {**MANIFEST, "coverage_regions": [{"first_page": 2, "last_page": 2, "use_basis": "Reviewed fixture."}]}
        job, record = extract_pdf(client, pdf, manifest=manifest)
        extraction = record["extraction"]
        span = extraction["pages"][0]["spans"][0]
        path = f"/ingestions/{job}/review"
        review = {
            "schema_version": "1.1", "extraction_config_hash": extraction["config_hash"],
            "approved_span_ids": [span["span_id"]], "qa_note": "Checked the visible page label.",
        }
        client.post(path, json=review, headers=HEADERS).raise_for_status()
        prepare = {"schema_version": "1.1", "job_ids": [job]}
        old = client.post("/snapshots", json=prepare, headers=HEADERS).raise_for_status().json()
        invalid = client.post(path, json={**review, "printed_page_labels": {"2": "7.2", "02": "wrong"}}, headers=HEADERS)
        assert (invalid.status_code, invalid.json()["code"]) == (400, "invalid_contract")
        foreign = client.post(path, json={**review, "printed_page_labels": {"1": "7.1"}}, headers=HEADERS)
        assert (foreign.status_code, foreign.json()["code"]) == (400, "invalid_review_pages")
        client.post(path, json={**review, "printed_page_labels": {"2": "7.2"}}, headers=HEADERS).raise_for_status()
        corrected = client.post("/snapshots", json=prepare, headers=HEADERS).raise_for_status().json()
        assert corrected["snapshot_id"] != old["snapshot_id"]
        publish(client, corrected["snapshot_id"])
        found = search(client, "commissioning")["bundles"][0]["spans"][0]
        assert found["printed_page_label"] == "7.2"
        assert found["pdf_page_1based"] == 2 and found["bounding_boxes"] == span["bounding_boxes"]
        saved = client.get(f"/ingestions/{job}", headers=HEADERS).raise_for_status().json()["extraction"]
        assert saved["pages"] == extraction["pages"]


def test_unapproved_or_missing_snapshot_evidence_does_not_replace_active_index(tmp_path):
    with local_client(tmp_path) as client:
        old, _ = approve_and_prepare(client, make_pdf("Original evidence remains eligible."))
        publish(client, old["snapshot_id"])
        new_pdf = make_pdf("Unverified evidence stays quarantined.")
        job, record = extract_pdf(client, new_pdf, key="quarantine")
        response = client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
            "schema_version": "1.1", "extraction_config_hash": record["extraction"]["config_hash"],
            "approved_span_ids": [], "qa_note": "No fixture spans are approved for this edition.",
        })
        assert (response.status_code, response.json()["code"]) == (400, "invalid_contract")
        client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
            "schema_version": "1.1", "extraction_config_hash": record["extraction"]["config_hash"],
            "approved_span_ids": [record["extraction"]["pages"][0]["spans"][0]["span_id"]],
            "qa_note": "Fixture line checked against the original.",
        }).raise_for_status()
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE ingestions SET extraction=json_set(extraction,'$.review.approved_span_ids',json('[]')) WHERE id=?", (job,))
        candidate = client.post("/snapshots", headers=HEADERS, json={"schema_version": "1.1", "job_ids": [job]})
        assert (candidate.status_code, candidate.json()["code"]) == (409, "empty_snapshot")
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute(
                "UPDATE documents SET metadata=json_set(metadata,'$.byte_hash',?) WHERE version_id=?",
                ("f" * 64, old["documents"][0]["version_id"]),
            )
        refused = client.post(f"/snapshots/{old['snapshot_id']}/restore", headers=HEADERS, json={})
        assert (refused.status_code, refused.json()["code"]) == (409, "snapshot_source_changed")
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] == old["snapshot_id"]
