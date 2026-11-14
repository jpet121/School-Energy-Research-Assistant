from test_admission import HEADERS, local_client, make_pdf
from test_corpus import approve_and_prepare, publish
from test_research import search


def draft(client, **kwargs):
    return client.post("/briefs", json={"schema_version": "1.1", **kwargs}, headers=HEADERS)


def attach_first(client):
    snapshot, _ = approve_and_prepare(client, make_pdf("Night setback for school HVAC."))
    publish(client, snapshot["snapshot_id"])
    run = search(client, "setback")
    attached = draft(client, text="", attach={"run_id": run["run_id"], "bundle_id": run["final_bundle_ids"][0]})
    assert attached.status_code == 201, attached.text
    return attached.json()


def test_revision_lineage_undo_and_download_survive_restart(tmp_path):
    with local_client(tmp_path) as client:
        root = attach_first(client)
        edited = draft(client, brief_id=root["brief_id"], parent_revision_id=root["revision_id"],
                       text=root["text"].replace("Night setback", "Day setback")).json()
        assert edited["revision_id"] != root["revision_id"]
        assert edited["claims"][0]["review_status"] == "NEEDS_EVIDENCE_REVIEW"
        repeat = draft(client, brief_id=root["brief_id"], parent_revision_id=edited["revision_id"], text=edited["text"]).json()
        assert repeat["revision_id"] == edited["revision_id"]
        undo = draft(client, brief_id=root["brief_id"], parent_revision_id=edited["revision_id"], restore_revision_id=root["revision_id"]).json()
        assert undo["text"] == root["text"]
        assert undo["claims"] == root["claims"]

    with local_client(tmp_path) as restarted:
        path = f"/briefs/{root['brief_id']}/export"
        saved = restarted.get(path, params={"format": "json", "revision_id": root["revision_id"]}, headers=HEADERS).json()
        assert saved["revision_id"] == root["revision_id"] and saved["parent_revision_id"] is None
        md = restarted.get(path, params={"format": "markdown", "revision_id": undo["revision_id"]}, headers=HEADERS).text
        assert f"Parent revision: {edited['revision_id']}" in md
        assert f"Revision: {undo['revision_id']}" in md
        assert root["text"] in md


def test_stale_revision_and_failed_download_keep_saved_draft(tmp_path):
    with local_client(tmp_path) as client:
        root = attach_first(client)
        brief = root["brief_id"]
        edit = {"brief_id": brief, "parent_revision_id": root["revision_id"], "text": root["text"]}
        claim = root["claims"][0]
        for fields, code in (
            ({**edit, "detach_claim_ids": ["unknown"]}, "invalid_claim_mapping"),
            ({**edit, "text": ""}, "empty_draft"),
            ({"brief_id": brief, "text": root["text"]}, "invalid_contract"),
        ):
            rejected = draft(client, **fields)
            assert rejected.json()["code"] == code
        other = draft(client, text="Independent draft.").raise_for_status().json()
        foreign = draft(client, brief_id=brief, parent_revision_id=root["revision_id"], restore_revision_id=other["revision_id"])
        assert (foreign.status_code, foreign.json()["code"]) == (404, "missing_revision")
        detached = draft(client, **edit, detach_claim_ids=[claim["claim_id"]]).raise_for_status().json()
        assert detached["claims"] == [] and claim["marker"] not in detached["text"]
        assert claim["text"] in detached["text"]
        assert draft(client, **edit).json()["code"] == "revision_changed"

        operation = client.post(f"/briefs/{brief}/exports", headers=HEADERS, json={
            "schema_version": "1.1", "revision_id": root["revision_id"], "format": "json",
        }).raise_for_status().json()["operation_id"]
        path = f"/exports/{operation}"
        byte_hash = claim["citations"][0]["document_byte_hash"]
        original = tmp_path / "objects" / f"{byte_hash}.pdf"
        content = original.read_bytes()
        original.unlink()
        failure = client.get(path, headers=HEADERS)
        assert (failure.status_code, failure.json()["code"]) == (409, "missing_original")
        original.write_bytes(content)
        assert client.get(path, headers=HEADERS).json()["code"] == "export_not_pending"
        recovered = client.get(f"/briefs/{brief}/export", headers=HEADERS, params={
            "format": "json", "revision_id": root["revision_id"],
        }).raise_for_status().json()
        assert recovered["text"] == root["text"] and recovered["claims"] == root["claims"]
        current = client.get(f"/briefs/{brief}", headers=HEADERS).raise_for_status().json()
        assert current == detached


def test_deleting_only_one_marker_preserves_the_other_association(tmp_path):
    with local_client(tmp_path) as client:
        root = attach_first(client)
        citation = root["claims"][0]["citations"][0]
        paired = draft(client, brief_id=root["brief_id"], parent_revision_id=root["revision_id"], text=root["text"], attach={
            "run_id": root["run_id"], "bundle_id": citation["bundle_id"],
        }).raise_for_status().json()
        first, second = paired["claims"]
        detached = draft(client, brief_id=root["brief_id"], parent_revision_id=paired["revision_id"],
                         text=paired["text"].replace(first["marker"], "")).raise_for_status().json()
        assert detached["claims"] == [second]
        assert first["marker"] not in detached["text"] and first["text"] in detached["text"]
        assert second["marker"] in detached["text"] and detached["parent_revision_id"] == paired["revision_id"]
        history = client.get(f"/briefs/{root['brief_id']}/revisions", headers=HEADERS).raise_for_status().json()["revisions"]
        assert next(row for row in history if row["revision_id"] == paired["revision_id"]) == paired

        edited = draft(client, brief_id=root["brief_id"], parent_revision_id=detached["revision_id"],
                       text=detached["text"] + "\nNew uncited prose.").raise_for_status().json()
        assert edited["claims"] == [{**second, "review_status": "NEEDS_EVIDENCE_REVIEW"}]
from copy import deepcopy
import json
import sqlite3

from retrofit.contracts import fingerprint



def test_revision_hash_is_checked_before_opening_or_delivering_saved_text(tmp_path):
    with local_client(tmp_path) as client:
        original = draft(client, text="Approved to retain this exact fixture text.").raise_for_status().json()
        path = f"/briefs/{original['brief_id']}"
        prepare = {"schema_version": "1.1", "format": "json"}
        operation = client.post(f"{path}/exports", headers=HEADERS, json=prepare).raise_for_status().json()["operation_id"]
        export = f"/exports/{operation}"
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE revisions SET payload=? WHERE id=?", (
                json.dumps({**original, "text": "Altered text under the previous identifier."}), original["revision_id"],
            ))
        for endpoint in (path, f"{path}/revisions", f"{path}/export?format=json", export, "/briefs"):
            denied = client.get(endpoint, headers=HEADERS)
            assert (denied.status_code, denied.json().get("code")) == (409, "invalid_saved_revision")
            assert "Altered text" not in denied.text
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE revisions SET payload=? WHERE id=?", (json.dumps(original), original["revision_id"]))
        assert client.get(path, headers=HEADERS).json() == original
        assert client.get(export, headers=HEADERS).json()["code"] == "export_not_pending"
        retry = client.post(f"{path}/exports", headers=HEADERS, json=prepare).raise_for_status().json()["operation_id"]
        delivered = client.get(f"/exports/{retry}", headers=HEADERS).raise_for_status().json()
        assert (delivered["revision_id"], delivered["text"]) == (original["revision_id"], original["text"])


def test_latest_pointer_cannot_open_another_briefs_valid_revision(tmp_path):
    with local_client(tmp_path) as client:
        original = draft(client, text="First draft.").raise_for_status().json()
        other = draft(client, text="Second draft.").raise_for_status().json()
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE briefs SET latest_revision_id=? WHERE id=?", (other["revision_id"], original["brief_id"]))
        path = f"/briefs/{original['brief_id']}"
        denied = client.get(path, headers=HEADERS)
        assert (denied.status_code, denied.json().get("code")) == (404, "missing_revision")
        assert client.get("/briefs", headers=HEADERS).json() == {"briefs": [other]}
        original_by_id = client.get(path, params={"revision_id": original["revision_id"]}, headers=HEADERS).raise_for_status()
        assert original_by_id.json() == original


def test_draft_bounds_leave_latest_revision_and_export_operations_intact(tmp_path):
    with local_client(tmp_path) as client:
        original = attach_first(client)
        claim = original["claims"][0]["citations"][0]
        brief = original["brief_id"]
        parent = original["revision_id"]
        path = f"/briefs/{brief}"
        for invalid in (
            {},
            {"brief_id": brief, "parent_revision_id": parent, "restore_revision_id": parent, "text": "Conflict."},
            {"text": "", "attach": {"run_id": original["run_id"], "bundle_id": claim["bundle_id"]},
             "generated_operation_id": "different"},
        ):
            assert draft(client, **invalid).json()["code"] == "invalid_contract"
        large = draft(client, brief_id=brief, parent_revision_id=parent, text="a" * 128_000,
                      attach={"run_id": original["run_id"], "bundle_id": claim["bundle_id"]})
        assert (large.status_code, large.json()["code"]) == (413, "draft_limit")
        assert client.get(path, headers=HEADERS).json() == original
        assert client.get("/exports/unknown", headers=HEADERS).json()["code"] == "missing_export"
        assert client.post("/exports/unknown/cancel", headers=HEADERS, json={}).json()["code"] == "missing_export"


def test_exports_recheck_recorded_bundle_and_anchor_relationships(tmp_path):
    with local_client(tmp_path) as client:
        root = attach_first(client)
        path = f"/briefs/{root['brief_id']}/export"
        db_path = tmp_path / "evidence.sqlite3"
        with sqlite3.connect(db_path) as db:
            saved = db.execute("SELECT * FROM runs WHERE id=?", (root["run_id"],)).fetchone()
        intact = json.loads(saved[3])
        for field, value in (
            ("bundle_id", "unrelated"), ("version_id", "unrelated"), ("spans", []),
        ):
            changed = deepcopy(intact)
            changed["bundles"][0][field] = value
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps(changed), root["run_id"]))
            denied = client.get(path, headers=HEADERS, params={"format": "json"})
            assert (denied.status_code, denied.json()["code"]) == (409, "invalid_saved_anchor")
        for field, value in (("normalized_text", "Different source content"), ("rotation", 180)):
            changed = deepcopy(intact)
            changed["bundles"][0]["spans"][0][field] = value
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps(changed), root["run_id"]))
            denied = client.get(path, headers=HEADERS, params={"format": "json"})
            assert (denied.status_code, denied.json()["code"]) == (409, "invalid_saved_anchor")
        with sqlite3.connect(db_path) as db:
            db.execute("DELETE FROM runs WHERE id=?", (root["run_id"],))
        assert client.get(path, headers=HEADERS, params={"format": "json"}).json()["code"] == "invalid_saved_anchor"
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?)", saved)

        citation = root["claims"][0]["citations"][0]
        source = citation["anchors"][0]["source_span_id"]
        for changes in (
            {"document_byte_hash": "mismatched"},
            {"anchors": [{**citation["anchors"][0], "source_span_id": "unrecorded"}]},
            {"quote": {"source_span_id": "unrecorded", "start": 0, "end": 1, "text": "N", "bounding_boxes": []}},
            {"quote": {"source_span_id": source, "start": 0, "end": 1, "text": "N", "bounding_boxes": []}},
        ):
            # A self-consistent legacy revision hash must not certify a broken source relationship.
            changed = deepcopy(root)
            changed.pop("revision_id")
            changed["claims"][0]["citations"][0].update(changes)
            identity = fingerprint(changed)
            payload = json.dumps({**changed, "revision_id": identity})
            with sqlite3.connect(db_path) as db:
                db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (identity, root["brief_id"], None, root["run_id"], payload))
            rejected = client.get(path, headers=HEADERS, params={"format": "json", "revision_id": identity})
            assert (rejected.status_code, rejected.json()["code"]) == (409, "invalid_saved_anchor")
        intact_export = client.get(path, headers=HEADERS, params={"format": "json"}).raise_for_status().json()
        assert intact_export["claims"] == root["claims"]
