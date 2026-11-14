import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event
from time import monotonic, sleep
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter
from pydantic import Field, StringConstraints, ValidationError

from .contracts import ApiError, Contract, ShortText, Versioned, canonical, fingerprint, now, quote_geometry
from .corpus import Opaque
from .hosted_policy import INPUT_TOKENS, INSTRUCTIONS, OUTPUT_TOKENS, PaidAuthority, ProviderFailure, ProviderInfo, ProviderReply
from .openai_provider import OpenAIProvider
from .research import Context, Question, Research, withdrawn
from .store import Store


SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_ops (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    state TEXT NOT NULL,
    group_name TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_generation ON generation_ops(state) WHERE state='pending';
CREATE TABLE IF NOT EXISTS provider_attempts (
    operation_id TEXT NOT NULL REFERENCES generation_ops(id),
    attempt INTEGER NOT NULL,
    reservation INTEGER NOT NULL,
    actual INTEGER,
    payload TEXT NOT NULL,
    PRIMARY KEY(operation_id,attempt)
);
CREATE TABLE IF NOT EXISTS dispatch_denials (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    code TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""
HOLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_holds (
    reference TEXT PRIMARY KEY,
    frozen_configuration TEXT,
    evidence TEXT NOT NULL,
    resolved_by TEXT
);
"""
EVIDENCE_TOKENS = 7_500
DEADLINE_SECONDS = 30
ATTEMPT_LIMIT = 1_600
GROUP_LIMITS = {"development": 180, "held_out": 270, "latency": 200, "additional": 150}


class Confirm(Versioned):
    confirmed: bool
    confirmation_id: Opaque
    question: Question
    context: Context
    group_name: Literal["development", "held_out", "latency", "additional"] = "additional"


class Toggle(Versioned):
    enabled: bool


class Reconcile(Versioned):
    actual_nanos: Annotated[int, Field(strict=True, ge=0, le=(2**63 - 1) // ATTEMPT_LIMIT)]
    billing_reference: ShortText


class RecoverBudget(Versioned):
    report_sha256: Opaque


def ledger_totals(db: sqlite3.Connection) -> sqlite3.Row:
    return db.execute(
        "SELECT COALESCE(SUM(CASE WHEN json_extract(payload,'$.dispatch_at') IS NOT NULL THEN 1 ELSE 0 END),0) n,"
        "COALESCE(SUM(COALESCE(actual,reservation)),0) committed FROM provider_attempts"
    ).fetchone()


def maximum_reservation(info: ProviderInfo) -> int:
    return INPUT_TOKENS * info.input_nanos_per_token + OUTPUT_TOKENS * info.output_nanos_per_token


def budget_policy_identity(info: ProviderInfo, deadline_seconds: float = DEADLINE_SECONDS) -> str:
    deadline = int(deadline_seconds) if float(deadline_seconds).is_integer() else deadline_seconds
    return fingerprint([
        info.model, info.revision, info.tokenizer, info.input_nanos_per_token, info.output_nanos_per_token,
        INSTRUCTIONS, INPUT_TOKENS, OUTPUT_TOKENS, deadline, ATTEMPT_LIMIT, GROUP_LIMITS,
    ])


def budget_case_identity(run: dict, bundles: list[dict]) -> str:
    evidence = [
        [bundle["anchor_chunk_id"], bundle["mandatory_qualifier_span_ids"],
         [(span["span_id"], span["normalized_text"]) for span in bundle["spans"]]]
        for bundle in bundles
    ]
    return fingerprint([
        run["snapshot_id"], run["eligibility_revision"], run["question"], run["context"],
        run["mode_requested"], sorted(run["version_ids"]), evidence,
    ])


def usage_exceeds_budget(input_count: int | None, output_count: int | None) -> bool:
    return (input_count is not None and not 0 <= input_count <= INPUT_TOKENS) or (
        output_count is not None and not 0 <= output_count <= OUTPUT_TOKENS
    )


class ProposedQuote(Contract):
    source_span_id: Opaque
    start: Annotated[int, Field(ge=0, le=8192)]
    end: Annotated[int, Field(ge=1, le=8192)]
    text: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=8192)]


class ProposedCitation(Contract):
    bundle_id: Opaque
    supporting_span_ids: Annotated[list[Opaque], Field(min_length=1, max_length=100)]
    quote: ProposedQuote | None


class ProposedClaim(Contract):
    text: ShortText
    qualifiers: Annotated[list[ShortText], Field(max_length=20)]
    citations: Annotated[list[ProposedCitation], Field(min_length=1, max_length=8)]


class ProposedAnswer(Contract):
    status: Literal["answer", "partial", "clarify", "abstain"]
    claims: Annotated[list[ProposedClaim], Field(max_length=40)]
    conflicts: Annotated[list[ShortText], Field(max_length=20)]
    missing_information: Annotated[list[ShortText], Field(max_length=20)]


def response_schema() -> dict:
    # The provider receives one strict object schema. Local validation additionally applies lengths and bounds.
    schema = ProposedAnswer.model_json_schema()

    def simplify(value) -> None:
        if isinstance(value, dict):
            for key in ("title", "maxLength", "minLength", "maximum", "minimum", "minItems", "maxItems"):
                value.pop(key, None)
            for child in value.values():
                simplify(child)
        elif isinstance(value, list):
            for child in value:
                simplify(child)

    simplify(schema)
    return schema


def packed_context(provider: OpenAIProvider, run: dict) -> tuple[str, list[dict], int, bool]:
    schema = response_schema()
    base = {"instructions": INSTRUCTIONS, "question": run["question"], "context": run["context"]}
    packed: list[dict] = []
    included: list[dict] = []
    for bundle in run["bundles"][:8]:
        payload = {
            "bundle_id": bundle["bundle_id"],
            "mandatory_qualifier_span_ids": bundle["mandatory_qualifier_span_ids"],
            "spans": [{"id": span["span_id"], "text": span["normalized_text"]} for span in bundle["spans"]],
        }
        candidate = [*packed, payload]
        complete = canonical({**base, "evidence": candidate})
        if provider.tokens(canonical(candidate)) <= EVIDENCE_TOKENS and provider.input_tokens(complete, schema) <= INPUT_TOKENS:
            packed.append(payload)
            included.append(bundle)
    request = canonical({**base, "evidence": packed})
    total = provider.input_tokens(request, schema)
    if total > INPUT_TOKENS:
        raise ApiError(400, "generator_input_too_long", "Shorten the question or use local evidence search.")
    return request, included, total, bool(run.get("incomplete_coverage")) or len(included) < len(run["bundles"])


class Generation:
    def __init__(
        self, store: Store, research: Research, session: str, *,
        provider: OpenAIProvider | None, authority: PaidAuthority | None,
        deadline_seconds: float = DEADLINE_SECONDS,
    ) -> None:
        if not 0 < deadline_seconds <= DEADLINE_SECONDS:
            raise ValueError("Generation deadlines must be within the accepted 30-second limit")
        self.store = store
        self.research = research
        self.session_hash = fingerprint(session)
        self.provider = provider
        self.authority = authority
        self.deadline_seconds = deadline_seconds
        self.enabled = Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retrofit-generation")
        with store.connection() as db:
            db.executescript(SCHEMA)
        with store.connection(write=True) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(generation_holds)")}
            if "report_id" in columns:
                db.execute("ALTER TABLE generation_holds RENAME TO generation_holds_legacy")
                db.execute(HOLD_SCHEMA)
                db.execute(
                    "INSERT INTO generation_holds SELECT report_id,frozen_configuration,'[]',resolved_by "
                    "FROM generation_holds_legacy"
                )
                db.execute("DROP TABLE generation_holds_legacy")
            else:
                db.execute(HOLD_SCHEMA)
            rows = db.execute("SELECT id,payload FROM generation_ops WHERE state='pending'").fetchall()
            for row in rows:
                value = json.loads(row["payload"])
                value.update(state="error", failure_code="interrupted", finished_at=now())
                db.execute("UPDATE generation_ops SET state='error',payload=? WHERE id=?", (canonical(value), row["id"]))
            db.execute(
                "UPDATE provider_attempts SET actual=0,payload=json_set(payload,'$.cost_status','not_dispatched') "
                "WHERE actual IS NULL AND json_extract(payload,'$.dispatch_at') IS NULL"
            )
            db.execute(
                "UPDATE provider_attempts SET payload=json_set(payload,'$.state','interrupted','$.finished_at',?,"
                "'$.actual_nanos',actual,'$.cost_status',CASE "
                "WHEN json_extract(payload,'$.dispatch_at') IS NULL THEN 'not_dispatched' "
                "WHEN actual IS NULL THEN 'unresolved_reservation_held' ELSE 'reconciled' END) "
                "WHERE json_extract(payload,'$.state') IN ('reserved','dispatched')", (now(),),
            )

    def status(self) -> dict:
        with self.store.connection() as db:
            row = ledger_totals(db)
            budget_holds = [hold["reference"] for hold in db.execute(
                "SELECT reference FROM generation_holds WHERE frozen_configuration IS NULL AND resolved_by IS NULL ORDER BY rowid",
            )]
            unreconciled = [dict(attempt) for attempt in db.execute(
                "SELECT operation_id,attempt,reservation AS reserved_nanos FROM provider_attempts "
                "WHERE actual IS NULL AND json_extract(payload,'$.dispatch_at') IS NOT NULL ORDER BY rowid",
            )]
        return {
            "enabled": self.enabled.is_set(), "configured": self.provider is not None and self.authority is not None,
            "dispatch_count": row["n"], "actual_and_reserved_nanos": row["committed"],
            "price_version": self.provider.info.price_version if self.provider else None,
            "budget_holds": budget_holds, "unreconciled_attempts": unreconciled,
        }

    def toggle(self, request: Toggle) -> dict:
        with self.store.connection(write=True) as db:
            if request.enabled:
                if not self.provider or not self.authority:
                    raise ApiError(403, "paid_execution_not_authorized", "Generation needs a configured provider and separate D3 authority.")
                if db.execute("SELECT 1 FROM generation_holds WHERE resolved_by IS NULL LIMIT 1").fetchone():
                    raise ApiError(409, "generation_regressions_pending", "Generation remains disabled until its recorded safety holds are resolved.")
                self.enabled.set()
            else:
                self.enabled.clear()
        return self.status()

    def _preview(self, run: dict) -> dict:
        confirmation_id = fingerprint([self.session_hash, run["run_id"], run["question"], run["context"]])
        return {
            "confirmation_id": confirmation_id, "question": run["question"], "context": run["context"],
            "notice": "A confirmed request includes generation instructions and eligible public evidence excerpts.",
        }

    def preview(self, run_id: str) -> dict:
        run = self.research.get(run_id)
        if run["status"] != "result" or run["mode_used"] == "manual":
            raise ApiError(409, "query_not_ready", "Complete an evidence search before reviewing hosted input.")
        return {**self._preview(run), **self.status()}

    def _deny(self, run_id: str, status: int, code: str, message: str) -> None:
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO dispatch_denials VALUES (?,?,?,?)", (uuid4().hex, run_id, code, now()))
        raise ApiError(status, code, message)

    def start(self, run_id: str, decision: Confirm) -> dict:
        run = self.research.get(run_id)
        preview = self.preview(run_id)
        if not decision.confirmed:
            self._deny(run_id, 403, "hosted_input_declined", "Hosted input was declined. Local evidence and drafting remain available.")
        if any(getattr(decision, key) != preview[key] for key in ("question", "context", "confirmation_id")):
            self._deny(run_id, 409, "hosted_input_changed", "The hosted input changed or belongs to an expired session.")
        if not self.authority or not self.provider or not self.enabled.is_set():
            self._deny(run_id, 403, "generation_disabled", "Generation is disabled. Local evidence and drafting remain available.")
        assert self.authority and self.provider
        text, bundles, token_count, incomplete = packed_context(self.provider, run)
        operation_id = uuid4().hex
        deadline = monotonic() + self.deadline_seconds
        deadline_at = datetime.now(timezone.utc) + timedelta(seconds=self.deadline_seconds)
        record = {
            "operation_id": operation_id, "run_id": run_id, "state": "pending", "schema_version": "1.1",
            "confirmed_at": now(), "confirmation_id": decision.confirmation_id,
            "snapshot_id": run["snapshot_id"], "eligibility_revision": run["eligibility_revision"],
            "packed_bundle_ids": [bundle["bundle_id"] for bundle in bundles],
            "generator_bundle_tokens": {
                bundle["bundle_id"]: self.provider.tokens(canonical({
                    "bundle_id": bundle["bundle_id"], "mandatory_qualifier_span_ids": bundle["mandatory_qualifier_span_ids"],
                    "spans": [{"id": span["span_id"], "text": span["normalized_text"]} for span in bundle["spans"]],
                })) for bundle in bundles
            },
            "incomplete_coverage": incomplete, "prompt_hash": fingerprint(text),
            "model_hash": fingerprint(vars(self.provider.info)), "provider": vars(self.provider.info),
            "budget_policy_sha256": budget_policy_identity(self.provider.info, self.deadline_seconds),
            "budget_case_sha256": budget_case_identity(run, bundles),
            "deadline_at": deadline_at.isoformat(), "deadline_seconds": self.deadline_seconds,
            "authority_reference": self.authority.reference, "input_tokens_preflight": token_count, "answer": None,
        }
        with self.store.connection(write=True) as db:
            if db.execute("SELECT 1 FROM generation_ops WHERE state='pending'").fetchone() or db.execute(
                "SELECT 1 FROM runs WHERE status='pending'"
            ).fetchone():
                raise ApiError(429, "generation_busy", "One generation request is already active.")
            count = db.execute("SELECT COUNT(*) FROM generation_ops WHERE group_name=?", (decision.group_name,)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM generation_ops").fetchone()[0]
            if count >= GROUP_LIMITS[decision.group_name] or total >= 800:
                raise ApiError(409, "request_count_limit", "The authorized experiment request limit is exhausted.")
            db.execute("INSERT INTO generation_ops VALUES (?,?,'pending',?,?)", (
                operation_id, run_id, decision.group_name, canonical(record),
            ))
        self.pool.submit(self._execute, record, text, bundles, deadline)
        return {"operation_id": operation_id, "state": "pending"}

    def _reserve(self, record: dict, attempt: int, bundles: list[dict]) -> bool:
        assert self.authority and self.provider
        info = self.provider.info
        reserved = maximum_reservation(info)
        operation = record["operation_id"]
        with self.store.connection(write=True) as db:
            state = db.execute("SELECT state FROM generation_ops WHERE id=?", (operation,)).fetchone()["state"]
            if state != "pending":
                return False
            if not self.enabled.is_set():
                raise ApiError(403, "generation_disabled", "Generation was disabled before dispatch.")
            if any(withdrawn(db, bundle["version_id"]) for bundle in bundles):
                raise ApiError(409, "source_withdrawn", "Eligible evidence changed before dispatch. Run a new search.")
            row = ledger_totals(db)
            if row["n"] >= ATTEMPT_LIMIT or row["committed"] + reserved > self.authority.maximum_nanos:
                raise ApiError(409, "budget_limit", "The authorized attempt or cost limit is exhausted.")
            db.execute("INSERT INTO provider_attempts VALUES (?,?,?,NULL,?)", (
                operation, attempt, reserved, canonical({"state": "reserved", "reserved_nanos": reserved, "started_at": now()}),
            ))
        return True

    def _begin_dispatch(self, operation: str, attempt: int, bundles: list[dict], deadline: float) -> None:
        assert self.authority and self.provider
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT state FROM generation_ops WHERE id=?", (operation,)).fetchone()
            if row["state"] != "pending":
                raise ApiError(409, "generation_canceled", "The generation request was canceled before dispatch.")
            if any(withdrawn(db, bundle["version_id"]) for bundle in bundles):
                raise ApiError(409, "source_withdrawn", "Eligible evidence changed before dispatch.")
            row = db.execute("SELECT reservation,payload FROM provider_attempts WHERE operation_id=? AND attempt=?", (operation, attempt)).fetchone()
            payload = json.loads(row["payload"])
            if payload["state"] != "reserved":
                raise ApiError(409, "dispatch_already_recorded", "That provider attempt already passed the dispatch boundary.")
            ledger = ledger_totals(db)
            if (
                row["reservation"] != maximum_reservation(self.provider.info)
                or ledger["committed"] > self.authority.maximum_nanos or ledger["n"] >= ATTEMPT_LIMIT
            ):
                raise ApiError(409, "budget_limit", "The dispatch no longer has a valid reservation within the authorized budget.")
            if not self.enabled.is_set() or monotonic() >= deadline:
                raise ApiError(503, "generation_unavailable", "Generation was disabled or reached its deadline before dispatch.")
            payload.update(
                state="dispatched", dispatch_at=now(), authority_maximum_nanos=self.authority.maximum_nanos,
                committed_nanos_at_dispatch=ledger["committed"], dispatch_count_before=ledger["n"],
            )
            db.execute(
                "UPDATE provider_attempts SET payload=? WHERE operation_id=? AND attempt=?",
                (canonical(payload), operation, attempt),
            )

    def _record_attempt(self, operation: str, attempt: int, reply: ProviderReply | None, state: str) -> None:
        assert self.provider
        input_count = reply.usage_input if reply else None
        output_count = reply.usage_output if reply else None
        info = self.provider.info
        exceeded = usage_exceeds_budget(input_count, output_count)
        estimate = None
        if input_count is not None and output_count is not None and not exceeded:
            estimate = input_count * info.input_nanos_per_token + output_count * info.output_nanos_per_token
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT payload,reservation,actual FROM provider_attempts WHERE operation_id=? AND attempt=?", (operation, attempt)).fetchone()
            payload = json.loads(row["payload"])
            actual = row["actual"] if payload.get("dispatch_at") else 0
            if not payload.get("dispatch_at"):
                cost_status = "not_dispatched"
            elif actual is not None:
                cost_status = "reconciled"
            else:
                cost_status = "unresolved_reservation_held"
            payload.update({
                "state": state, "finished_at": now(), "usage_input": input_count, "usage_output": output_count,
                "actual_model": reply.actual_model if reply else None, "actual_nanos": actual, "estimated_nanos": estimate,
                "cost_status": cost_status,
                # Escape malformed Unicode in diagnostics before persisting usage and applying a hold.
                "restricted_output": reply.output.encode("utf-8", "backslashreplace").decode("utf-8") if reply else None,
            })
            db.execute("UPDATE provider_attempts SET actual=?,payload=? WHERE operation_id=? AND attempt=?", (
                actual, canonical(payload), operation, attempt,
            ))
            if exceeded:
                self._hold_budget(db, operation, budget_policy_identity(info, self.deadline_seconds))

    def _hold_budget(self, db: sqlite3.Connection, operation: str, policy_sha256: str) -> None:
        db.execute(
            "INSERT INTO generation_holds VALUES (?,NULL,?,NULL) "
            "ON CONFLICT(reference) DO UPDATE SET evidence=excluded.evidence,resolved_by=NULL",
            (operation, canonical({"reason": "provider_budget_violation", "policy_sha256": policy_sha256, "observed_at": now()})),
        )
        self.enabled.clear()

    def _execute(self, record: dict, text: str, bundles: list[dict], deadline: float) -> None:
        provider = self.provider
        assert provider is not None
        operation = record["operation_id"]
        answer = None
        failure = None
        try:
            if not bundles:
                answer = {"status": "abstain", "claims": [], "conflicts": [], "missing_information": []}
            else:
                attempt = 1
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise ApiError(503, "generation_deadline", "The generation deadline expired.")
                    if not self._reserve(record, attempt, bundles):
                        return
                    try:
                        async def invoke() -> ProviderReply:
                            timeout = max(0.001, deadline - monotonic())
                            async with asyncio.timeout(timeout):
                                return await provider.infer(
                                    text, response_schema(), max_output=OUTPUT_TOKENS, timeout=timeout,
                                    before_dispatch=lambda: self._begin_dispatch(operation, attempt, bundles, deadline),
                                )
                        reply = asyncio.run(invoke())
                    except ProviderFailure as exc:
                        self._record_attempt(operation, attempt, None, "transient_error" if exc.transient else "provider_error")
                        if exc.transient and attempt == 1:
                            sleep(max(0.0, min(0.15, deadline - monotonic())))
                            attempt = 2
                            continue
                        raise ApiError(503, "provider_failed", "The provider failed. Manual drafting remains available.") from exc
                    except TimeoutError as exc:
                        self._record_attempt(operation, attempt, None, "deadline")
                        raise ApiError(503, "generation_deadline", "The generation deadline expired.") from exc
                    except ApiError:
                        self._record_attempt(operation, attempt, None, "dispatch_denied")
                        raise
                    except Exception as exc:
                        self._record_attempt(operation, attempt, None, "provider_error")
                        raise ApiError(503, "provider_failed", "The provider failed. Manual drafting remains available.") from exc
                    self._record_attempt(operation, attempt, reply, "received")
                    if reply.usage_input is None or reply.usage_output is None:
                        raise ApiError(503, "usage_unresolved", "Provider usage was not available. The reservation is held.")
                    if usage_exceeds_budget(reply.usage_input, reply.usage_output):
                        raise ApiError(503, "provider_token_limit", "The provider exceeded the recorded token budget.")
                    if not self.enabled.is_set():
                        raise ApiError(503, "generation_disabled", "Generation was disabled before the response completed.")
                    answer = self.validate(reply.output, bundles)
                    break
        except (ApiError, ValueError, KeyError, RuntimeError) as exc:
            failure = exc.code if isinstance(exc, ApiError) else "invalid_provider_output"
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT state FROM generation_ops WHERE id=?", (operation,)).fetchone()
            if row["state"] != "pending":
                return
            if any(withdrawn(db, bundle["version_id"]) for bundle in bundles):
                failure = "source_withdrawn"
            if failure is None and monotonic() >= deadline:
                failure = "generation_deadline"
            state = "error" if failure else "result"
            record.update(state=state, failure_code=failure, finished_at=now(), answer=None if failure else answer)
            db.execute("UPDATE generation_ops SET state=?,payload=? WHERE id=?", (state, canonical(record), operation))

    @staticmethod
    def validate(output: str, bundles: list[dict]) -> dict:
        if len(output.encode("utf-8")) > 128_000:
            raise ApiError(503, "invalid_provider_output", "The provider response exceeded its structured size bound.")
        try:
            answer = ProposedAnswer.model_validate_json(output, strict=True)
        except ValidationError as exc:
            raise ApiError(503, "invalid_provider_output", "The provider response did not match the answer contract.") from exc
        if (
            (answer.status == "answer" and not answer.claims)
            or (answer.status in {"clarify", "abstain"} and answer.claims)
            or (answer.status == "clarify" and not answer.missing_information)
            or (answer.status == "partial" and not (answer.claims or answer.conflicts or answer.missing_information))
        ):
            raise ApiError(503, "invalid_provider_output", "The response status did not match the returned content.")
        by_id = {bundle["bundle_id"]: bundle for bundle in bundles}
        claims = []
        for claim in answer.claims:
            mandatory_qualifiers = list(claim.qualifiers)
            citations = []
            known_citations: set[str] = set()
            for citation in claim.citations:
                bundle = by_id.get(citation.bundle_id)
                if not bundle:
                    raise ApiError(503, "citation_outside_evidence", "The provider cited a bundle outside its eligible context.")
                spans = {span["span_id"]: span for span in bundle["spans"]}
                selected = set(citation.supporting_span_ids)
                mandatory = bundle["mandatory_qualifier_span_ids"]
                if not selected <= spans.keys() or not set(mandatory) <= selected:
                    raise ApiError(503, "citation_outside_evidence", "A citation omitted mandatory context or named an unknown span.")
                if citation.quote:
                    quote = citation.quote
                    source = spans.get(quote.source_span_id)
                    if source is None or quote.source_span_id not in selected or not 0 <= quote.start < quote.end <= len(source["normalized_text"]):
                        raise ApiError(503, "invalid_quote_anchor", "The provider returned an invalid quotation anchor.")
                    if source["normalized_text"][quote.start:quote.end] != quote.text:
                        raise ApiError(503, "quote_mismatch", "The provider quotation did not match the immutable source span.")
                    geometry = quote_geometry(source, quote.start, quote.end, quote.text)
                    if not geometry:
                        raise ApiError(503, "invalid_quote_anchor", "The quotation did not resolve to one contiguous original source region.")
                payload = citation.model_dump()
                if citation.quote:
                    payload["quote"]["bounding_boxes"] = geometry
                payload["supporting_span_ids"] = [sid for sid in bundle["ordered_span_ids"] if sid in selected]
                key = fingerprint(payload)
                if key in known_citations:
                    continue
                known_citations.add(key)
                mandatory_qualifiers.extend(spans[sid]["normalized_text"] for sid in mandatory)
                citations.append({**payload, "citation_id": key})
            claims.append({
                "claim_id": fingerprint([claim.text, citations]), "text": claim.text,
                "qualifiers": list(dict.fromkeys(mandatory_qualifiers)),
                "citations": citations, "citation_ids": [item["citation_id"] for item in citations],
            })
        return {**answer.model_dump(), "claims": claims, "qualification": "Draft — verify evidence and semantic support."}

    def get(self, operation_id: str) -> dict:
        with self.store.connection() as db:
            row = db.execute("SELECT payload FROM generation_ops WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise ApiError(404, "missing_operation", "That generation operation could not be found.")
            record = json.loads(row["payload"])
            attempts = [json.loads(row["payload"]) for row in db.execute(
                "SELECT payload FROM provider_attempts WHERE operation_id=? ORDER BY attempt", (operation_id,),
            )]
        if record["answer"] and record["packed_bundle_ids"]:
            try:
                run = self.research.get(record["run_id"])
            except ApiError:
                record.update(state="error", failure_code="saved_evidence_unavailable", answer=None)
            else:
                if not set(record["packed_bundle_ids"]) <= set(run["final_bundle_ids"]):
                    code = "source_withdrawn" if run.get("eligibility_changed") else "saved_evidence_unavailable"
                    record.update(state="error", failure_code=code, answer=None)
        for attempt in attempts:
            attempt.pop("restricted_output", None)
        return {**record, "attempts": attempts}

    def cancel(self, operation_id: str) -> dict:
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT state,payload FROM generation_ops WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise ApiError(404, "missing_operation", "That generation operation could not be found.")
            state = row["state"]
            if state == "pending":
                value = json.loads(row["payload"])
                value.update(state="canceled", finished_at=now(), answer=None)
                db.execute("UPDATE generation_ops SET state='canceled',payload=? WHERE id=?", (canonical(value), operation_id))
            return {"operation_id": operation_id, "state": "canceled" if state in {"pending", "canceled"} else "already_completed"}

    def reconcile(self, operation_id: str, attempt: int, request: Reconcile) -> dict:
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT actual,reservation,payload FROM provider_attempts WHERE operation_id=? AND attempt=?", (
                operation_id, attempt,
            )).fetchone()
            if row is None:
                raise ApiError(404, "missing_attempt", "That provider attempt could not be found.")
            if row["actual"] is not None:
                raise ApiError(409, "already_reconciled", "The attempt already has recorded actual cost.")
            payload = json.loads(row["payload"])
            if payload.get("dispatch_at") is None:
                raise ApiError(409, "not_dispatched", "Only dispatched attempts can have external billing.")
            payload.update(cost_status="reconciled", actual_nanos=request.actual_nanos, billing_reference=request.billing_reference)
            db.execute("UPDATE provider_attempts SET actual=?,payload=? WHERE operation_id=? AND attempt=?", (
                request.actual_nanos, canonical(payload), operation_id, attempt,
            ))
            if request.actual_nanos > row["reservation"]:
                record = db.execute("SELECT payload FROM generation_ops WHERE id=?", (operation_id,)).fetchone()
                observed = json.loads(record["payload"])
                self._hold_budget(db, operation_id, budget_policy_identity(
                    ProviderInfo(**observed["provider"]), observed.get("deadline_seconds", DEADLINE_SECONDS),
                ))
        return {"operation_id": operation_id, "attempt": attempt, "cost_status": "reconciled", "actual_nanos": request.actual_nanos}

    def recover_budget(self, operation_id: str, request: RecoverBudget) -> dict:
        with self.store.connection(write=True) as db:
            hold = db.execute(
                "SELECT h.evidence,h.resolved_by,o.payload,r.payload AS run_payload FROM generation_holds h "
                "JOIN generation_ops o ON o.id=h.reference JOIN runs r ON r.id=o.run_id "
                "WHERE h.reference=? AND h.frozen_configuration IS NULL", (operation_id,),
            ).fetchone()
            if hold is None:
                raise ApiError(404, "missing_budget_hold", "That operation has no provider-budget hold.")
            if hold["resolved_by"]:
                return {"operation_id": operation_id, "resolved_by": hold["resolved_by"], "enabled": self.enabled.is_set()}
            if not self.provider or not self.authority:
                raise ApiError(403, "paid_execution_not_authorized", "Recovery requires a configured provider and separate D3 authority.")
            record = json.loads(hold["payload"])
            policy = budget_policy_identity(self.provider.info, self.deadline_seconds)
            incident = json.loads(hold["evidence"])
            recorded_policy = incident.get("policy_sha256") or budget_policy_identity(ProviderInfo(**record["provider"]))
            if policy != recorded_policy:
                raise ApiError(409, "budget_configuration_changed", "Use the recorded provider configuration to verify its budget regression.")
            if record["state"] == "pending" or db.execute(
                "SELECT 1 FROM provider_attempts WHERE operation_id=? AND (actual IS NULL "
                "OR json_extract(payload,'$.state') IN ('reserved','dispatched'))", (operation_id,),
            ).fetchone():
                raise ApiError(409, "billing_reconciliation_required", "Wait for the operation to finish and reconcile its outstanding billing.")
            report = db.execute(
                "SELECT runs FROM evaluation_regressions WHERE report_id=? AND passing=1", (request.report_sha256,),
            ).fetchone()
            ended = max(datetime.fromisoformat(value) for value in (record["finished_at"], incident.get("observed_at", record["finished_at"])))
            saved_run = json.loads(hold["run_payload"])
            packed = set(record["packed_bundle_ids"])
            affected_case = budget_case_identity(saved_run, [bundle for bundle in saved_run["bundles"] if bundle["bundle_id"] in packed])
            current_time = datetime.now(timezone.utc)
            if report is None or not all(
                run.get("budget_policy_sha256") == policy
                and ended < datetime.fromisoformat(run["started_at"]) <= datetime.fromisoformat(run["finished_at"]) <= current_time
                and run.get("budget_dispatches_checked", 0) > 0
                and affected_case in run.get("budget_case_hashes", ()) and not run["unresolved_cases"]
                for run in json.loads(report["runs"])
            ):
                raise ApiError(409, "budget_regression_unverified", "Import a later passing evaluation with measured budget regressions for this configuration.")
            ledger = ledger_totals(db)
            if ledger["n"] >= ATTEMPT_LIMIT or ledger["committed"] + maximum_reservation(self.provider.info) > self.authority.maximum_nanos:
                raise ApiError(409, "budget_limit", "The authorized attempt or cost limit is exhausted.")
            db.execute("UPDATE generation_holds SET resolved_by=? WHERE reference=?", (request.report_sha256, operation_id))
        return {"operation_id": operation_id, "resolved_by": request.report_sha256, "enabled": False}


def generation_routes(generation: Generation) -> APIRouter:
    router = APIRouter()
    router.add_api_route("/generation", generation.status, methods=["GET"])
    router.add_api_route("/generation", generation.toggle, methods=["POST"])
    router.add_api_route("/queries/{run_id}/hosted-input", generation.preview, methods=["GET"])
    router.add_api_route("/queries/{run_id}/generate", generation.start, methods=["POST"], status_code=202)
    router.add_api_route("/generation/{operation_id}", generation.get, methods=["GET"])
    router.add_api_route("/generation/{operation_id}/cancel", generation.cancel, methods=["POST"])
    router.add_api_route("/generation/{operation_id}/attempts/{attempt}/reconcile", generation.reconcile, methods=["POST"])
    router.add_api_route("/generation/{operation_id}/recover-budget", generation.recover_budget, methods=["POST"])
    return router
