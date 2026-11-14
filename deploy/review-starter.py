from contextlib import closing
import json
from pathlib import Path
import sys
from time import monotonic, sleep

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace))
from retrofit.evaluate import client_for

documents = [
    ("C1", "school_retrofit.pdf", "https://docs.nlr.gov/docs/fy14osti/60913.pdf", "December 2013", 73, "beb595c796dcd89d2e8aff83186a31b2f24c48252bd576f06dbd92301d4251db"),
    ("C2", "school_om.pdf", "https://files.eric.ed.gov/fulltext/ED486496.pdf", "August 2004", 78, "4d7025e094e6bc2d3bc90e97f6fe17ea634dc67dc091c65bf85655fab0cb4137"),
    ("C3", "femp_om.pdf", "https://www.energy.gov/sites/default/files/2020/04/f74/omguide_complete_w-eo-disclaimer.pdf", "August 2010", 96, "9cf0adbc462c73b7ec20f97b21643ba709c2c7c4571ff6630a2d628d78e3147c"),
    ("C4", "lighting_2024.pdf", "https://www.energy.gov/sites/default/files/2024-12/upgrading-led-schools_nov24.pdf", "November 2024", 4, "faf3c2a2125c4174a09c907ebe67acab825e5a7057fdf2e9a67e9616b2234761"),
]
with closing(client_for(sys.argv[1])) as client:
    results = {"purpose": "Original-page extraction evidence. Source admission and independent acceptance pending.", "parser": client.get("/parser").json(), "documents": []}
    for identity, filename, url, date, page, expected_hash in documents:
        pdf = (Path(sys.argv[2]) / filename).read_bytes()
        manifest = {
            "schema_version": "1.1", "title": f"{identity} QA source", "publisher": {"C1":"U.S. Department of Energy and National Renewable Energy Laboratory", "C2":"Association of School Business Officials International", "C3":"U.S. Department of Energy Federal Energy Management Program", "C4":"Office of Energy Efficiency and Renewable Energy"}[identity],
            "source_url": url, "unknown_reason": f"Publication is dated {date}. The exact day is unavailable.",
            "scope": f"Selected page {page} parser QA",
            "use_basis": "Local extraction qualification on the publicly linked original. No publication or redistribution.",
            "public_document": True, "coverage_regions": [{"first_page": page, "last_page": page, "use_basis": "Internal engineering inspection only"}],
        }
        job = client.post("/ingestions", files={"pdf": (filename, pdf, "application/pdf")},
                          data={"manifest": json.dumps(manifest), "idempotency_key": identity}).raise_for_status().json()["job_id"]
        started = monotonic()
        client.post(f"/ingestions/{job}/extract", json={}).raise_for_status()
        while True:
            record = client.get(f"/ingestions/{job}").raise_for_status().json()
            if record["state"] != "extracting" or monotonic() - started >= 20:
                break
            sleep(0.06)
        summary = {"id": identity, "job_id": job, "source_sha256": record["document"]["byte_hash"], "state": record["state"],
                   "failure": record["failure_code"], "extraction_seconds": round(monotonic() - started, 3), "pdf_page": page}
        assert summary["source_sha256"] == expected_hash, identity
        summary["hash_matches_accepted_baseline"] = True
        summary["outside_selected_pages_denied"] = client.get(f"/ingestions/{job}/pages/{page + 1}").status_code == 404
        if record["extraction"]:
            found = record["extraction"]["pages"][0]
            summary.update({
                "source_pages": record["extraction"]["page_count"], "candidate_spans": len(found["spans"]),
                "quarantined_lines": found["quarantined_lines"], "table_candidates": found["table_candidates"],
                "pdf_navigation_label": found["printed_page_label"],
            })
            image = client.get(f"/ingestions/{job}/pages/{page}")
            summary["rendered_original"] = image.status_code == 200 and image.content[:4].hex() == "89504e47"
            if summary["rendered_original"]:
                (Path(sys.argv[2]) / f"{identity}-page.png").write_bytes(image.content)
            (Path(sys.argv[2]) / f"{identity}-spans.json").write_text(
                json.dumps(found, ensure_ascii=False), encoding="utf-8"
            )
        results["documents"].append(summary)
    print(json.dumps(results, indent=2))
