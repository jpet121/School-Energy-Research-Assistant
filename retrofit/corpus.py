from collections.abc import Iterable
from hashlib import sha256
import json
import re
import sqlite3
from typing import Annotated, Any, Self

from fastapi import APIRouter
from pydantic import Field, StringConstraints, model_validator

from .chunking import CHUNK_PAYLOAD_IDENTITY, Strategy, make_chunk, read_snapshot_chunk, windows
from .contracts import (
    ApiError, Contract, CORPUS_CHUNKS, CORPUS_PAGES, ShortText, Versioned, box_within_page, canonical, fingerprint, now, quote_geometry,
)
from .store import Store
from .embeddings import Embeddings, ModelName, TOKENIZER_HASH, valid_vector


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    manifest TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_documents (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    version_id TEXT NOT NULL REFERENCES documents(version_id),
    PRIMARY KEY(snapshot_id, version_id)
);
CREATE TABLE IF NOT EXISTS snapshot_chunks (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES documents(version_id),
    payload TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, chunk_id)
);
CREATE TABLE IF NOT EXISTS snapshot_vectors (
    snapshot_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY(snapshot_id, chunk_id),
    FOREIGN KEY(snapshot_id,chunk_id) REFERENCES snapshot_chunks(snapshot_id,chunk_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS withdrawals (
    version_id TEXT PRIMARY KEY REFERENCES documents(version_id),
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS corpus_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    active_snapshot_id TEXT REFERENCES snapshots(id),
    eligibility_revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO corpus_state(singleton) VALUES (1);
"""
Opaque = Annotated[str, StringConstraints(min_length=1, max_length=128)]
PdfPageKey = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,2}$")]
SpanList = Annotated[list[Opaque], Field(min_length=1, max_length=100)]


class TableLinks(Contract):
    cell_span_ids: SpanList
    row_header_span_ids: SpanList
    column_header_span_ids: SpanList
    unit_span_ids: SpanList
    mandatory_note_span_ids: Annotated[list[Opaque], Field(max_length=100)]
    notes_absent_reason: ShortText | None = None

    @model_validator(mode="after")
    def notes(self) -> Self:
        if bool(self.mandatory_note_span_ids) == bool(self.notes_absent_reason):
            raise ValueError("Link required note spans or document why the row has no notes")
        return self


class Review(Versioned):
    extraction_config_hash: Opaque
    approved_span_ids: Annotated[list[Opaque], Field(min_length=1, max_length=CORPUS_CHUNKS)]
    table_rows: Annotated[list[TableLinks], Field(max_length=1000)] = []
    normalized_cell_ids: Annotated[list[Opaque], Field(max_length=1000)] = []
    printed_page_labels: Annotated[dict[PdfPageKey, ShortText], Field(max_length=500)] = {}
    qa_note: ShortText


class Prepare(Versioned):
    job_ids: Annotated[list[Opaque], Field(min_length=1, max_length=CORPUS_PAGES)]
    chunking: Strategy = "span"
    embedding_model: ModelName | None = None
    include_vectors: bool = False

    @model_validator(mode="after")
    def declared_model(self) -> Self:
        if (self.chunking != "span" or self.include_vectors) and self.embedding_model is None:
            raise ValueError("This snapshot requires a pinned tokenizer")
        if self.embedding_model and self.chunking == "span":
            raise ValueError("Choose a deterministic chunking strategy for the embedding candidate")
        return self


def current(db: sqlite3.Connection) -> sqlite3.Row:
    return db.execute("SELECT active_snapshot_id,eligibility_revision FROM corpus_state WHERE singleton=1").fetchone()


def fts_table(snapshot_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
        raise ApiError(409, "invalid_snapshot_identity", "The snapshot identity is invalid.")
    return f"fts_{snapshot_id}"


def read_snapshot_manifest(raw: str, snapshot_id: str) -> dict:
    try:
        manifest = json.loads(raw)
        stored_id = manifest.pop("snapshot_id")
        if stored_id != snapshot_id or fingerprint(manifest) != snapshot_id:
            raise ValueError("Stored manifest does not match its identity")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ApiError(409, "invalid_snapshot_identity", "The recorded snapshot manifest has changed.") from exc
    return {**manifest, "snapshot_id": stored_id}


def require_snapshot_chunk_set(db: sqlite3.Connection, manifest: dict) -> None:
    if manifest.get("chunk_payload_identity") == CHUNK_PAYLOAD_IDENTITY:
        ids = [row["chunk_id"] for row in db.execute(
            "SELECT chunk_id FROM snapshot_chunks WHERE snapshot_id=? ORDER BY chunk_id", (manifest["snapshot_id"],),
        )]
        if fingerprint(ids) != manifest["chunk_set_hash"]:
            raise ApiError(409, "incomplete_snapshot", "The recorded snapshot contains a different set of chunks.")


def vector_hash(vectors: list[tuple[str, bytes]]) -> str:
    checksum = sha256()
    for chunk_id, vector in sorted(vectors):
        checksum.update(chunk_id.encode("ascii"))
        checksum.update(vector)
    return checksum.hexdigest()


def verify_decimal_spacing(span: dict) -> dict:
    text = span["normalized_text"]
    removed: set[int] = set()
    for match in re.finditer(r"(?<=\d)[.\s]+(?=\d)", text):
        dots = [index for index in range(match.start(), match.end()) if text[index] == "."]
        if not dots:
            continue
        if len(dots) > 1:
            positions = [span["normalization_map"][index] for index in dots]
            if any(index is None for index in positions):
                continue
            boxes = [span["raw_character_boxes"][index] for index in positions]
            if any(box != boxes[0] for box in boxes[1:]):
                continue
        removed.update(index for index in range(match.start(), match.end()) if index != dots[0])
    if not removed:
        raise ApiError(400, "no_decimal_spacing_to_normalize", "The selected table cell has no separated decimal to normalize.")
    corrected = "".join(char for index, char in enumerate(text) if index not in removed)
    return {
        **span, "span_id": fingerprint([span["span_id"], "verified_table_decimal_spacing", corrected]),
        "normalized_text": corrected,
        "normalization_map": [entry for index, entry in enumerate(span["normalization_map"]) if index not in removed],
        "verified_normalization": {"rule": "verified_table_decimal_spacing", "original_span_id": span["span_id"], "prior_text": text},
    }


class Corpus:
    def __init__(self, store: Store, embeddings: Embeddings) -> None:
        self.store = store
        self.embeddings = embeddings
        with store.connection() as db:
            db.executescript(SCHEMA)

    def _require_mapped_spans(self, job: dict, spans: Iterable[dict]) -> None:
        for span in spans:
            text = span["normalized_text"]
            geometry = quote_geometry(span, 0, len(text), text)
            if geometry and geometry == span["bounding_boxes"] and all(
                box_within_page(box, span["page_width"], span["page_height"]) for box in geometry
            ):
                continue
            with self.store.connection(write=True) as db:
                row = db.execute("SELECT state,extraction FROM ingestions WHERE id=?", (job["job_id"],)).fetchone()
                if row["state"] == job["state"] and row["extraction"] and json.loads(row["extraction"]) == job["extraction"]:
                    db.execute(
                        "UPDATE ingestions SET state='failed',extraction=NULL,failure_code='invalid_span_geometry' WHERE id=?",
                        (job["job_id"],),
                    )
            raise ApiError(409, "invalid_span_geometry", "A selected span has an invalid original-page mapping. Reopen the staged source and extract again.")

    def review(self, job_id: str, review: Review) -> dict:
        job = self.store.ingestion(job_id)
        extracted = job["extraction"]
        if job["state"] not in {"review_required", "ready"} or not extracted:
            raise ApiError(409, "staging_incomplete", "This PDF has no complete extraction to review.")
        if review.extraction_config_hash != extracted["config_hash"]:
            raise ApiError(409, "extraction_changed", "Open the current extraction before recording review.")
        admitted_pages = {str(page["pdf_page_1based"]) for page in extracted["pages"] if page["within_use_scope"]}
        if not review.printed_page_labels.keys() <= admitted_pages:
            raise ApiError(400, "invalid_review_pages", "Verify printed labels only for admitted PDF pages.")
        candidates = {
            span["span_id"]: span
            for page in extracted["pages"] if page["within_use_scope"]
            for span in page["spans"] if not span.get("excluded_reason")
        }
        approved = set(review.approved_span_ids)
        if len(approved) != len(review.approved_span_ids) or not approved <= candidates.keys():
            raise ApiError(400, "invalid_review_spans", "Select unique spans within the admitted page ranges.")
        self._require_mapped_spans(job, (candidates[sid] for sid in approved))
        rows: list[dict] = []
        cells: set[str] = set()
        for links in review.table_rows:
            required = list(dict.fromkeys([
                *links.cell_span_ids, *links.row_header_span_ids, *links.column_header_span_ids,
                *links.unit_span_ids, *links.mandatory_note_span_ids,
            ]))
            if not set(required) <= approved or cells.intersection(links.cell_span_ids):
                raise ApiError(400, "invalid_table_links", "A row requires approved cells, headers, units and all linked notes.")
            if len({candidates[sid]["pdf_page_1based"] for sid in required}) != 1:
                raise ApiError(400, "cross_page_table", "Cross-page table reconstruction is outside this workbench. Quarantine the row.")
            body = links.model_dump()
            row_id = fingerprint([job["document"]["version_id"], body])
            primary = [candidates[sid] for sid in links.cell_span_ids]
            detected_rows = {(span.get("table_candidate"), span.get("candidate_row")) for span in primary}
            if any(table for table, _ in detected_rows) and len(detected_rows) != 1:
                raise ApiError(400, "unrelated_table_cells", "Select value cells from one detected row.")
            table_id = fingerprint([
                job["document"]["version_id"], primary[0]["pdf_page_1based"],
                primary[0].get("table_candidate"), links.column_header_span_ids,
            ])
            rows.append({"row_id": row_id, "table_id": table_id, **body, "ordered_span_ids": required})
            cells.update(links.cell_span_ids)
        if any(candidates[sid]["requires_review"] and sid not in cells for sid in approved):
            # Additional related table spans can establish context for otherwise ambiguous lines.
            represented = {sid for row in rows for sid in row["ordered_span_ids"]}
            if any(candidates[sid]["requires_review"] and sid not in represented for sid in approved):
                raise ApiError(400, "ambiguous_span", "Quarantine unverified lines or review them with their full table context.")
        approved_in_order = [sid for sid in candidates if sid in approved]
        normalized = set(review.normalized_cell_ids)
        if not normalized <= cells:
            raise ApiError(400, "normalization_outside_table", "Only verified table value cells can receive decimal normalization.")
        normalizations = {sid: verify_decimal_spacing(candidates[sid]) for sid in normalized}
        reviewed = {**extracted, "review": {
            "approved_span_ids": approved_in_order, "table_rows": rows, "qa_note": review.qa_note, "recorded_at": now(),
            "normalizations": normalizations,
            "printed_page_labels": review.printed_page_labels,
        }}
        with self.store.connection(write=True) as db:
            # An overlapping review cannot replace the reviewed version silently.
            row = db.execute("SELECT extraction,state FROM ingestions WHERE id=?", (job_id,)).fetchone()
            if row["state"] != job["state"] or json.loads(row["extraction"]) != extracted:
                raise ApiError(409, "review_changed", "The review changed. Refresh it before saving.")
            db.execute("UPDATE ingestions SET state='ready',extraction=? WHERE id=?", (canonical(reviewed), job_id))
        return {"job_id": job_id, "state": "ready", "approved_count": len(approved), "table_row_count": len(rows)}

    def prepare(self, request: Prepare) -> dict:
        tokenizer = self.embeddings.tokenizer(request.embedding_model) if request.embedding_model else None
        if len(set(request.job_ids)) != len(request.job_ids):
            raise ApiError(400, "duplicate_edition", "Select each reviewed edition once.")
        jobs = [self.store.ingestion(job_id) for job_id in sorted(request.job_ids)]
        if any(job["state"] != "ready" for job in jobs):
            raise ApiError(409, "staging_incomplete", "Only reviewed editions can be included in a snapshot.")
        page_count = sum(job["extraction"]["page_count"] for job in jobs)
        if page_count > CORPUS_PAGES:
            raise ApiError(413, "corpus_page_limit", "A collection may contain at most 1,000 PDF pages.")
        documents = []
        chunks = []
        table_count = 0
        for job in jobs:
            document = job["document"]
            self.store.original(document)
            version = document["version_id"]
            extracted = job["extraction"]
            review = extracted["review"]
            spans = {span["span_id"]: span for page in extracted["pages"] for span in page["spans"]}
            spans.update(review.get("normalizations", {}))
            self._require_mapped_spans(job, (spans[sid] for sid in review["approved_span_ids"]))
            labels = review.get("printed_page_labels", {})
            for original_id, span in spans.items():
                label = labels.get(str(span["pdf_page_1based"]))
                if label != span["printed_page_label"]:
                    spans[original_id] = {
                        **span, "printed_page_label": label,
                        "span_id": fingerprint([span["span_id"], "reviewed_printed_page_label", label]),
                    }
            cells = {sid for row in review["table_rows"] for sid in row["cell_span_ids"]}
            prefix = f"{document['title']} — {document['scope']}\n" if tokenizer else ""
            for row in review["table_rows"]:
                links = {
                    key: [spans[sid]["span_id"] for sid in value] if key.endswith("_span_ids") else value
                    for key, value in row.items()
                }
                if row["ordered_span_ids"] != links["ordered_span_ids"]:
                    links["row_id"] = fingerprint([row["row_id"], links["ordered_span_ids"]])
                chunks.append((version, make_chunk(
                    [spans[sid] for sid in row["ordered_span_ids"]], version,
                    row=links, tokenizer=tokenizer, prefix=prefix, strategy=request.chunking,
                )))
            table_count += len(review["table_rows"])
            ordinary = [
                spans[sid] for sid in review["approved_span_ids"]
                if sid not in cells and not spans[sid].get("table_candidate") and not spans[sid]["requires_review"]
            ]
            windows_for_document = windows(ordinary, request.chunking, tokenizer, prefix) if tokenizer else [
                (span["normalized_text"], [span]) for span in ordinary
            ]
            for text, segment in windows_for_document:
                chunks.append((version, make_chunk(
                    segment, version, text=text, tokenizer=tokenizer, prefix=prefix, strategy=request.chunking,
                )))
            documents.append({"version_id": version, "byte_hash": document["byte_hash"], "extraction_config_hash": extracted["config_hash"]})
        if not chunks:
            raise ApiError(409, "empty_snapshot", "A snapshot needs at least one verified evidence span.")
        if len(chunks) > CORPUS_CHUNKS:
            raise ApiError(413, "corpus_chunk_limit", "A snapshot may contain at most 10,000 chunks.")
        embedding = None
        vectors: list[tuple[str, bytes]] = []
        if request.include_vectors and request.embedding_model:
            embedding = self.embeddings.config(request.embedding_model)
            vectors = [
                (chunk["chunk_id"], self.embeddings.encode(request.embedding_model, chunk["embedding_text"]))
                for _, chunk in chunks
            ]
        manifest = {
            "schema_version": "1.1", "documents": documents, "page_count": page_count,
            "chunk_count": len(chunks), "table_row_count": table_count,
            "chunk_set_hash": fingerprint(sorted(chunk["chunk_id"] for _, chunk in chunks)),
            "chunk_payload_identity": CHUNK_PAYLOAD_IDENTITY,
            "chunking": {
                "strategy": request.chunking, "tokenizer_sha256": TOKENIZER_HASH if tokenizer else None,
                "maximum_input_tokens": max(chunk["embedding_token_count"] for _, chunk in chunks) if tokenizer else None,
            },
            "embedding": embedding, "enabled_modes": ["lexical", "dense", "hybrid"] if embedding else ["lexical"],
            "vector_set_sha256": vector_hash(vectors) if vectors else None,
            "retrieval_settings": {
                "lexical": "SQLite FTS5 unicode61 BM25", "candidate_limit": 20, "final_bundle_limit": 8,
                "dense": "NumPy exact cosine" if embedding else None, "rrf_constant": 60 if embedding else None,
            },
        }
        snapshot_id = fingerprint(manifest)
        manifest["snapshot_id"] = snapshot_id
        with self.store.connection(write=True) as db:
            existing = db.execute("SELECT published_at FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if existing is not None:
                return {**manifest, "published_at": existing["published_at"]}
            db.execute("INSERT INTO snapshots(id,manifest) VALUES (?,?)", (snapshot_id, canonical(manifest)))
            db.executemany("INSERT INTO snapshot_documents VALUES (?,?)", [(snapshot_id, doc["version_id"]) for doc in documents])
            db.executemany("INSERT INTO snapshot_chunks VALUES (?,?,?,?)", [
                (snapshot_id, chunk["chunk_id"], version, canonical(chunk)) for version, chunk in chunks
            ])
            fts = fts_table(snapshot_id)
            db.execute(f"CREATE VIRTUAL TABLE {fts} USING fts5(chunk_id UNINDEXED, body, tokenize='unicode61')")
            db.executemany(f"INSERT INTO {fts} VALUES (?,?)", [
                (chunk["chunk_id"], chunk["normalized_text"]) for _, chunk in chunks
            ])
            db.executemany("INSERT INTO snapshot_vectors VALUES (?,?,?)", [
                (snapshot_id, chunk_id, vector) for chunk_id, vector in vectors
            ])
        return {**manifest, "published_at": None}

    def snapshots(self) -> dict:
        with self.store.connection() as db:
            state = current(db)
            snapshots = [
                {**json.loads(row["manifest"]), "published_at": row["published_at"]}
                for row in db.execute("SELECT manifest,published_at FROM snapshots ORDER BY rowid DESC")
            ]
        return {**dict(state), "snapshots": snapshots}

    def inventory(self, snapshot_id: str) -> dict:
        with self.store.connection() as db:
            db.execute("BEGIN")
            snapshot = db.execute("SELECT manifest,published_at FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if snapshot is None or not snapshot["published_at"]:
                raise ApiError(404, "missing_snapshot", "Evaluation requires a published snapshot.")
            manifest = read_snapshot_manifest(snapshot["manifest"], snapshot_id)
            require_snapshot_chunk_set(db, manifest)
            state = current(db)
            rows = db.execute(
                """SELECT chunk_id,version_id,payload FROM snapshot_chunks c WHERE snapshot_id=?
                   AND NOT EXISTS (SELECT 1 FROM withdrawals w WHERE w.version_id=c.version_id)
                   ORDER BY chunk_id""", (snapshot_id,),
            )
            spans: dict[str, dict] = {}
            tables: dict[str, dict] = {}
            for row in rows:
                chunk = read_snapshot_chunk(row["payload"], row["chunk_id"], row["version_id"], manifest.get("chunk_payload_identity"))
                spans.update((span["span_id"], span) for span in chunk["spans"])
                if chunk["table_row"]:
                    table = chunk["table_row"]
                    tables[table["row_id"]] = table
        return {
            "schema_version": "1.1", "snapshot_id": snapshot_id, "eligibility_revision": state["eligibility_revision"],
            "documents": manifest["documents"],
            "spans": list(spans.values()), "table_rows": list(tables.values()),
        }

    def publish(self, snapshot_id: str, *, restore: bool = False) -> dict:
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT id,manifest,published_at FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise ApiError(404, "missing_snapshot", "That complete snapshot could not be found.")
            if restore and row["published_at"] is None:
                raise ApiError(409, "snapshot_not_published", "Restore requires a previously published snapshot.")
            manifest = read_snapshot_manifest(row["manifest"], snapshot_id)
            complete = db.execute("SELECT COUNT(*) FROM snapshot_chunks WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0]
            indexed = db.execute(f"SELECT COUNT(*) FROM {fts_table(row['id'])}").fetchone()[0]
            if not complete or complete != indexed or complete != manifest["chunk_count"]:
                raise ApiError(409, "incomplete_snapshot", "The candidate snapshot is incomplete. The active collection was preserved.")
            require_snapshot_chunk_set(db, manifest)
            for item in db.execute("SELECT chunk_id,version_id,payload FROM snapshot_chunks WHERE snapshot_id=?", (snapshot_id,)):
                read_snapshot_chunk(item["payload"], item["chunk_id"], item["version_id"], manifest.get("chunk_payload_identity"))
            embedding = manifest["embedding"]
            if embedding:
                vectors = [(row["chunk_id"], row["vector"]) for row in db.execute(
                    "SELECT chunk_id,vector FROM snapshot_vectors WHERE snapshot_id=?", (snapshot_id,),
                )]
                if (
                    len(vectors) != complete or vector_hash(vectors) != manifest["vector_set_sha256"]
                    or self.embeddings.config(embedding["key"]) != embedding
                ):
                    raise ApiError(409, "embedding_config_mismatch", "The vector index fails its recorded manifest checks.")
                for _, data in vectors:
                    valid_vector(data, embedding["dimension"])
            for doc in manifest["documents"]:
                document = self.store.document(doc["version_id"])
                if document["byte_hash"] != doc["byte_hash"]:
                    raise ApiError(409, "snapshot_source_changed", "A recorded edition changed. The active collection was preserved.")
                self.store.original(document)
            published_at = row["published_at"] or now()
            db.execute("UPDATE snapshots SET published_at=? WHERE id=?", (published_at, snapshot_id))
            db.execute("UPDATE corpus_state SET active_snapshot_id=? WHERE singleton=1", (snapshot_id,))
        return {"active_snapshot_id": snapshot_id, "manifest": {**manifest, "published_at": published_at}}


def corpus_routes(corpus: Corpus) -> APIRouter:
    router = APIRouter()
    router.add_api_route("/ingestions/{job_id}/review", corpus.review, methods=["POST"])
    router.add_api_route("/snapshots", corpus.prepare, methods=["POST"], status_code=201)
    router.add_api_route("/snapshots", corpus.snapshots, methods=["GET"])
    router.add_api_route("/snapshots/{snapshot_id}/evaluation-inventory", corpus.inventory, methods=["GET"])
    @router.get("/embedding-models")
    def embedding_models() -> dict:
        return {"models": corpus.embeddings.configured()}

    @router.post("/snapshots/{snapshot_id}/publish")
    def publish(snapshot_id: str) -> dict:
        return corpus.publish(snapshot_id)

    @router.post("/snapshots/{snapshot_id}/restore")
    def restore(snapshot_id: str) -> dict:
        return corpus.publish(snapshot_id, restore=True)

    return router
