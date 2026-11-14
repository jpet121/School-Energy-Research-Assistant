import json
import re
import sqlite3
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Response
from pydantic import Field, StringConstraints, model_validator

from .contracts import ApiError, Contract, Versioned, canonical, fingerprint, now, quote_geometry
from .corpus import Opaque, current
from .generation import Generation
from .research import Research, withdrawn
from .store import Store


SCHEMA = """
CREATE TABLE IF NOT EXISTS briefs (
    id TEXT PRIMARY KEY,
    latest_revision_id TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL REFERENCES briefs(id),
    parent_revision_id TEXT REFERENCES revisions(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES revisions(id),
    format TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
"""
Format = Literal["markdown", "json"]
DRAFT_EXPORT_LABEL = "Draft — verify evidence"
DraftText = Annotated[str, StringConstraints(strip_whitespace=False, max_length=128_000)]


class Attachment(Contract):
    run_id: Opaque
    bundle_id: Opaque


class Edit(Versioned):
    brief_id: Opaque | None = None
    parent_revision_id: Opaque | None = None
    text: DraftText | None = None
    attach: Attachment | None = None
    generated_operation_id: Opaque | None = None
    detach_claim_ids: Annotated[list[Opaque], Field(max_length=200)] = []
    restore_revision_id: Opaque | None = None

    @model_validator(mode="after")
    def edit_type(self):
        if bool(self.brief_id) != bool(self.parent_revision_id):
            raise ValueError("Brief and parent must be supplied together")
        if self.restore_revision_id:
            if not self.brief_id or self.text is not None or self.attach or self.detach_claim_ids or self.generated_operation_id:
                raise ValueError("A restore is a separate draft operation")
        elif self.text is None:
            raise ValueError("Draft text is required")
        if self.attach and self.generated_operation_id:
            raise ValueError("Choose one kind of evidence attachment")
        return self


class ExportRequest(Versioned):
    format: Format
    revision_id: Opaque | None = None


def verified_revision(row: sqlite3.Row) -> dict:
    try:
        revision = json.loads(row["payload"])
        identity = revision.pop("revision_id")
        if identity != row["id"] or fingerprint(revision) != identity:
            raise ValueError("Revision identity mismatch")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ApiError(409, "invalid_saved_revision", "The saved draft no longer matches its recorded revision identity.") from exc
    return {**revision, "revision_id": identity}


def load_revision(db: sqlite3.Connection, brief_id: str, revision_id: str | None) -> dict:
    if revision_id is None:
        row = db.execute(
            "SELECT r.id,r.payload FROM briefs b JOIN revisions r ON b.latest_revision_id=r.id AND r.brief_id=b.id WHERE b.id=?",
            (brief_id,),
        ).fetchone()
    else:
        row = db.execute("SELECT id,payload FROM revisions WHERE brief_id=? AND id=?", (brief_id, revision_id)).fetchone()
    if row is None:
        raise ApiError(404, "missing_revision", "That saved draft revision could not be found.")
    return verified_revision(row)


def fenced(text: str) -> str:
    width = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0) + 1
    fence = "`" * max(3, width)
    return f"{fence}text\n{text}\n{fence}"


class Briefs:
    def __init__(self, store: Store, research: Research, generation: Generation) -> None:
        self.store = store
        self.research = research
        self.generation = generation
        with store.connection() as db:
            db.executescript(SCHEMA)

    def _manual_run(self, db: sqlite3.Connection) -> str:
        run_id = uuid4().hex
        state = current(db)
        payload = {
            "schema_version": "1.1", "run_id": run_id, "snapshot_id": state["active_snapshot_id"],
            "status": "result", "result_kind": "manual_draft", "eligibility_revision": state["eligibility_revision"],
            "question": None, "context": None, "input_filter_hash": None, "mode_requested": "manual",
            "mode_used": "manual", "prompt_hash": None, "model_hash": None,
            "null_reason": "Manual drafting without a provider or retrieval call",
            "bundles": [], "ranked_candidates": [], "final_bundle_ids": [],
            "attempts": [{"status": "manual", "finished_at": now()}],
            "hosted_input_confirmation": None, "usage": None, "price_version": None,
            "cost_estimate": None, "cost_actual": None, "cost_status": "no_hosted_dispatch",
        }
        db.execute("INSERT INTO runs VALUES (?,?,'result',?)", (run_id, state["active_snapshot_id"], canonical(payload)))
        return run_id

    @staticmethod
    def _citation(evidence: dict, support_ids: list[str] | None = None, quote: dict | None = None) -> dict:
        included = evidence["ordered_span_ids"] if support_ids is None else support_ids
        spans = evidence["spans"]
        return {
            "citation_id": uuid4().hex, "bundle_id": evidence["bundle_id"], "run_id": evidence["run_id"],
            "supporting_span_ids": included, "quote": quote,
            "document_version_id": evidence["document"]["version_id"],
            "document_byte_hash": evidence["document"]["byte_hash"],
            "source_title": evidence["document"]["title"],
            "source_url": evidence["document"]["source_url"],
            "publication_date": evidence["document"]["publication_date"],
            "unknown_reason": evidence["document"]["unknown_reason"],
            "anchors": [
                {
                    "source_span_id": span["span_id"], "pdf_page_1based": span["pdf_page_1based"],
                    "printed_page_label": span["printed_page_label"], "bounding_boxes": span["bounding_boxes"],
                    "rotation": span["rotation"], "origin": span["origin"],
                    "normalized_start": 0, "normalized_end": len(span["normalized_text"]), "text": span["normalized_text"],
                } for span in spans if span["span_id"] in included
            ],
        }

    def _new_claim(self, evidence: dict) -> tuple[dict, str]:
        claim_id = uuid4().hex
        marker = f"[[EVIDENCE:{claim_id}]]"
        note = evidence["rendered_context"]
        citation = self._citation(evidence)
        qualifiers = [
            span["normalized_text"] for span in evidence["spans"] if span["span_id"] in evidence["mandatory_qualifier_span_ids"]
        ]
        claim = {
            "claim_id": claim_id, "marker": marker, "text": note, "qualifiers": qualifiers,
            "citation_ids": [citation["citation_id"]], "citations": [citation],
            "review_status": "SOURCE_NOTE_UNVERIFIED",
        }
        return claim, f"{marker}\n{note}"

    def save(self, edit: Edit) -> dict:
        evidence = self.research.evidence(edit.attach.run_id, edit.attach.bundle_id) if edit.attach else None
        generated = self.generation.get(edit.generated_operation_id) if edit.generated_operation_id else None
        generated_evidence: dict[str, dict] = {}
        if generated:
            if generated["state"] != "result" or not generated["answer"]["claims"]:
                raise ApiError(409, "answer_not_available", "Only validated answer claims can be attached.")
            generated_evidence = {
                citation["bundle_id"]: self.research.evidence(generated["run_id"], citation["bundle_id"])
                for claim in generated["answer"]["claims"] for citation in claim["citations"]
            }
        with self.store.connection(write=True) as db:
            brief_id = edit.brief_id or uuid4().hex
            parent = None
            if edit.brief_id:
                parent = load_revision(db, brief_id, None)
                if edit.parent_revision_id != parent["revision_id"]:
                    raise ApiError(409, "revision_changed", "Open the current draft revision before saving changes.")
            else:
                db.execute("INSERT INTO briefs(id) VALUES (?)", (brief_id,))
            claims = list(parent["claims"]) if parent else []
            text = edit.text if edit.text is not None else ""
            run_id = parent["run_id"] if parent else None
            if edit.restore_revision_id:
                checkpoint = load_revision(db, brief_id, edit.restore_revision_id)
                text = checkpoint["text"]
                claims = checkpoint["claims"]
                run_id = checkpoint["run_id"]
            else:
                detach = set(edit.detach_claim_ids)
                if not detach <= {claim["claim_id"] for claim in claims}:
                    raise ApiError(400, "invalid_claim_mapping", "Only saved claim associations can be detached.")
                prior_text = parent["text"] if parent else text
                for claim in claims:
                    marker = claim["marker"]
                    if claim["claim_id"] in detach:
                        text = text.replace(marker, "")
                    if marker not in text:
                        prior_text = prior_text.replace(marker, "")
                claims = [claim for claim in claims if claim["marker"] in text]
                if text != prior_text:
                    claims = [{**claim, "review_status": "NEEDS_EVIDENCE_REVIEW"} for claim in claims]
            new_evidence = [*generated_evidence.values(), *([evidence] if evidence is not None else [])]
            if any(withdrawn(db, bundle["version_id"]) for bundle in new_evidence):
                raise ApiError(409, "source_withdrawn", "Withdrawn evidence cannot support a new draft claim.")
            if evidence is not None and edit.attach is not None:
                claim, note = self._new_claim(evidence)
                text = f"{text}\n\n{note}" if text else note
                claims.append(claim)
                run_id = edit.attach.run_id
            if generated:
                for proposed in generated["answer"]["claims"]:
                    claim_id = uuid4().hex
                    marker = f"[[EVIDENCE:{claim_id}]]"
                    citations = []
                    for citation in proposed["citations"]:
                        bundle = generated_evidence[citation["bundle_id"]]
                        citations.append(self._citation(bundle, citation["supporting_span_ids"], citation["quote"]))
                    qualifiers = proposed["qualifiers"]
                    note = proposed["text"] + ("\nQualifications: " + ", ".join(qualifiers) if qualifiers else "")
                    text += ("\n\n" if text else "") + f"{marker}\n{note}"
                    claims.append({
                        "claim_id": claim_id, "marker": marker, "text": proposed["text"], "qualifiers": qualifiers,
                        "citation_ids": [item["citation_id"] for item in citations], "citations": citations,
                        "review_status": "GENERATED_DRAFT_UNVERIFIED",
                    })
                run_id = generated["run_id"]
            if not text.strip():
                raise ApiError(400, "empty_draft", "Enter draft text or attach an eligible source note.")
            if len(text) > 128_000 or len(claims) > 200:
                raise ApiError(413, "draft_limit", "The draft exceeds the local text or association limit.")
            if parent and (text, claims) == (parent["text"], parent["claims"]):
                return parent
            run_id = run_id or self._manual_run(db)
            answer = generated["answer"] if generated else (checkpoint if edit.restore_revision_id else parent) or {}
            revision = {
                "schema_version": "1.1", "brief_id": brief_id, "parent_revision_id": edit.parent_revision_id,
                "run_id": run_id, "status": answer.get("status", "partial"), "text": text, "claims": claims,
                "conflicts": answer.get("conflicts", []), "missing_information": answer.get("missing_information", []),
                "qualification": "Draft. Manual claims and semantic support are unverified.",
                "created_at": now(),
            }
            revision_id = fingerprint(revision)
            revision["revision_id"] = revision_id
            db.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?)",
                (revision_id, brief_id, edit.parent_revision_id, run_id, canonical(revision)),
            )
            db.execute("UPDATE briefs SET latest_revision_id=? WHERE id=?", (revision_id, brief_id))
        return revision

    def list(self) -> dict:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT r.id,r.payload FROM briefs b JOIN revisions r ON b.latest_revision_id=r.id AND r.brief_id=b.id ORDER BY b.rowid DESC"
            )
            briefs = [verified_revision(row) for row in rows]
        return {"briefs": briefs}

    def get(self, brief_id: str, revision_id: str | None = None) -> dict:
        with self.store.connection() as db:
            return load_revision(db, brief_id, revision_id)

    def history(self, brief_id: str) -> dict:
        with self.store.connection() as db:
            load_revision(db, brief_id, None)
            rows = db.execute("SELECT id,payload FROM revisions WHERE brief_id=? ORDER BY rowid DESC", (brief_id,))
            return {"revisions": [verified_revision(row) for row in rows]}

    def _render_export(self, db: sqlite3.Connection, revision: dict, format: Format) -> Response:
        # Export-time annotations live outside the immutable revision payload.
        warnings = []
        for claim in revision["claims"]:
            for citation in claim["citations"]:
                version = citation["document_version_id"]
                metadata = self.store.document(version)
                self.store.original(metadata)
                if metadata["byte_hash"] != citation["document_byte_hash"] or not citation["anchors"]:
                    raise ApiError(409, "invalid_saved_anchor", "A saved citation anchor could not be verified.")
                if not self._anchors_valid(db, citation):
                    raise ApiError(409, "invalid_saved_anchor", "A saved citation no longer matches its recorded evidence.")
                flag = withdrawn(db, version)
                if flag:
                    warnings.append({
                        "claim_id": claim["claim_id"], "document_version_id": version,
                        "status": "HISTORICAL_UNVERIFIED", "withdrawal": flag,
                        "message": "Historical draft — withdrawn source, unverified for current use.",
                    })
        result = {**revision, "draft": True, "label": DRAFT_EXPORT_LABEL, "withdrawal_warnings": warnings}
        if format == "json":
            body = canonical(result) + "\n"
            media_type, extension = "application/json", "json"
        else:
            parent = revision["parent_revision_id"] or "None (root)"
            body = (
                f"# {DRAFT_EXPORT_LABEL}\n\n"
                f"Revision: {revision['revision_id']}\n\nParent revision: {parent}\n\n"
                f"{revision['qualification']}\n\n## Saved draft text\n\n{fenced(revision['text'])}\n\n"
                "## Claim-to-source mappings and qualifications\n\n"
                + fenced(json.dumps(revision["claims"], ensure_ascii=False, indent=2)) + "\n"
            )
            if warnings:
                body += "\n## Current withdrawal warnings\n\nHistorical draft — withdrawn source, unverified for current use.\n\n"
                body += fenced(json.dumps(warnings, ensure_ascii=False, indent=2)) + "\n"
            media_type, extension = "text/markdown", "md"
        filename = f"retrofit-draft-{revision['revision_id'][:12]}.{extension}"
        return Response(body, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @staticmethod
    def _anchors_valid(db: sqlite3.Connection, citation: dict) -> bool:
        row = db.execute("SELECT payload FROM runs WHERE id=?", (citation["run_id"],)).fetchone()
        if row is None:
            return False
        bundles = json.loads(row["payload"])["bundles"]
        bundle = next((item for item in bundles if item["bundle_id"] == citation["bundle_id"]), None)
        if not bundle or bundle["version_id"] != citation["document_version_id"]:
            return False
        spans = {span["span_id"]: span for span in bundle["spans"]}
        if not set(citation["supporting_span_ids"]) <= spans.keys():
            return False
        for anchor in citation["anchors"]:
            source = spans.get(anchor["source_span_id"])
            if source is None:
                return False
            start, end = anchor["normalized_start"], anchor["normalized_end"]
            text = source["normalized_text"]
            if not 0 <= start < end <= len(text) or text[start:end] != anchor["text"]:
                return False
            if any(source[field] != anchor[field] for field in ("bounding_boxes", "pdf_page_1based", "rotation", "origin")):
                return False
        if citation.get("quote"):
            quote = citation["quote"]
            span = spans.get(quote["source_span_id"])
            if span is None or quote["source_span_id"] not in citation["supporting_span_ids"]:
                return False
            if quote_geometry(span, quote["start"], quote["end"], quote["text"]) != quote["bounding_boxes"]:
                return False
        return True

    def export(self, brief_id: str, format: Format, revision_id: str | None = None) -> Response:
        with self.store.connection(write=True) as db:
            return self._render_export(db, load_revision(db, brief_id, revision_id), format)

    def prepare_export(self, brief_id: str, request: ExportRequest) -> dict:
        with self.store.connection(write=True) as db:
            revision = load_revision(db, brief_id, request.revision_id)
            operation_id = uuid4().hex
            db.execute("INSERT INTO exports VALUES (?,?,?,'queued',?,NULL)", (
                operation_id, revision["revision_id"], request.format, now(),
            ))
        return {"operation_id": operation_id, "state": "queued", "revision_id": revision["revision_id"]}

    def deliver(self, operation_id: str) -> Response:
        failure: ApiError | None = None
        result = None
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT e.state,e.format,r.id,r.payload FROM exports e JOIN revisions r ON e.revision_id=r.id WHERE e.id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "missing_export", "That export operation could not be found.")
            if row["state"] != "queued":
                raise ApiError(409, "export_not_pending", "That export has already completed or was canceled. Prepare a new export to retry.")
            try:
                result = self._render_export(db, verified_revision(row), row["format"])
            except ApiError as exc:
                failure = exc
            db.execute("UPDATE exports SET state=?,finished_at=? WHERE id=?", ("failed" if failure else "delivered", now(), operation_id))
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def cancel_export(self, operation_id: str) -> dict:
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT state FROM exports WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise ApiError(404, "missing_export", "That export operation could not be found.")
            if row["state"] == "queued":
                db.execute("UPDATE exports SET state='canceled',finished_at=? WHERE id=?", (now(), operation_id))
            state = "canceled" if row["state"] in {"queued", "canceled"} else "already_completed"
        return {"operation_id": operation_id, "state": state}


def brief_routes(briefs: Briefs) -> APIRouter:
    router = APIRouter()
    router.add_api_route("/briefs", briefs.save, methods=["POST"], status_code=201)
    router.add_api_route("/briefs", briefs.list, methods=["GET"])
    router.add_api_route("/briefs/{brief_id}", briefs.get, methods=["GET"])
    router.add_api_route("/briefs/{brief_id}/revisions", briefs.history, methods=["GET"])
    router.add_api_route("/briefs/{brief_id}/export", briefs.export, methods=["GET"])
    router.add_api_route("/briefs/{brief_id}/exports", briefs.prepare_export, methods=["POST"], status_code=201)
    router.add_api_route("/exports/{operation_id}", briefs.deliver, methods=["GET"])
    router.add_api_route("/exports/{operation_id}/cancel", briefs.cancel_export, methods=["POST"])
    return router
