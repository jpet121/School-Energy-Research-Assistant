from hashlib import sha256

from test_admission import HEADERS, local_client, make_pdf
from test_briefs import attach_first, draft
from test_corpus import approve_and_prepare
from test_research import search


def test_withdrawal_preserves_old_drafts_but_excludes_new_use(tmp_path):
    with local_client(tmp_path) as client:
        root = attach_first(client)
        citation = root["claims"][0]["citations"][0]
        edition = citation["document_version_id"]
        run = client.get(f"/queries/{root['run_id']}", headers=HEADERS).json()
        snapshot = run["snapshot_id"]
        withdraw = client.post(f"/editions/{edition}/withdraw", json={"schema_version": "1.1", "reason": "Fixture disposition withdrawn."}, headers=HEADERS)
        assert withdraw.status_code == 200, withdraw.text
        retry = client.post(f"/editions/{edition}/withdraw", json={
            "schema_version": "1.1", "reason": "A retry must preserve the original disposition.",
        }, headers=HEADERS)
        assert retry.json() == withdraw.json()
        missing = client.post("/editions/unknown/withdraw", json={
            "schema_version": "1.1", "reason": "No matching edition.",
        }, headers=HEADERS)
        assert (missing.status_code, missing.json()["code"]) == (404, "missing_edition")
        assert client.post(f"/snapshots/{snapshot}/restore", json={}, headers=HEADERS).status_code == 200
        assert search(client, "setback")["result_kind"] == "no_evidence"
        assert client.get(f"/queries/{root['run_id']}", headers=HEADERS).json()["final_bundle_ids"] == []
        denied = draft(client, text="", attach={"run_id": root["run_id"], "bundle_id": citation["bundle_id"]})
        assert denied.status_code == 409
        exported = client.get(f"/briefs/{root['brief_id']}/export", params={"format": "json"}, headers=HEADERS).json()
        assert exported["revision_id"] == root["revision_id"]
        assert exported["text"] == root["text"]
        assert exported["claims"] == root["claims"]
        assert exported["withdrawal_warnings"][0]["status"] == "HISTORICAL_UNVERIFIED"
        assert exported["label"] == "Draft — verify evidence"
        override = client.get(f"/briefs/{root['brief_id']}/export", params={
            "format": "json", "label": "Approved",
        }, headers=HEADERS).raise_for_status().json()
        assert override == exported
        markdown = client.get(f"/briefs/{root['brief_id']}/export", params={"format": "markdown"}, headers=HEADERS).text
        assert markdown.startswith("# Draft — verify evidence\n")
        assert "Historical draft — withdrawn source" in markdown
        assert root["revision_id"] in markdown and root["text"] in markdown
        history = client.get(f"/briefs/{root['brief_id']}/revisions", headers=HEADERS).json()["revisions"]
        assert history == [root]
        recovery = draft(
            client, brief_id=root["brief_id"], parent_revision_id=root["revision_id"],
            text="Manual recovery draft with unverified context.",
        ).json()
        assert recovery["claims"] == []
        exact = client.get(f"/briefs/{root['brief_id']}/export", params={
            "format": "json", "revision_id": root["revision_id"],
        }, headers=HEADERS).json()
        assert exact["revision_id"] == root["revision_id"] and exact["claims"] == root["claims"]


def test_cleanup_previews_only_unretained_snapshot_artifacts(tmp_path):
    with local_client(tmp_path) as client:
        attach_first(client)
        candidate, _ = approve_and_prepare(client, make_pdf("Unpublished lighting candidate."), key="unpublished")
        orphan_pdf = make_pdf("Interrupted intake with unreferenced original bytes.")
        orphan_name = sha256(orphan_pdf).hexdigest() + ".pdf"
        orphan = tmp_path / "objects" / orphan_name
        orphan.write_bytes(orphan_pdf)
        preview = client.get("/cleanup", headers=HEADERS).json()
        assert [item["snapshot_id"] for item in preview["snapshots"]] == [candidate["snapshot_id"]]
        assert preview["unreferenced_originals"] == [orphan_name]
        assert client.post("/cleanup", json={"schema_version": "1.1", "preview_hash": "stale", "confirm": True}, headers=HEADERS).status_code == 409
        done = client.post("/cleanup", json={"schema_version": "1.1", "preview_hash": preview["preview_hash"], "confirm": True}, headers=HEADERS)
        assert done.status_code == 200, done.text
        assert done.json() == {"removed_snapshots": 1, "removed_originals": 1}
        assert not orphan.exists()
        assert len(client.get("/snapshots", headers=HEADERS).json()["snapshots"]) == 1
        assert client.get("/briefs", headers=HEADERS).json()["briefs"]


def test_export_cancel_respects_delivery_boundary(tmp_path):
    with local_client(tmp_path) as client:
        root = attach_first(client)
        path = f"/briefs/{root['brief_id']}/exports"
        operation = client.post(path, json={"schema_version": "1.1", "format": "json"}, headers=HEADERS).json()
        url = f"/exports/{operation['operation_id']}"
        assert client.post(f"{url}/cancel", json={}, headers=HEADERS).json()["state"] == "canceled"
        assert client.get(url, headers=HEADERS).status_code == 409
        retry = client.post(path, json={"schema_version": "1.1", "format": "json"}, headers=HEADERS).json()
        retry_url = f"/exports/{retry['operation_id']}"
        assert client.get(retry_url, headers=HEADERS).json()["revision_id"] == root["revision_id"]
        assert client.post(f"{retry_url}/cancel", json={}, headers=HEADERS).json()["state"] == "already_completed"
