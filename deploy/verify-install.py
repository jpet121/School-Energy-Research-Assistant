"""Exercise an installed image with its real store, parser and HTTP endpoints."""

import argparse
from pathlib import Path
import re
import ssl
import sys
from time import monotonic, sleep
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from pdf_fixture import pdf_with_pages, text_stream


def until(client, path, pending):
    deadline = monotonic() + 20
    while monotonic() < deadline:
        result = client.get(path).raise_for_status().json()
        if result["state" if "state" in result else "status"] not in pending:
            return result
        sleep(0.1)
    raise RuntimeError(f"Timed out at {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    parser.add_argument("--certificate", type=Path)
    parser.add_argument("--password-file", type=Path)
    args = parser.parse_args()
    options = {"base_url": args.origin, "trust_env": False, "timeout": 25}
    if args.certificate is not None:
        options["verify"] = ssl.create_default_context(cafile=str(args.certificate))
    with httpx.Client(**options) as client:
        if args.password_file:
            login = client.get("/")
            assert login.status_code == 401 and "Basic " in login.headers["www-authenticate"], login.status_code
            assert client.get("/assets/workbench.js").status_code == 401
            assert client.get("/catalog", auth=("operator", "invalid")).status_code == 401
            client.auth = ("operator", args.password_file.read_text().strip())
        home = client.get("/").raise_for_status()
        assert client.get("/catalog").status_code == 401
        assert client.get("/", headers={"Origin": "https://unrelated.example"}).status_code == 403
        assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert home.headers["cache-control"] == "no-store" and "frame-ancestors 'none'" in home.headers["content-security-policy"]
        if args.password_file:
            assert "max-age=" in home.headers["strict-transport-security"]
        token = re.search(r'<meta name="retrofit-session" content="([A-Za-z0-9_-]+)">', home.text)
        assert token and "Retrofit Evidence" in home.text
        client.headers.update({"Origin": args.origin, "X-Session-Token": token[1]})
        runtime = client.get("/parser").raise_for_status().json()
        assert all(runtime.get(key) for key in ["enabled", "network_denied", "child_creation_denied"]), runtime
        generation = client.get("/generation").raise_for_status().json()
        assert not generation["enabled"] and not generation["configured"]
        pdf = pdf_with_pages(text_stream("Setback scheduling for schools."), text_stream(uuid4().hex))
        manifest = ('{"schema_version":"1.1","title":"Installed runtime test","publisher":"Synthetic fixture",'
                    '"source_url":"https://example.org/fixture.pdf","publication_date":"2025-01-01",'
                    '"scope":"Desktop and web integration test","use_basis":"Synthetic test fixture","public_document":true,'
                    '"coverage_regions":[{"first_page":1,"last_page":1,"use_basis":"Synthetic fixture"}]}')
        file = {"pdf": ("fixture.pdf", pdf, "application/pdf")}
        data = {"manifest": manifest, "idempotency_key": uuid4().hex}
        staged = client.post("/ingestions", files=file, data=data).raise_for_status().json()
        path = f"/ingestions/{staged['job_id']}"
        client.post(f"{path}/extract", json={}).raise_for_status()
        found = until(client, path, {"extracting", "staged"})
        assert found["state"] == "review_required", found.get("failure_code")
        result = found["extraction"]
        spans = result["pages"][0]["spans"]
        png = client.get(f"{path}/pages/1").raise_for_status()
        assert png.content.startswith(b"\x89PNG\r\n\x1a\n")
        client.post(f"{path}/review", json={
            "schema_version": "1.1", "extraction_config_hash": result["config_hash"],
            "approved_span_ids": [span["span_id"] for span in spans],
            "qa_note": "Verified the text of this synthetic test fixture.",
        }).raise_for_status()
        snapshot = client.post("/snapshots", json={
            "schema_version": "1.1", "job_ids": [staged["job_id"]],
        }).raise_for_status().json()["snapshot_id"]
        client.post(f"/snapshots/{snapshot}/publish", json={}).raise_for_status()
        created = client.post("/queries", json={
            "schema_version": "1.1", "question": "setback scheduling", "mode": "lexical",
        }).raise_for_status().json()
        query = until(client, f"/queries/{created['run_id']}", {"pending"})
        assert query["mode_used"] == "lexical" and query["final_bundle_ids"]
        draft = client.post("/briefs", json={
            "schema_version": "1.1", "text": "",
            "attach": {"run_id": created["run_id"], "bundle_id": query["final_bundle_ids"][0]},
        }).raise_for_status().json()
        exported = client.get(f"/briefs/{draft['brief_id']}/export",
                              params={"format": "markdown", "revision_id": draft["revision_id"]}).raise_for_status()
        assert "Setback scheduling for schools." in exported.text
        print(f"PASS {args.origin}: authorization, origin and token checks, sandbox probe, PDF render,"
              " review, publish, lexical retrieval, draft and export. Generation disabled.")


if __name__ == "__main__":
    main()
