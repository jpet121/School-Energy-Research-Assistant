import json
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter

from .contracts import ApiError, ShortText, Versioned, fingerprint, now
from .corpus import Opaque, current, fts_table
from .research import withdrawn
from .store import Store


class Withdrawal(Versioned):
    reason: ShortText


class Cleanup(Versioned):
    preview_hash: Opaque
    confirm: Literal[True]


class Controls:
    def __init__(self, store: Store) -> None:
        self.store = store

    def withdraw(self, version_id: str, request: Withdrawal) -> dict:
        with self.store.connection(write=True) as db:
            if db.execute("SELECT 1 FROM documents WHERE version_id=?", (version_id,)).fetchone() is None:
                raise ApiError(404, "missing_edition", "That recorded edition could not be found.")
            previous = withdrawn(db, version_id)
            if previous:
                return {"version_id": version_id, "withdrawal": previous}
            revision = current(db)["eligibility_revision"] + 1
            flag = {"reason": request.reason, "recorded_at": now(), "revision": revision}
            db.execute("INSERT INTO withdrawals VALUES (?,?,?,?)", (version_id, *flag.values()))
            db.execute("UPDATE corpus_state SET eligibility_revision=? WHERE singleton=1", (revision,))
        return {"version_id": version_id, "withdrawal": flag}

    def _candidates(self, db: sqlite3.Connection) -> dict:
        rows = db.execute(
            "SELECT id,manifest FROM snapshots WHERE published_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM runs WHERE snapshot_id=snapshots.id) ORDER BY id"
        )
        manifests = [
            {
                "snapshot_id": row["id"],
                "chunk_count": item["chunk_count"],
                "document_version_ids": [doc["version_id"] for doc in item["documents"]],
            }
            for row in rows for item in [json.loads(row["manifest"])]
        ]
        used = {json.loads(row["metadata"])["byte_hash"] for row in db.execute("SELECT metadata FROM documents")}
        orphan_objects = sorted(
            path.name for path in self.store.objects.iterdir()
            if re.fullmatch(r"[a-f0-9]{64}\.pdf", path.name) and path.stem not in used
        )
        data = {"snapshots": manifests, "unreferenced_originals": orphan_objects}
        return {**data, "preview_hash": fingerprint(data)}

    def preview(self) -> dict:
        with self.store.connection(write=True) as db:
            return self._candidates(db)

    def clean(self, request: Cleanup) -> dict:
        with self.store.connection(write=True) as db:
            preview = self._candidates(db)
            if preview["preview_hash"] != request.preview_hash:
                raise ApiError(409, "cleanup_preview_changed", "Preview cleanup again before removing artifacts.")
            for manifest in preview["snapshots"]:
                snapshot_id = manifest["snapshot_id"]
                db.execute(f"DROP TABLE {fts_table(snapshot_id)}")
                db.execute("DELETE FROM snapshots WHERE id=?", (snapshot_id,))
            for name in preview["unreferenced_originals"]:
                (self.store.objects / name).unlink(missing_ok=True)
        return {
            "removed_snapshots": len(preview["snapshots"]),
            "removed_originals": len(preview["unreferenced_originals"]),
        }


def control_routes(controls: Controls) -> APIRouter:
    router = APIRouter()
    router.add_api_route("/editions/{version_id}/withdraw", controls.withdraw, methods=["POST"])
    router.add_api_route("/cleanup", controls.preview, methods=["GET"])
    router.add_api_route("/cleanup", controls.clean, methods=["POST"])
    return router
