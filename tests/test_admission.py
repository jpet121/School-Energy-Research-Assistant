import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse
import pytest

from retrofit.app import create_app
from retrofit.contracts import PDF_BYTES


ORIGIN = "http://127.0.0.1:8765"
HEADERS = {"X-Session-Token": "test-local-launch", "Origin": ORIGIN}
MANIFEST = {
    "schema_version": "1.1",
    "title": "School energy controls",
    "publisher": "Fixture publisher",
    "source_url": "https://example.org/controls.pdf",
    "publication_date": "2025-01-01",
    "scope": "School HVAC controls",
    "use_basis": "Public fixture released for testing",
    "public_document": True,
    "coverage_regions": [{"first_page": 1, "last_page": 1, "use_basis": "Public fixture"}],
}


def make_pdf(*lines: str) -> bytes:
    commands = ["BT /F1 12 Tf 50 730 Td 18 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"({escaped}) Tj T*")
    stream = ("\n".join(commands) + "\nET\n").encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    pdf.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(pdf)


def stage(client, pdf, manifest=MANIFEST, key="fixture-1", headers=HEADERS):
    return client.post(
        "/ingestions",
        files={"pdf": ("controls.pdf", pdf, "application/pdf")},
        data={"manifest": json.dumps(manifest), "idempotency_key": key},
        headers=headers,
    )


def local_client(path, token=HEADERS["X-Session-Token"], **settings):
    return TestClient(create_app(path, origin=ORIGIN, token=token, **settings), base_url=ORIGIN, client=("127.0.0.1", 51234))


def test_local_boundary_redacts_invalid_saved_data_and_unknown_routes(tmp_path, caplog):
    with pytest.raises(ValueError, match="explicit IPv4 loopback origin"):
        create_app(tmp_path, origin="http://0.0.0.0:8765")
    with local_client(tmp_path) as client:
        missing = client.get("/unrecognized-route", headers=HEADERS)
        assert (missing.status_code, missing.json()["code"]) == (404, "request_failed")
        staged = stage(client, make_pdf("Protect private data.")).raise_for_status().json()
        document = client.get(f"/ingestions/{staged['job_id']}", headers=HEADERS).json()["document"]
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE documents SET metadata=? WHERE version_id=?", ("private invalid JSON", document["version_id"]))
        failed = client.get("/catalog", headers=HEADERS)
        assert (failed.status_code, failed.json()["code"]) == (500, "local_failure")
        assert "private invalid JSON" not in failed.text and failed.headers["cache-control"] == "no-store"
        assert f"code=local_failure correlation_id={failed.json()['correlation_id']}" in caplog.text
        rejected = client.post("/queries", json={
            "schema_version": "incompatible", "question": "Private full query sentinel for safe diagnostic.",
        }, headers=HEADERS)
        assert rejected.json()["code"] == "invalid_contract"
        assert f"code=invalid_contract correlation_id={rejected.json()['correlation_id']}" in caplog.text
        assert "private invalid JSON" not in caplog.text
        assert "Private full query sentinel for safe diagnostic." not in caplog.text
        assert HEADERS["X-Session-Token"] not in caplog.text
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE documents SET metadata=? WHERE version_id=?", (json.dumps(document), document["version_id"]))
        assert client.get("/catalog", headers=HEADERS).json()["ingestions"][0]["document"] == document


def test_boundary_keeps_committed_response_when_local_post_delivery_work_fails(tmp_path):
    missing = tmp_path / "unavailable-post-delivery-fixture"
    app = create_app(tmp_path, origin=ORIGIN, token=HEADERS["X-Session-Token"])

    @app.get("/post-delivery-fixture")
    def completed_export():
        return PlainTextResponse("Recorded delivery.", background=BackgroundTask(missing.read_bytes))

    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51234)) as client:
        response = client.get("/post-delivery-fixture", headers=HEADERS)
        assert (response.status_code, response.text, response.headers["cache-control"]) == (200, "Recorded delivery.", "no-store")
        assert client.get("/catalog", headers=HEADERS).raise_for_status().json()["ingestions"] == []


def test_private_store_and_edition_references_are_validated_before_local_mutations(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(PermissionError, match="private data directory"):
        create_app(shared, origin=ORIGIN)
    with local_client(tmp_path / "private") as client:
        pdf = make_pdf("An immutable edition.")
        job = stage(client, pdf).raise_for_status().json()["job_id"]
        changed = stage(client, pdf, key="conflict", manifest={**MANIFEST, "scope": "Conflicting scope"})
        assert (changed.status_code, changed.json()["code"]) == (409, "edition_manifest_conflict")
        assert client.get("/ingestions/missing", headers=HEADERS).json()["code"] == "missing_ingestion"
        assert client.post("/ingestions/missing/cancel", headers=HEADERS, json={}).json()["code"] == "missing_ingestion"
        assert client.get("/editions/missing/original", headers=HEADERS).json()["code"] == "missing_edition"
        assert client.get(f"/ingestions/{job}", headers=HEADERS).json()["document"]["scope"] == MANIFEST["scope"]


def test_public_pdf_is_staged_and_reopened_after_restart(tmp_path):
    pdf = make_pdf("Night setback for school HVAC.")
    with local_client(tmp_path) as client:
        response = stage(client, pdf)
        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        assert stage(client, pdf).json()["job_id"] == job_id

    with local_client(tmp_path) as restarted:
        saved = restarted.get(f"/ingestions/{job_id}", headers=HEADERS).json()
        assert saved["state"] == "staged"
        assert saved["document"]["byte_hash"] == hashlib.sha256(pdf).hexdigest()
        assert saved["document"]["source_url"] == MANIFEST["source_url"]
        assert saved["document"]["coverage_regions"] == MANIFEST["coverage_regions"]
        assert restarted.get(f"/editions/{saved['document']['version_id']}/original", headers=HEADERS).content == pdf


def test_rejected_manifests_and_sessions_do_not_stage_sources(tmp_path):
    pdf = make_pdf("A real PDF fixture.")
    with local_client(tmp_path) as client:
        assert stage(client, pdf, headers={"Origin": ORIGIN}).status_code == 401
        assert stage(client, pdf, headers={**HEADERS, "Origin": "http://foreign.example"}).status_code == 403
        assert client.get("/catalog", headers={**HEADERS, "Host": "other.example"}).status_code == 403
        for changes in (
            {"schema_version": "3.0"}, {"public_document": False}, {"source_url": "file:///tmp/a.pdf"},
            {"scope": ""}, {"publication_date": None}, {"coverage_regions": []},
        ):
            response = stage(client, pdf, manifest={**MANIFEST, **changes})
            assert response.status_code == 400
            assert set(response.json()) == {"code", "message", "correlation_id"}
        assert stage(client, b"malformed").status_code == 422
        assert client.get("/catalog", headers=HEADERS).json()["ingestions"] == []


def test_identical_edition_can_be_reopened_and_different_bytes_have_a_new_edition(tmp_path):
    original = make_pdf("Original")
    amended = make_pdf("Amended")
    with local_client(tmp_path) as client:
        first = stage(client, original).json()["job_id"]
        assert stage(client, original, key="same-edition").json()["job_id"] == first
        assert stage(client, amended).status_code == 409
        second = stage(client, amended, key="new-edition").json()["job_id"]
        first_edition = client.get(f"/ingestions/{first}", headers=HEADERS).json()["document"]
        second_edition = client.get(f"/ingestions/{second}", headers=HEADERS).json()["document"]
        assert first_edition["doc_id"] == second_edition["doc_id"]
        assert first_edition["version_id"] != second_edition["version_id"]


def test_intake_requires_bounded_origin_and_an_unambiguous_coverage_manifest(tmp_path):
    pdf = make_pdf("Original policy fixture.")
    with local_client(tmp_path) as client:
        for headers, status, code in (
            ({"X-Session-Token": HEADERS["X-Session-Token"]}, 403, "origin_denied"),
            ({**HEADERS, "Sec-Fetch-Site": "same-site"}, 403, "origin_denied"),
            ({**HEADERS, "Transfer-Encoding": "chunked"}, 400, "unbounded_request"),
            ({**HEADERS, "Content-Length": "four"}, 400, "unbounded_request"),
            ({**HEADERS, "Content-Length": "9" * 4_500}, 413, "request_too_large"),
        ):
            denied = client.post("/ingestions", content=b"x", headers=headers)
            assert (denied.status_code, denied.json()["code"]) == (status, code)
        for changes in (
            {"source_url": "https://name:secret@example.org/controls.pdf"},
            {"publication_date": "2025-01-01", "unknown_reason": "Mutually exclusive."},
            {"coverage_regions": [{"first_page": 2, "last_page": 1, "use_basis": "Reversed."}]},
            {"coverage_regions": MANIFEST["coverage_regions"] * 2},
        ):
            assert stage(client, pdf, manifest={**MANIFEST, **changes}).json()["code"] == "invalid_manifest"
        unbounded_pdf = stage(client, pdf.ljust(PDF_BYTES + 1, b" "))
        assert (unbounded_pdf.status_code, unbounded_pdf.json()["code"]) == (413, "pdf_too_large")
        malformed = client.post("/ingestions", json=MANIFEST, headers=HEADERS)
        assert (malformed.status_code, malformed.json()["code"]) == (400, "invalid_intake")
        assert client.get("/catalog", headers=HEADERS).json()["ingestions"] == []
        manifest = {**MANIFEST, "publication_date": None, "unknown_reason": "Undated public source."}
        admitted = stage(client, pdf, manifest=manifest)
        record = client.get(f"/ingestions/{admitted.json()['job_id']}", headers=HEADERS).json()
        assert record["document"]["publication_date"] is None
        assert record["document"]["unknown_reason"] == manifest["unknown_reason"]
