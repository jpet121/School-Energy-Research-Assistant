from datetime import datetime, timezone
import json
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import AwareDatetime, Field, StrictBool, StringConstraints, TypeAdapter

from .contracts import ApiError, Contract, ShortText, Versioned, canonical, fingerprint
from .corpus import Opaque
from .evaluate import eligibility, generation_gate
from .generation import Generation
from .store import Store


GATE_RESULT: TypeAdapter[bool | None] = TypeAdapter(StrictBool | None)
COUNT: TypeAdapter[int] = TypeAdapter(Annotated[int, Field(strict=True, ge=0)])
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
DIGEST: TypeAdapter[str] = TypeAdapter(Digest)


def reaches_threshold(value: object, threshold: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and threshold <= value <= 1


class ReportDecisions(Contract):
    production: Literal["PASS", "FAIL", "NOT_EXECUTED"]
    retrieval: Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]
    generation: Literal["PASS", "FAIL", "UNMEASURED"]
    eligible_scope: Literal["WITHHELD", "WORKBENCH_ONLY", "WORKBENCH_AND_GENERATION"]
    generation_eligible: bool
    owner_acceptance: Literal["NOT_RECORDED"]
    release: Literal["NOT_AUTHORIZED"]


class ReportImport(Versioned):
    evaluation_mode: Literal["SELF_EVALUATED", "INDEPENDENT"]
    protocol_sha256: Opaque
    selection_sha256: Opaque
    candidate: ShortText
    custody_reference: ShortText
    builder_exposure: ShortText | None
    decisions: ReportDecisions
    retrieval: Annotated[list[dict], Field(min_length=3, max_length=3)]
    generation: Annotated[list[dict], Field(max_length=3)]
    production_evidence_reference: ShortText | None
    limitations: Annotated[list[ShortText], Field(max_length=20)]
    recorded_at: ShortText
    report_sha256: Opaque


class RegressionRun(Contract):
    model_hash: Opaque
    execution_sha256: Digest
    outputs_sha256: Digest
    started_at: AwareDatetime
    finished_at: AwareDatetime
    unresolved_cases: Annotated[list[Digest], Field(max_length=90)]
    budget_dispatches_checked: Annotated[int, Field(strict=True, ge=0, le=360)] = 0
    budget_policy_sha256: Digest | None = None
    budget_case_hashes: Annotated[list[Digest], Field(max_length=180)] = Field(default_factory=list)


def regression_resolves(failure: list[dict], correction: list[dict]) -> bool:
    # The reports refer to the same frozen case inventory. A new aggregate must
    # bind new outputs and pass every case that failed in the recorded repetitions.
    if not failure or not correction:
        return False
    last_failure = max(item["finished_at"] for item in failure)
    failures = {case for item in failure for case in item["unresolved_cases"]}
    old_runs = {item["execution_sha256"] for item in failure}
    old_outputs = {item["outputs_sha256"] for item in failure}
    return all(
        item["started_at"] > last_failure and item["execution_sha256"] not in old_runs
        and item["outputs_sha256"] not in old_outputs and failures.isdisjoint(item["unresolved_cases"])
        for item in correction
    )


def update_generation_holds(
    db: sqlite3.Connection, identifier: str, decisions: ReportDecisions,
    configuration: str, runs: list[dict], service: Generation,
) -> None:
    if decisions.generation == "FAIL":
        passing = db.execute(
            "SELECT report_id,runs FROM evaluation_regressions WHERE configuration=? AND passing=1 ORDER BY rowid",
            (configuration,),
        )
        resolved_by = None
        for row in passing:
            if regression_resolves(runs, json.loads(row["runs"])):
                resolved_by = row["report_id"]
                break
        db.execute("INSERT INTO generation_holds VALUES (?,?,?,?)", (identifier, configuration, canonical(runs), resolved_by))
        if resolved_by is None:
            service.enabled.clear()
    elif decisions.eligible_scope == "WORKBENCH_ONLY":
        service.enabled.clear()
    elif decisions.generation_eligible and runs:
        pending = db.execute(
            "SELECT reference,evidence FROM generation_holds WHERE resolved_by IS NULL AND frozen_configuration=?", (configuration,),
        ).fetchall()
        for row in pending:
            if regression_resolves(json.loads(row["evidence"]), runs):
                db.execute("UPDATE generation_holds SET resolved_by=? WHERE reference=?", (identifier, row["reference"]))


def report_routes(store: Store, generation_service: Generation) -> APIRouter:
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS evaluation_reports (id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evaluation_regressions (
                report_id TEXT PRIMARY KEY REFERENCES evaluation_reports(id),
                configuration TEXT NOT NULL,
                passing INTEGER NOT NULL,
                runs TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluation_execution_bindings (
                execution_sha256 TEXT PRIMARY KEY,
                outputs_sha256 TEXT NOT NULL UNIQUE,
                binding TEXT NOT NULL
            );
        """)
    router = APIRouter()

    @router.get("/evaluation/reports")
    def reports() -> dict:
        with store.connection() as db:
            return {"reports": [json.loads(row["payload"]) for row in db.execute(
                "SELECT json_remove(payload,'$.regression_runs','$.regression_model','$.regression_configuration') AS payload "
                "FROM evaluation_reports ORDER BY rowid DESC",
            )]}

    @router.post("/evaluation/reports", status_code=201)
    def save(report: ReportImport) -> dict:
        original = report.model_dump()
        identifier = original.pop("report_sha256")
        try:
            actual_identifier = fingerprint(original)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_report", "The evaluation report contains invalid numeric values.") from exc
        if actual_identifier != identifier:
            raise ApiError(409, "report_identity_mismatch", "Use the original evaluation report without rewriting it.")
        if report.decisions.production != "NOT_EXECUTED" and not report.production_evidence_reference:
            raise ApiError(400, "missing_production_evidence", "A measured production gate requires its separate evidence reference.")
        try:
            retrieval = [{
                "repetition": COUNT.validate_python(item["repetition"]), "case_count": COUNT.validate_python(item["case_count"]),
                "failures": len(item["failure_cases"]), "thresholds_met": GATE_RESULT.validate_python(item["retrieval_thresholds_met"]),
                "recall": item["metrics"]["recall"]["point"], "table_recall": item["metrics"]["table_recall"]["point"],
                "returned_precision": item["metrics"]["returned_precision"]["point"],
                "ranked_precision": item["metrics"]["ranked_precision"]["point"],
                "recall_interval": item["metrics"]["recall"]["query_cluster_interval_95"],
            } for item in report.retrieval]
            generation = [{
                "repetition": COUNT.validate_python(item["repetition"]), "thresholds_met": GATE_RESULT.validate_python(item["generation_thresholds_met"]),
                "citation_set_accuracy": item["citation_set_accuracy"]["point"],
                "answer_completeness": item["answer_completeness"]["point"],
                "consequential_errors": COUNT.validate_python(item["consequential_errors"]),
            } for item in report.generation]
            runs = [
                RegressionRun.model_validate(item["regression_run"])
                for item in sorted(report.generation, key=lambda item: item["repetition"]) if "regression_run" in item
            ]
            if runs and (
                len(runs) != 3 or len({run.model_hash for run in runs}) != 1
                or len({run.execution_sha256 for run in runs}) != 3
                or any(
                    run.finished_at < run.started_at or run.execution_sha256 != item["raw_execution_sha256"]
                    for run, item in zip(
                        runs, sorted(report.retrieval, key=lambda item: item["repetition"]), strict=True,
                    )
                )
            ):
                raise ValueError("Incomplete or mismatched regression run evidence")
        except (TypeError, KeyError, ValueError) as exc:
            raise ApiError(400, "invalid_report", "Import the aggregate report produced by the external evaluator.") from exc
        if {item["repetition"] for item in retrieval} != {1, 2, 3}:
            raise ApiError(400, "missing_repetition", "The report must contain each of the three repetitions.")
        if any(
            item["case_count"] != 90
            or (item["thresholds_met"] is True and (
                type(raw["metrics"]["recall"].get("n")) is not int or raw["metrics"]["recall"]["n"] != 45
                or type(raw["metrics"]["table_recall"].get("n")) is not int or raw["metrics"]["table_recall"]["n"] != 15
                or not reaches_threshold(item["recall"], 0.90) or not reaches_threshold(item["table_recall"], 0.80)
            ))
            for item, raw in zip(retrieval, report.retrieval, strict=True)
        ):
            raise ApiError(400, "inconsistent_retrieval_gate", "The report's retrieval decision does not match its case counts or metrics.")
        if report.decisions.retrieval == "PASS":
            try:
                executions = {DIGEST.validate_python(item.get("raw_execution_sha256"), strict=True) for item in report.retrieval}
                if len(executions) != 3 or any(
                    raw.get("incomplete_attempt_cases") != [] or item["recall"] > (90 - item["failures"]) / 45
                    for item, raw in zip(retrieval, report.retrieval, strict=True)
                ):
                    raise ValueError("Repetition evidence is incomplete or repeated")
            except ValueError as exc:
                raise ApiError(400, "inconsistent_retrieval_evidence", "Passing retrieval requires distinct runs and complete attempt evidence.") from exc
        try:
            contradiction = any(
                item["generation_thresholds_met"] is True
                and (
                    item.get("budget_policy_violation") is not False
                    or item.get("regression_run", {}).get("budget_dispatches_checked", 0) <= 0
                    or generation_gate(item) is not True
                )
                for item in report.generation
            )
        except (KeyError, TypeError, ValueError):
            contradiction = True
        if contradiction:
            raise ApiError(400, "inconsistent_generation_gate", "The report's generation decision does not match its recorded metrics.")
        expected = eligibility(
            report.decisions.production,
            [item["thresholds_met"] for item in sorted(retrieval, key=lambda item: item["repetition"])],
            [item["thresholds_met"] for item in sorted(generation, key=lambda item: item["repetition"])],
            bool(generation),
        )
        if expected != report.decisions.model_dump() or (generation and {item["repetition"] for item in generation} != {1, 2, 3}):
            raise ApiError(400, "inconsistent_scope", "The report's eligible scope does not follow its per-repetition evidence.")
        summary = {**original, "retrieval": retrieval, "generation": generation, "report_sha256": identifier}
        configuration = fingerprint([report.protocol_sha256, report.selection_sha256, report.candidate, runs[0].model_hash if runs else None])
        with store.connection(write=True) as db:
            inserted = db.execute("INSERT OR IGNORE INTO evaluation_reports VALUES (?,?)", (identifier, canonical(summary)))
            if inserted.rowcount == 1:
                evidence = []
                for run in runs:
                    binding = {
                        **run.model_dump(mode="json", exclude_defaults=True),
                        "started_at": utc_time(run.started_at), "finished_at": utc_time(run.finished_at),
                        "unresolved_cases": sorted(run.unresolved_cases),
                        "frozen_configuration": configuration,
                    }
                    value = canonical(binding)
                    prior = db.execute(
                        "SELECT binding FROM evaluation_execution_bindings WHERE execution_sha256=? OR outputs_sha256=? "
                        "UNION ALL SELECT json_set(entry.value,'$.frozen_configuration',json_extract(payload,'$.regression_configuration')) "
                        "FROM evaluation_reports, json_each(payload,'$.regression_runs') AS entry "
                        "WHERE json_extract(entry.value,'$.execution_sha256')=? OR json_extract(entry.value,'$.outputs_sha256')=?",
                        (run.execution_sha256, run.outputs_sha256, run.execution_sha256, run.outputs_sha256),
                    )
                    if any(json.loads(row["binding"]) != binding for row in prior):
                        raise ApiError(409, "regression_identity_changed", "A recorded regression execution was presented with a changed identity.")
                    db.execute("INSERT OR IGNORE INTO evaluation_execution_bindings VALUES (?,?,?)", (
                        run.execution_sha256, run.outputs_sha256, value,
                    ))
                    evidence.append(binding)
                if evidence:
                    db.execute("INSERT INTO evaluation_regressions VALUES (?,?,?,?)", (
                        identifier, configuration, report.decisions.generation_eligible, canonical(evidence),
                    ))
                update_generation_holds(db, identifier, report.decisions, configuration, evidence, generation_service)
        return summary

    return router


def utc_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
