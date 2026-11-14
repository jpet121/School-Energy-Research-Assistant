from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import uuid4

from .contracts import Admission, ApiError, PDF_BYTES, canonical, digest, fingerprint, now


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    version_id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestions (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES documents(version_id),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    extraction TEXT,
    failure_code TEXT
);
CREATE TABLE IF NOT EXISTS ingestion_keys (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES ingestions(id)
);
"""


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and self.root.stat().st_mode & 0o077:
            raise PermissionError("Choose a private data directory with mode 0700")
        self.objects = self.root / "objects"
        self.objects.mkdir(exist_ok=True, mode=0o700)
        self.database = self.root / "evidence.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        self.database.chmod(0o600)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _save_pdf(self, byte_hash: str, pdf: bytes) -> Path:
        destination = self.objects / f"{byte_hash}.pdf"
        if destination.is_file():
            self._verified_pdf(destination, byte_hash)
            return destination
        with NamedTemporaryFile(dir=self.objects, delete=False) as staging:
            temporary = Path(staging.name)
            try:
                staging.write(pdf)
                staging.flush()
                os.fsync(staging.fileno())
                staging.close()
                os.replace(temporary, destination)
                if os.name == "posix":
                    directory_fd = os.open(self.objects, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    def stage(self, admission: Admission, pdf: bytes, key: str) -> str:
        byte_hash = digest(pdf)
        manifest = admission.model_dump(mode="json")
        doc_id = fingerprint({"source_url": manifest["source_url"]})
        version_id = fingerprint({"doc_id": doc_id, "byte_hash": byte_hash})
        request_hash = fingerprint([version_id, manifest])
        with self.connection(write=True) as db:
            existing_key = db.execute("SELECT request_hash, job_id FROM ingestion_keys WHERE key=?", (key,)).fetchone()
            if existing_key:
                if existing_key["request_hash"] != request_hash:
                    raise ApiError(409, "idempotency_conflict", "That intake key belongs to a different PDF or manifest.")
                self._save_pdf(byte_hash, pdf)
                return str(existing_key["job_id"])
            existing = db.execute(
                "SELECT id, metadata FROM ingestions JOIN documents USING(version_id) WHERE version_id=?", (version_id,)
            ).fetchone()
            if existing:
                saved = json.loads(existing["metadata"])
                if any(saved[name] != value for name, value in manifest.items()):
                    raise ApiError(409, "edition_manifest_conflict", "This edition already has a different recorded manifest.")
                self._save_pdf(byte_hash, pdf)
                job_id = str(existing["id"])
            else:
                original = self._save_pdf(byte_hash, pdf)
                metadata = {
                    **manifest,
                    "doc_id": doc_id,
                    "version_id": version_id,
                    "byte_hash": byte_hash,
                    "original_path": original.relative_to(self.root).as_posix(),
                    "retrieved_at": now(),
                    "status": "staged",
                }
                job_id = uuid4().hex
                db.execute("INSERT INTO documents VALUES (?, ?)", (version_id, canonical(metadata)))
                db.execute(
                    "INSERT INTO ingestions(id, version_id, state, created_at) VALUES (?, ?, 'staged', ?)",
                    (job_id, version_id, now()),
                )
            db.execute("INSERT INTO ingestion_keys VALUES (?, ?, ?)", (key, request_hash, job_id))
        return job_id

    def ingestion(self, job_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT ingestions.*, metadata FROM ingestions JOIN documents USING(version_id) WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise ApiError(404, "missing_ingestion", "That staged source could not be found.")
        return self._ingestion(row)

    def catalog(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT ingestions.*, metadata FROM ingestions JOIN documents USING(version_id) ORDER BY created_at DESC, id"
            ).fetchall()
        return [self._ingestion(row, include_extraction=False) for row in rows]

    @staticmethod
    def _ingestion(row: sqlite3.Row, *, include_extraction: bool = True) -> dict[str, Any]:
        return {
            "job_id": row["id"],
            "state": row["state"],
            "document": json.loads(row["metadata"]),
            "failure_code": row["failure_code"],
            "extraction": json.loads(row["extraction"]) if include_extraction and row["extraction"] else None,
        }

    def document(self, version_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT metadata FROM documents WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise ApiError(404, "missing_edition", "The recorded edition could not be found.")
        return json.loads(row["metadata"])

    def original(self, document: dict[str, Any]) -> bytes:
        byte_hash = document["byte_hash"]
        return self._verified_pdf(self.objects / f"{byte_hash}.pdf", byte_hash)

    @staticmethod
    def _verified_pdf(path: Path, byte_hash: str) -> bytes:
        try:
            with path.open("rb") as stream:
                pdf = stream.read(PDF_BYTES + 1)
        except OSError as exc:
            raise ApiError(409, "missing_original", "The recorded original PDF is unavailable.") from exc
        if len(pdf) > PDF_BYTES or digest(pdf) != byte_hash:
            raise ApiError(409, "source_hash_mismatch", "The recorded original PDF failed its integrity check.")
        return pdf
