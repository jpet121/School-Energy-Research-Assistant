from concurrent.futures import ThreadPoolExecutor
import json
import re
import sqlite3
from time import monotonic
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Response
from pydantic import Field, StringConstraints
import numpy as np

from .chunking import read_snapshot_chunk, required_qualifiers
from .contracts import ApiError, QUESTION_CHARS, Versioned, canonical, fingerprint, now
from .corpus import Opaque, current, fts_table, read_snapshot_manifest, require_snapshot_chunk_set
from .embeddings import Embeddings, valid_vector
from .extraction import Extraction
from .store import Store


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT REFERENCES snapshots(id),
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_query ON runs(status) WHERE status='pending';
"""
Mode = Literal["lexical", "dense", "hybrid"]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=QUESTION_CHARS)]
Context = Annotated[str, StringConstraints(max_length=QUESTION_CHARS)]


class Query(Versioned):
    question: Question
    context: Context = ""
    mode: Mode
    snapshot_id: Opaque | None = None
    version_ids: Annotated[list[Opaque], Field(max_length=1000)] = []


def withdrawn(db: sqlite3.Connection, version_id: str) -> dict | None:
    row = db.execute("SELECT reason, recorded_at, revision FROM withdrawals WHERE version_id=?", (version_id,)).fetchone()
    return dict(row) if row else None


def lexical_ranks(
    db: sqlite3.Connection, snapshot_id: str, question: str, version_ids: list[str], limit: int = 20,
) -> list[tuple[str, str, float]]:
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", question.casefold())))
    if not terms:
        return []
    match = " OR ".join(f'"{term}"' for term in terms)
    fts = fts_table(snapshot_id)
    filter_sql = ""
    args: list = [snapshot_id, match]
    if version_ids:
        filter_sql = " AND c.version_id IN (" + ",".join("?" for _ in version_ids) + ")"
        args.extend(version_ids)
    rows = db.execute(
        f"""
        SELECT c.chunk_id,c.version_id,bm25({fts}) AS distance FROM {fts}
        JOIN snapshot_chunks c ON c.snapshot_id=? AND c.chunk_id={fts}.chunk_id
        WHERE {fts} MATCH ?
          AND NOT EXISTS (SELECT 1 FROM withdrawals w WHERE w.version_id=c.version_id)
        """ + filter_sql + " ORDER BY distance,c.chunk_id LIMIT ?", (*args, limit),
    )
    return [(row["chunk_id"], row["version_id"], float(-row["distance"])) for row in rows]


def make_bundle(run_id: str, chunk: dict, version_id: str, eligibility_revision: int) -> dict:
    span_ids = chunk["source_span_ids"]
    return {
        "bundle_id": fingerprint([run_id, chunk["chunk_id"], eligibility_revision]),
        "anchor_chunk_id": chunk["chunk_id"], "version_id": version_id,
        "ordered_span_ids": span_ids, "mandatory_qualifier_span_ids": required_qualifiers(chunk["table_row"]),
        "rendered_context": chunk["rendered_context"], "generator_token_count": None,
        "deduplication_key": fingerprint(span_ids), "eligibility_revision": eligibility_revision,
        "spans": chunk["spans"], "table_row": chunk["table_row"],
    }


class Research:
    def __init__(self, store: Store, extraction: Extraction, embeddings: Embeddings) -> None:
        self.store = store
        self.extraction = extraction
        self.embeddings = embeddings
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retrofit-retrieval")
        with store.connection() as db:
            db.executescript(SCHEMA)
        with store.connection(write=True) as db:
            for row in db.execute("SELECT id,payload FROM runs WHERE status='pending'").fetchall():
                payload = json.loads(row["payload"])
                payload["status"] = "error"
                payload["failure_code"] = "interrupted"
                payload["attempts"].append({"status": "interrupted", "finished_at": now()})
                db.execute("UPDATE runs SET status='error',payload=? WHERE id=?", (canonical(payload), row["id"]))

    def start(self, query: Query) -> dict:
        run_id = uuid4().hex
        with self.store.connection(write=True) as db:
            if db.execute("SELECT 1 FROM runs WHERE status='pending'").fetchone() or db.execute(
                "SELECT 1 FROM generation_ops WHERE state='pending'"
            ).fetchone():
                raise ApiError(429, "query_busy", "One query is already active. Wait or cancel the current search.")
            state = current(db)
            snapshot_id = query.snapshot_id or state["active_snapshot_id"]
            if not snapshot_id:
                raise ApiError(409, "no_published_snapshot", "Publish a complete evidence snapshot before searching.")
            row = db.execute("SELECT manifest,published_at FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if row is None or not row["published_at"]:
                raise ApiError(409, "snapshot_unavailable", "Choose a published evidence snapshot.")
            manifest = read_snapshot_manifest(row["manifest"], snapshot_id)
            known_versions = {doc["version_id"] for doc in manifest["documents"]}
            if len(query.version_ids) != len(set(query.version_ids)) or not set(query.version_ids) <= known_versions:
                raise ApiError(400, "invalid_filter", "Choose unique source editions in the recorded snapshot.")
            payload = {
                "schema_version": "1.1", "run_id": run_id, "status": "pending", "snapshot_id": snapshot_id,
                "snapshot_manifest": {**manifest, "published_at": row["published_at"]},
                "requested_at": now(), "eligibility_revision": state["eligibility_revision"],
                "input_filter_hash": fingerprint(query.model_dump(mode="json")),
                "question": query.question, "context": query.context, "mode_requested": query.mode,
                "mode_used": None, "version_ids": query.version_ids, "ranked_candidates": [],
                "final_bundle_ids": [], "bundles": [], "result_kind": None,
                "prompt_hash": None, "model_hash": None, "null_reason": "Local retrieval without generation",
                "retrieval_config_hash": fingerprint(manifest["retrieval_settings"]),
                "hosted_input_confirmation": None, "attempts": [],
                "usage": None, "price_version": None, "cost_estimate": None, "cost_actual": None,
                "cost_status": "no_hosted_dispatch",
            }
            db.execute("INSERT INTO runs VALUES (?,?,'pending',?)", (run_id, snapshot_id, canonical(payload)))
        self.pool.submit(self._execute, run_id, payload)
        return {"run_id": run_id, "status": "pending"}

    def rank(self, db: sqlite3.Connection, payload: dict) -> list[tuple[str, str, float]]:
        mode = payload["mode_requested"]
        manifest = payload["snapshot_manifest"]
        snapshot_id, question, version_ids = manifest["snapshot_id"], payload["question"], payload["version_ids"]
        if mode == "lexical":
            return lexical_ranks(db, snapshot_id, question, version_ids)
        config = manifest["embedding"]
        if not config:
            raise ApiError(503, "embedding_unavailable", "That retrieval mode is unavailable. Lexical search remains available.")
        model = config["key"]
        if config != self.embeddings.config(model):
            raise ApiError(409, "embedding_config_mismatch", "The query model does not match the pinned vector index.")
        vector = valid_vector(self.embeddings.encode(model, question, query=True), config["dimension"])
        rows = db.execute(
            """
            SELECT c.chunk_id,c.version_id,v.vector FROM snapshot_vectors v
            JOIN snapshot_chunks c USING(snapshot_id,chunk_id)
            WHERE v.snapshot_id=? AND NOT EXISTS (SELECT 1 FROM withdrawals w WHERE w.version_id=c.version_id)
            """, (snapshot_id,),
        )
        chunks = [row for row in rows if not version_ids or row["version_id"] in version_ids]
        dense = []
        if chunks:
            matrix = np.stack([valid_vector(row["vector"], config["dimension"]) for row in chunks])
            scores = matrix @ vector
            dense = sorted(
                ((row["chunk_id"], row["version_id"], float(score)) for row, score in zip(chunks, scores)),
                key=lambda item: (-item[2], item[0]),
            )[:20]
        if mode == "dense":
            return dense
        fusion: dict[tuple[str, str], float] = {}
        for ranks in (lexical_ranks(db, snapshot_id, question, version_ids), dense):
            for rank, (chunk_id, version_id, _) in enumerate(ranks, 1):
                key = (chunk_id, version_id)
                fusion[key] = fusion.get(key, 0.0) + 1 / (60 + rank)
        return sorted(((cid, vid, score) for (cid, vid), score in fusion.items()), key=lambda item: (-item[2], item[0]))[:20]

    def _execute(self, run_id: str, payload: dict) -> None:
        started = monotonic()
        try:
            with self.store.connection() as db:
                ranked = self.rank(db, payload)
            with self.store.connection(write=True) as db:
                row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                if row["status"] != "pending":
                    return
                require_snapshot_chunk_set(db, payload["snapshot_manifest"])
                state = current(db)
                payload["eligibility_revision"] = state["eligibility_revision"]
                ranked = [(cid, vid, score) for cid, vid, score in ranked if withdrawn(db, vid) is None]
                payload["ranked_candidates"] = [
                    {"chunk_id": cid, "version_id": vid, "score": score} for cid, vid, score in ranked
                ]
                bundles = []
                seen: set[str] = set()
                incomplete_coverage = False
                for chunk_id, version_id, _ in ranked:
                    chunk_row = db.execute(
                        "SELECT COALESCE((SELECT payload FROM snapshot_chunks WHERE snapshot_id=? AND chunk_id=?),'') AS payload",
                        (payload["snapshot_id"], chunk_id),
                    ).fetchone()
                    try:
                        chunk = read_snapshot_chunk(
                            chunk_row["payload"], chunk_id, version_id,
                            payload["snapshot_manifest"].get("chunk_payload_identity"),
                        )
                    except ApiError:
                        incomplete_coverage = True
                        continue
                    bundle = make_bundle(run_id, chunk, version_id, payload["eligibility_revision"])
                    key = bundle["deduplication_key"]
                    if key in seen:
                        continue
                    seen.add(key)
                    bundles.append(bundle)
                    if len(bundles) == 8:
                        break
                payload.update({
                    "status": "result", "mode_used": payload["mode_requested"],
                    "result_kind": "evidence" if bundles else "no_evidence",
                    "incomplete_coverage": incomplete_coverage,
                    "bundles": bundles, "final_bundle_ids": [bundle["bundle_id"] for bundle in bundles],
                    "attempts": [{"status": "result", "finished_at": now(), "elapsed_ms": round((monotonic() - started) * 1000, 2)}],
                })
                db.execute("UPDATE runs SET status='result',payload=? WHERE id=?", (canonical(payload), run_id))
        except (ApiError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
            payload.update({
                "status": "error", "failure_code": exc.code if isinstance(exc, ApiError) else "retrieval_failed",
                "attempts": [{"status": "error", "finished_at": now(), "elapsed_ms": round((monotonic() - started) * 1000, 2)}],
            })
            with self.store.connection(write=True) as db:
                db.execute("UPDATE runs SET status='error',payload=? WHERE id=? AND status='pending'", (canonical(payload), run_id))

    @staticmethod
    def _verify_saved_bundles(db: sqlite3.Connection, run_id: str, record: dict) -> None:
        saved_bundles = record["bundles"]
        if saved_bundles == [] and record["final_bundle_ids"] == []:
            return
        snapshot_id = record["snapshot_id"]
        row = db.execute("SELECT manifest FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise ApiError(409, "snapshot_unavailable", "The saved query's evidence snapshot is unavailable.")
        manifest = read_snapshot_manifest(row["manifest"], snapshot_id)
        require_snapshot_chunk_set(db, manifest)
        valid = []
        for saved in saved_bundles if isinstance(saved_bundles, list) else ():
            if not isinstance(saved, dict) or not isinstance(chunk_id := saved.get("anchor_chunk_id"), str):
                continue
            chunk_row = db.execute(
                "SELECT version_id,payload FROM snapshot_chunks WHERE snapshot_id=? AND chunk_id=?",
                (snapshot_id, chunk_id),
            ).fetchone()
            if chunk_row is None:
                continue
            version = chunk_row["version_id"]
            try:
                chunk = read_snapshot_chunk(chunk_row["payload"], chunk_id, version, manifest.get("chunk_payload_identity"))
            except ApiError:
                continue
            if saved == make_bundle(run_id, chunk, version, record["eligibility_revision"]):
                valid.append(saved)
        bundle_ids = [bundle["bundle_id"] for bundle in valid]
        if valid != saved_bundles or bundle_ids != record["final_bundle_ids"]:
            record.update(
                bundles=valid, final_bundle_ids=bundle_ids,
                result_kind="evidence" if valid else "no_evidence", incomplete_coverage=True,
            )

    def get(self, run_id: str, *, historical: bool = False) -> dict:
        with self.store.connection() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ApiError(404, "missing_run", "That recorded query could not be found.")
            record = json.loads(row["payload"])
            self._verify_saved_bundles(db, run_id, record)
            if not historical:
                bundles = [bundle for bundle in record["bundles"] if withdrawn(db, bundle["version_id"]) is None]
                if len(bundles) != len(record["bundles"]):
                    record.update({
                        "bundles": bundles, "final_bundle_ids": [bundle["bundle_id"] for bundle in bundles],
                        "result_kind": "evidence" if bundles else "no_evidence", "eligibility_changed": True,
                    })
                operations = [json.loads(item["payload"]) for item in db.execute(
                    "SELECT payload FROM generation_ops WHERE run_id=? ORDER BY rowid", (run_id,),
                )]
                denials = [dict(item) for item in db.execute(
                    "SELECT code,recorded_at FROM dispatch_denials WHERE run_id=? ORDER BY rowid", (run_id,),
                )]
                record["hosted_operations"] = [item["operation_id"] for item in operations]
                record["hosted_dispatch_denials"] = denials
                if operations:
                    latest = operations[-1]
                    record.update({
                        "hosted_input_confirmation": {
                            "confirmation_id": latest["confirmation_id"], "confirmed_at": latest["confirmed_at"],
                            "question": record["question"], "context": record["context"],
                        },
                        "prompt_hash": latest["prompt_hash"], "model_hash": latest["model_hash"],
                        "price_version": latest["provider"]["price_version"],
                    })
                    provider_attempts = [json.loads(item["payload"]) for item in db.execute(
                        "SELECT payload FROM provider_attempts WHERE operation_id=? ORDER BY attempt", (latest["operation_id"],),
                    )]
                    for item in provider_attempts:
                        item.pop("restricted_output", None)
                    if provider_attempts:
                        record["provider_attempts"] = provider_attempts
                        record["usage"] = [
                            {"input_tokens": item.get("usage_input"), "output_tokens": item.get("usage_output")}
                            for item in provider_attempts
                        ]
                        if all(item.get("estimated_nanos") is not None for item in provider_attempts):
                            record["cost_estimate"] = sum(item["estimated_nanos"] for item in provider_attempts)
                        if all(item.get("actual_nanos") is not None for item in provider_attempts):
                            record["cost_actual"] = sum(item["actual_nanos"] for item in provider_attempts)
                        record["cost_status"] = (
                            "unresolved_reservation_held" if any(item.get("actual_nanos") is None for item in provider_attempts)
                            else "reconciled_or_not_dispatched"
                        )
                    counts = latest.get("generator_bundle_tokens", {})
                    for bundle in record["bundles"]:
                        bundle["generator_token_count"] = counts.get(bundle["bundle_id"])
        return record

    def evidence(self, run_id: str, bundle_id: str) -> dict:
        record = self.get(run_id, historical=True)
        bundle = next((bundle for bundle in record["bundles"] if bundle["bundle_id"] == bundle_id), None)
        if bundle is None:
            raise ApiError(404, "missing_bundle", "That evidence was not returned in the recorded query.")
        document = self.store.document(bundle["version_id"])
        self.store.original(document)
        with self.store.connection() as db:
            warning = withdrawn(db, bundle["version_id"])
        return {**bundle, "run_id": run_id, "document": document, "withdrawal": warning, "eligible_for_new_use": warning is None}

    def evidence_page(self, run_id: str, bundle_id: str, span_id: str) -> Response:
        bundle = self.evidence(run_id, bundle_id)
        span = next((span for span in bundle["spans"] if span["span_id"] == span_id), None)
        if span is None:
            raise ApiError(404, "missing_span", "That page region does not belong to the recorded evidence.")
        return Response(self.extraction.render(bundle["document"], span["pdf_page_1based"]), media_type="image/png")

    def cancel(self, run_id: str) -> dict:
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT status,payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ApiError(404, "missing_run", "That recorded query could not be found.")
            state = row["status"]
            if state == "pending":
                record = json.loads(row["payload"])
                record["status"] = "canceled"
                record["attempts"].append({"status": "canceled", "finished_at": now()})
                db.execute("UPDATE runs SET status='canceled',payload=? WHERE id=?", (canonical(record), run_id))
                state = "canceled"
            if state != "canceled":
                state = "already_completed"
        return {"operation_id": run_id, "state": state}


def research_routes(research: Research) -> APIRouter:
    router = APIRouter()
    router.add_api_route("/queries", research.start, methods=["POST"], status_code=202)
    @router.get("/queries/{run_id}")
    def query_status(run_id: str) -> dict:
        return research.get(run_id)

    router.add_api_route("/queries/{run_id}/cancel", research.cancel, methods=["POST"])
    router.add_api_route("/runs/{run_id}/evidence/{bundle_id}", research.evidence, methods=["GET"])
    router.add_api_route("/runs/{run_id}/evidence/{bundle_id}/pages/{span_id}", research.evidence_page, methods=["GET"])
    return router
