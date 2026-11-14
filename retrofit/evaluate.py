"""External evaluator. Gold evidence and adjudications stay outside the workbench."""

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import random
import statistics
import sys
from time import monotonic
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, TypeAdapter
from typing_extensions import TypedDict

from .contracts import Contract, ShortText, Versioned, canonical, fingerprint, now
from .corpus import Opaque
from .generation import ATTEMPT_LIMIT, DEADLINE_SECONDS, budget_policy_identity, maximum_reservation, usage_exceeds_budget
from .hosted_policy import M1_CAP_NANOS, ProviderInfo
from .research import Context, Mode, Question


COVERAGE_MINIMUMS = {
    "hvac_controls": 10, "lighting": 10, "commissioning": 10,
    "table": 5, "multi_passage": 5, "hard_negative": 10, "paired_assumptions": 5,
}
STRATA = dict.fromkeys(("direct", "multi_passage", "table", "unanswerable", "ambiguous_conflicting", "adversarial"), 15)
ANSWERABLE = {"direct", "multi_passage", "table"}
Subtype = Literal["direct", "multi_passage", "table", "unanswerable", "ambiguous_conflicting", "adversarial"]
EvaluationMode = Literal["SELF_EVALUATED", "INDEPENDENT"]
EVALUATOR_FAILURES = {"evaluation_deadline", "evaluation_cancel_failed", "hosted_evaluation_failed"}
SpanSet = Annotated[list[Opaque], Field(min_length=1)]
RequiredUnit = Annotated[list[SpanSet], Field(min_length=1)]


class GoldCase(TypedDict):
    relevance_sets: list[SpanSet]
    required_units: list[RequiredUnit]


GOLD_CASES: TypeAdapter[dict[str, GoldCase]] = TypeAdapter(dict[str, GoldCase])


class Case(Contract):
    case_id: Opaque
    split: Literal["development", "held_out"]
    subtype: Subtype
    leakage_cluster: Opaque
    question: Question
    context: Context = ""
    version_ids: Annotated[list[Opaque], Field(max_length=1000)] = Field(default_factory=list)


class Candidate(Contract):
    snapshot_id: Opaque
    mode: Mode


class Protocol(Versioned):
    mode: EvaluationMode
    builder_uid: int
    custody_reference: ShortText
    builder_exposure: ShortText | None
    candidates: dict[str, Candidate]
    gold_sha256: str
    labels_version: ShortText
    previous_gold_sha256: str | None
    retired_clusters: list[Opaque]
    cases: list[Case]
    predeclared_equal_quality_latency_win: bool = False


class Adjudication(Contract):
    case_id: Opaque
    repetition: Literal[1, 2, 3]
    output_sha256: Opaque
    reviewer_reference: ShortText
    result_status: Literal["answer", "partial", "clarify", "abstain", "error"]
    material_claims: int = Field(ge=0)
    cited_material_claims: int = Field(ge=0)
    sufficient_claims: int = Field(ge=0)
    citation_associations: int = Field(ge=0)
    relevant_citation_associations: int = Field(ge=0)
    required_claims: int = Field(ge=0)
    correct_required_claims: int = Field(ge=0)
    expected_status_met: bool
    consequential_error: bool
    prohibited_adversarial_behavior: bool
    anchors_valid: bool
    table_qualifiers_preserved: bool


def read_json(path: Path) -> dict:
    return json.loads(path.read_bytes())


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def audit_coverage(inventory: dict, coverage: dict) -> dict:
    spans = {span["span_id"]: span for span in inventory["spans"]}
    tables = {row["row_id"]: set(row["ordered_span_ids"]) for row in inventory["table_rows"]}
    counts: Counter[str] = Counter()
    identities = set()
    accepted = []
    rejected = []
    for unit in coverage["units"]:
        category = unit["category"]
        ids = set(unit["span_ids"])
        identity = (category, tuple(sorted(ids)))
        issue = None
        if category not in COVERAGE_MINIMUMS or not ids or not ids <= spans.keys():
            issue = "unknown category or ineligible span"
        elif identity in identities or not isinstance(unit.get("evaluator_note"), str) or not unit["evaluator_note"].strip():
            issue = "duplicate span set or missing evaluator rationale"
        elif category == "table" and not tables.get(unit.get("row_id"), {"missing"}) <= ids:
            issue = "missing required table context"
        elif category in {"multi_passage", "paired_assumptions"} and len(ids) < 2:
            issue = "multiple distinct spans are required"
        if issue:
            rejected.append({"unit_id": unit["unit_id"], "reason": issue})
        else:
            identities.add(identity)
            counts[category] += 1
            accepted.append(unit)
    return {
        "snapshot_id": inventory["snapshot_id"], "eligibility_revision": inventory["eligibility_revision"],
        "inventory_sha256": fingerprint(inventory), "inventory_content_sha256": inventory_content_hash(inventory),
        "documents": inventory["documents"],
        "status": "PASS" if all(counts[k] >= value for k, value in COVERAGE_MINIMUMS.items()) and not rejected else "HOLD",
        "counts": dict(counts), "minimums": COVERAGE_MINIMUMS,
        "accepted": accepted, "rejected": rejected, "recorded_at": now(),
    }


def inventory_content_hash(inventory: dict) -> str:
    return fingerprint({
        "documents": sorted(inventory["documents"], key=lambda item: item["version_id"]),
        "spans": sorted(inventory["spans"], key=lambda item: item["span_id"]),
        "table_rows": sorted(inventory["table_rows"], key=lambda item: item["row_id"]),
    })


def freeze_protocol(source: dict, audit: dict) -> dict:
    protocol = Protocol.model_validate(source)
    if audit["status"] != "PASS" or any(audit["counts"].get(key, 0) < count for key, count in COVERAGE_MINIMUMS.items()):
        raise ValueError("Coverage must pass before benchmark freeze")
    if len(protocol.cases) != 180 or len({case.case_id for case in protocol.cases}) != 180:
        raise ValueError("Require 180 distinct case identifiers")
    if len({(case.question.casefold(), case.context.casefold()) for case in protocol.cases}) != 180:
        raise ValueError("Duplicate exact case inputs are not distinct cases")
    cluster_split: dict[str, str] = {}
    for case in protocol.cases:
        if case.leakage_cluster in protocol.retired_clusters:
            raise ValueError("A previously retired cluster cannot remain in the frozen benchmark")
        previous = cluster_split.setdefault(case.leakage_cluster, case.split)
        development_examples = {"DEV-01", "DEV-02", "DEV-03", "DEV-04", "DEV-05"}
        if previous != case.split or (case.case_id in development_examples and case.split != "development"):
            raise ValueError("A protected split or leakage cluster crossed its boundary")
    for split in ("development", "held_out"):
        if Counter(case.subtype for case in protocol.cases if case.split == split) != STRATA:
            raise ValueError(f"Incorrect stratification in {split}")
    if not protocol.candidates or len(protocol.gold_sha256) != 64:
        raise ValueError("Declare candidate configurations and the separate gold file identity")
    if (protocol.mode == "SELF_EVALUATED") != bool(protocol.builder_exposure):
        raise ValueError("Disclose builder exposure in self-evaluation and keep protected labels separate in independent mode")
    result = {
        **protocol.model_dump(), "coverage": audit, "frozen_at": now(), "budget": {"candidates": 20, "bundles": 8},
        "protocol_version": "M1 §6 D2", "repetitions": 3,
    }
    return {**result, "protocol_sha256": fingerprint(result)}


def verify_frozen(protocol: dict) -> None:
    content = {key: value for key, value in protocol.items() if key != "protocol_sha256"}
    if fingerprint(content) != protocol.get("protocol_sha256"):
        raise ValueError("The frozen protocol changed")


class SessionToken(HTMLParser):
    token: str | None = None

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "meta" and data.get("name") == "retrofit-session":
            self.token = data.get("content")


def client_for(origin: str) -> httpx.Client:
    address = urlsplit(origin)
    if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port or origin != f"http://127.0.0.1:{address.port}":
        raise ValueError("The evaluator connects only to the local loopback application")
    client = httpx.Client(base_url=origin, timeout=35, trust_env=False, follow_redirects=False)
    try:
        parser = SessionToken()
        parser.feed(client.get("/").raise_for_status().text)
        if not parser.token:
            raise ValueError("Local session unavailable")
        client.headers.update({"Origin": origin, "X-Session-Token": parser.token})
    except BaseException:
        client.close()
        raise
    return client


async def cancel_after_deadline(client: httpx.AsyncClient, path: str) -> str:
    try:
        async with asyncio.timeout(0.5):
            (await client.post(f"{path}/cancel", json={})).raise_for_status()
    except (TimeoutError, httpx.HTTPError):
        return "evaluation_cancel_failed"
    return "evaluation_deadline"


async def terminal_evidence(client: httpx.AsyncClient, path: str, status_field: str) -> dict:
    try:
        async with asyncio.timeout(0.15):
            record = (await client.get(path)).raise_for_status().json()
            if record[status_field] != "pending":
                return record
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError):
        pass
    return {"attempts": None}


async def query_case(client: httpx.AsyncClient, candidate: dict, case: dict, *, deadline_seconds: float = 30) -> dict:
    start = monotonic()
    run_id = None
    try:
        async with asyncio.timeout(deadline_seconds):
            response = await client.post("/queries", json={
                "schema_version": "1.1", "mode": candidate["mode"], "snapshot_id": candidate["snapshot_id"],
                "question": case["question"], "context": case["context"],
                "version_ids": case.get("version_ids", []),
            })
            run_id = response.raise_for_status().json()["run_id"]
            while True:
                result = (await client.get(f"/queries/{run_id}")).raise_for_status().json()
                if result["status"] != "pending":
                    break
                await asyncio.sleep(0.06)
            if result["snapshot_id"] != candidate["snapshot_id"] or result["mode_requested"] != candidate["mode"]:
                raise ValueError("Recorded retrieval did not use the frozen candidate")
            return {
                "case_id": case["case_id"], "run_id": run_id, "status": result["status"],
                "failure_code": result.get("failure_code"), "elapsed_seconds": monotonic() - start,
                "ranked_candidates": result["ranked_candidates"],
                "bundles": [
                    {"bundle_id": bundle["bundle_id"], "span_ids": bundle["ordered_span_ids"]}
                    for bundle in result["bundles"]
                ],
                "eligibility_revision": result["eligibility_revision"],
                "attempts": result["attempts"],
            }
    except (TimeoutError, httpx.TimeoutException):
        failure = "evaluation_deadline"
        if run_id is not None:
            failure = await cancel_after_deadline(client, f"/queries/{run_id}")
    except (httpx.HTTPError, ValueError, KeyError):
        failure = "local_dependency_error"
    record = await terminal_evidence(client, f"/queries/{run_id}", "status") if run_id is not None else {}
    return {
        "case_id": case["case_id"], "run_id": run_id, "status": "error",
        "failure_code": failure, "elapsed_seconds": monotonic() - start, "bundles": [], "attempts": record.get("attempts"),
    }


def validate_hosted_manifest(manifest: dict, protocol: dict, split: str, cases: list[dict]) -> None:
    reference = manifest.get("operator_confirmation_reference")
    if (
        set(manifest) != {"protocol_sha256", "split", "operator_confirmation_reference", "pairs"}
        or manifest["protocol_sha256"] != protocol["protocol_sha256"] or manifest["split"] != split
        or not isinstance(reference, str) or not reference.strip()
    ):
        raise ValueError("Hosted evaluation requires a recorded exact-input operator confirmation for this frozen split")
    pairs = [{"case_id": case["case_id"], "question": case["question"], "context": case["context"]} for case in cases]
    if manifest["pairs"] != pairs:
        raise ValueError("The confirmed manifest must contain only the exact ordered case input pairs")


async def generation_case(client: httpx.AsyncClient, outcome: dict, case: dict, split: str, *, deadline_seconds: float = 32) -> dict:
    if outcome["status"] != "result":
        return {"state": "error", "failure_code": "retrieval_failed", "operation_id": None}
    path = f"/queries/{outcome['run_id']}"
    operation = None
    try:
        async with asyncio.timeout(deadline_seconds):
            preview = (await client.get(f"{path}/hosted-input")).raise_for_status().json()
            if (preview["question"], preview["context"]) != (case["question"], case["context"]):
                raise ValueError("Confirmed inputs differ from the saved query")
            response = await client.post(f"{path}/generate", json={
                "schema_version": "1.1", "confirmed": True, "confirmation_id": preview["confirmation_id"],
                "question": case["question"], "context": case["context"], "group_name": split,
            })
            operation = response.raise_for_status().json()["operation_id"]
            while True:
                record = (await client.get(f"/generation/{operation}")).raise_for_status().json()
                if record["state"] != "pending":
                    return record
                await asyncio.sleep(0.15)
    except (TimeoutError, httpx.TimeoutException):
        failure = "evaluation_deadline" if operation is None else await cancel_after_deadline(client, f"/generation/{operation}")
    except (httpx.HTTPError, ValueError, KeyError):
        failure = "hosted_evaluation_failed"
    record = await terminal_evidence(client, f"/generation/{operation}", "state") if operation is not None else {"attempts": None}
    return {**record, "state": "error", "failure_code": failure, "operation_id": operation, "answer": None}


async def evaluate_cases(
    origin: str, headers: httpx.Headers, candidate: dict, cases: list[dict], split: str, hosted: bool,
) -> tuple[list[dict], dict]:
    outcomes = []
    generated = {}
    async with httpx.AsyncClient(base_url=origin, headers=headers, trust_env=False, follow_redirects=False, timeout=None) as client:
        for case in cases:
            outcome = await query_case(client, candidate, case)
            outcomes.append(outcome)
            if hosted:
                generated[case["case_id"]] = await generation_case(client, outcome, case, split)
    return outcomes, generated


def run_comparison(
    protocol: dict, origin: str, label: str, split: str, selection: dict | None, repetition: int,
    hosted_manifest: dict | None = None,
) -> dict:
    verify_frozen(protocol)
    if split == "held_out" and (
        not selection or selection.get("candidate") != label
        or selection.get("protocol_sha256") != protocol["protocol_sha256"]
        or selection.get("selection_sha256") != fingerprint({k: v for k, v in selection.items() if k != "selection_sha256"})
    ):
        raise ValueError("Select and freeze one candidate before held-out execution")
    candidate = protocol["candidates"][label]
    cases = [case for case in protocol["cases"] if case["split"] == split]
    if hosted_manifest is not None:
        validate_hosted_manifest(hosted_manifest, protocol, split, cases)
    with closing(client_for(origin)) as client:
        inventory = client.get(f"/snapshots/{candidate['snapshot_id']}/evaluation-inventory").raise_for_status().json()
        expected = protocol["coverage"]
        if (
            inventory_content_hash(inventory) != expected["inventory_content_sha256"]
            or inventory["eligibility_revision"] != expected["eligibility_revision"]
        ):
            raise ValueError("Candidates must use the same audited source and eligibility state")
        available = client.get("/snapshots").raise_for_status().json()["snapshots"]
        observed = next(item for item in available if item["snapshot_id"] == candidate["snapshot_id"])
        if (
            observed["retrieval_settings"]["candidate_limit"] != protocol["budget"]["candidates"]
            or observed["retrieval_settings"]["final_bundle_limit"] != protocol["budget"]["bundles"]
            or candidate["mode"] not in observed["enabled_modes"]
        ):
            raise ValueError("Candidate mode or retrieval budget did not match the frozen protocol")
        if hosted_manifest is not None:
            state = client.get("/generation").raise_for_status().json()
            if not state["configured"] or not state["enabled"]:
                raise ValueError("Hosted evaluation requires separately authorized and enabled generation")
        started = now()
        outcomes, generated = asyncio.run(evaluate_cases(origin, client.headers, candidate, cases, split, hosted_manifest is not None))
    if any(row.get("eligibility_revision", expected["eligibility_revision"]) != expected["eligibility_revision"] for row in outcomes):
        raise ValueError("The eligible corpus changed during the comparison")
    return {
        "protocol_sha256": protocol["protocol_sha256"], "candidate": label, "config": candidate,
        "observed_pipeline": observed,
        "eligibility_revision": expected["eligibility_revision"], "split": split, "repetition": repetition,
        "selection_sha256": selection["selection_sha256"] if split == "held_out" and selection else None,
        "started_at": started, "finished_at": now(), "outcomes": outcomes,
        "hosted_confirmation_manifest_sha256": fingerprint(hosted_manifest) if hosted_manifest is not None else None,
        "generated_outputs": generated,
    }


def checked_gold(protocol: dict, path: Path) -> dict:
    if protocol["mode"] == "INDEPENDENT":
        info = path.stat()
        parent = path.parent.stat()
        if (
            os.getuid() == protocol["builder_uid"] or info.st_uid != os.getuid() or parent.st_uid != os.getuid()
            or info.st_mode & 0o077 or parent.st_mode & 0o077
        ):
            raise ValueError("Independent mode requires a separate custodian and private gold directory")
    data = path.read_bytes()
    if sha256(data).hexdigest() != protocol["gold_sha256"]:
        raise ValueError("Gold label identity does not match the frozen protocol")
    gold = json.loads(data)
    if set(gold["cases"]) != {case["case_id"] for case in protocol["cases"]}:
        raise ValueError("Gold must cover the exact frozen case inventory")
    try:
        GOLD_CASES.validate_python(gold["cases"], strict=True)
    except ValueError as exc:
        raise ValueError("Gold labels require nonempty source-span sets for each relevance set and unit alternative") from exc
    return gold


def retrieval_metrics(bundles: list[dict], gold: dict) -> dict:
    ranked = [set(bundle["span_ids"]) for bundle in bundles[:8]]
    relevance = [any(set(group) <= spans for group in gold["relevance_sets"]) for spans in ranked]
    count = sum(relevance)
    positions = [index for index, value in enumerate(relevance, 1) if value]
    covered = set().union(*ranked)
    units = gold["required_units"]
    hit = sum(any(set(alternative) <= covered for alternative in unit) for unit in units)
    return {
        "returned_precision": count / len(ranked) if ranked else 0.0,
        "ranked_precision": sum(order / position for order, position in enumerate(positions, 1)) / count if count else 0.0,
        "recall": hit / len(units) if units else None,
        "returned": len(ranked), "relevant": count, "required_units": len(units), "covered_units": hit,
        "p_at_8": count / 8,
    }


def clustered_interval(values: list[tuple[str, float]]) -> list[float] | None:
    if not values:
        return None
    groups: dict[str, list[float]] = defaultdict(list)
    for name, value in values:
        groups[name].append(value)
    clusters = list(groups.values())
    rng = random.Random(1729)
    means = [
        statistics.mean(value for group in rng.choices(clusters, k=len(clusters)) for value in group)
        for _ in range(1_000)
    ]
    means.sort()
    return [means[25], means[974]]


def summarize_retrieval(protocol: dict, gold: dict, execution: dict) -> dict:
    if (
        execution["protocol_sha256"] != protocol["protocol_sha256"]
        or execution["config"] != protocol["candidates"].get(execution["candidate"])
        or execution["eligibility_revision"] != protocol["coverage"]["eligibility_revision"]
    ):
        raise ValueError("Execution does not bind the frozen protocol")
    cases = {case["case_id"]: case for case in protocol["cases"] if case["split"] == execution["split"]}
    outcomes = {outcome["case_id"]: outcome for outcome in execution["outcomes"]}
    if set(cases) != set(outcomes) or len(outcomes) != len(execution["outcomes"]):
        raise ValueError("Every frozen case requires exactly one outcome per repetition")
    if any(
        row["status"] == "result" and (
            not row["run_id"] or not row["attempts"]
            or row.get("eligibility_revision") != protocol["coverage"]["eligibility_revision"]
        ) for row in outcomes.values()
    ):
        raise ValueError("Each successful outcome requires a recorded attempt and frozen eligibility identity")
    rows = []
    for case_id, case in cases.items():
        outcome = outcomes[case_id]
        if case["subtype"] in ANSWERABLE:
            score = retrieval_metrics(outcome["bundles"] if outcome["status"] == "result" else [], gold["cases"][case_id])
            rows.append({**score, "case_id": case_id, "subtype": case["subtype"], "cluster": case["leakage_cluster"]})
    metrics: dict[str, dict[str, Any]] = {}
    for key in ("returned_precision", "ranked_precision", "recall", "p_at_8"):
        values = [(row["cluster"], row[key]) for row in rows if row[key] is not None]
        metrics[key] = {
            "point": statistics.mean(value for _, value in values) if values else None,
            "n": len(values), "query_cluster_interval_95": clustered_interval(values),
        }
    table = [row["recall"] for row in rows if row["subtype"] == "table" and row["recall"] is not None]
    metrics["table_recall"] = {"point": statistics.mean(table) if table else None, "n": len(table)}
    failures = [row["case_id"] for row in outcomes.values() if row["status"] != "result"]
    incomplete_attempts = [row["case_id"] for row in outcomes.values() if not row.get("attempts")]
    recall = metrics["recall"]["point"]
    table_recall = metrics["table_recall"]["point"]
    threshold = None if (
        incomplete_attempts or recall is None or table_recall is None or metrics["recall"]["n"] != 45 or len(table) != 15
    ) else recall >= 0.90 and table_recall >= 0.80
    return {
        "candidate": execution["candidate"], "repetition": execution["repetition"], "metrics": metrics,
        "case_count": len(cases), "failure_cases": failures, "case_scores": rows,
        "incomplete_attempt_cases": incomplete_attempts,
        "retrieval_thresholds_met": threshold, "raw_execution_sha256": fingerprint(execution),
    }


def eligibility(
    production: str, retrieval: list[bool | None], generation: list[bool | None], generation_enabled: bool,
) -> dict:
    retrieval_status = "FAIL" if False in retrieval else "PASS" if retrieval == [True, True, True] else "INSUFFICIENT_EVIDENCE"
    generation_status = "FAIL" if False in generation else "PASS" if generation == [True, True, True] else "UNMEASURED"
    scope = "WITHHELD"
    if production == "PASS" and retrieval_status == "PASS":
        scope = "WORKBENCH_AND_GENERATION" if generation_status == "PASS" and generation_enabled else "WORKBENCH_ONLY"
    return {
        "production": production, "retrieval": retrieval_status, "generation": generation_status,
        "eligible_scope": scope, "generation_eligible": scope == "WORKBENCH_AND_GENERATION",
        "owner_acceptance": "NOT_RECORDED", "release": "NOT_AUTHORIZED",
    }


def choose_candidate(protocol: dict, gold: dict, executions: list[dict]) -> dict:
    if set(protocol["candidates"]) != {run["candidate"] for run in executions} or any(run["split"] != "development" for run in executions):
        raise ValueError("Development evidence is required for every declared candidate")
    if len(executions) != len(protocol["candidates"]):
        raise ValueError("Supply one complete development execution per candidate")
    summaries = {run["candidate"]: summarize_retrieval(protocol, gold, run) for run in executions}
    baseline = [label for label, cfg in protocol["candidates"].items() if cfg["mode"] == "lexical"]
    if not baseline:
        raise ValueError("Retain a measured lexical baseline")

    def point(label: str, key: str) -> float:
        value = summaries[label]["metrics"][key]["point"]
        if value is None:
            raise ValueError(f"{label} is missing {key} evidence")
        return float(value)

    def quality(label: str) -> tuple[float, float, float, str]:
        return (point(label, "recall"), point(label, "table_recall"), point(label, "returned_precision"), label)

    runs = {run["candidate"]: run for run in executions}
    p95 = {}
    for name, execution in runs.items():
        durations = [row["elapsed_seconds"] for row in execution["outcomes"]]
        p95[name] = statistics.quantiles(durations, n=20, method="inclusive")[18] if len(durations) > 1 else None
    lexical = max(baseline, key=quality)
    comparison: dict[str, dict[str, Any]] = {}
    baseline_cases = {row["case_id"]: row for row in summaries[lexical]["case_scores"]}
    for label, summary in summaries.items():
        paired = [
            (row["cluster"], row["recall"] - baseline_cases[row["case_id"]]["recall"])
            for row in summary["case_scores"] if row["recall"] is not None and baseline_cases[row["case_id"]]["recall"] is not None
        ]
        comparison[label] = {
            "paired_recall_difference": statistics.mean(value for _, value in paired) if paired else None,
            "query_cluster_interval_95": clustered_interval(paired),
            "measured_development_p95_seconds": p95[label],
            "summary": summary,
        }
    candidate_comparisons = []
    eligible = {label for label in summaries if label not in baseline}
    for label in sorted(eligible):
        run = runs[label]
        pipeline = run.get("observed_pipeline", {})
        if (pipeline.get("embedding") or {}).get("key") != "bge-small":
            continue
        for other, result in runs.items():
            challenger = result.get("observed_pipeline", {})
            if (
                other not in eligible or (challenger.get("embedding") or {}).get("key") != "bge-base"
                or pipeline["chunking"]["strategy"] != challenger["chunking"]["strategy"]
                or run["config"]["mode"] != result["config"]["mode"]
            ):
                continue
            difference = point(label, "recall") - point(other, "recall")
            small_p95, base_p95 = p95[label], p95[other]
            prefer_small = difference >= -0.02 and small_p95 is not None and base_p95 is not None and small_p95 < base_p95
            if prefer_small:
                eligible.discard(other)
            candidate_comparisons.append({
                "small": label, "base": other, "absolute_recall_difference": difference,
                "small_preferred_at_measured_development_p95": prefer_small,
            })
    challengers = []
    for label in sorted(eligible):
        paired_difference = comparison[label]["paired_recall_difference"]
        interval = comparison[label]["query_cluster_interval_95"]
        margin = (
            paired_difference is not None and paired_difference >= 0.05 - 1e-12
            and point(label, "returned_precision") - point(lexical, "returned_precision") >= -0.02 - 1e-12
        )
        candidate_p95, lexical_p95 = p95[label], p95[lexical]
        equal_latency = (
            protocol.get("predeclared_equal_quality_latency_win", False) and quality(label)[:3] == quality(lexical)[:3]
            and candidate_p95 is not None and lexical_p95 is not None and candidate_p95 < lexical_p95
        )
        if paired_difference is not None and interval is not None and ((margin and interval[0] > 0) or equal_latency):
            challengers.append(label)
    selected = max(challengers, key=quality) if challengers else lexical
    selection = {
        "protocol_sha256": protocol["protocol_sha256"], "candidate": selected, "lexical_baseline": lexical,
        "comparison": comparison, "embedding_candidate_comparisons": candidate_comparisons, "frozen_at": now(),
        "selection_note": "Development evidence only. The chosen configuration is frozen before held-out assessment.",
        "limitations": [
            "Development p95 is a comparative runtime measurement. The separate 200-request performance goal needs its own measurement.",
            "Sequential comparison does not establish a global optimum. Generation candidate selection needs authorized actual output evidence.",
        ],
    }
    return {**selection, "selection_sha256": fingerprint(selection)}


def generation_gate(metrics: dict) -> bool | None:
    def ratio_point(ratio: dict, cases: int) -> float | None:
        point = ratio["point"]
        numerator, denominator = ratio["numerator"], ratio["denominator"]
        not_applicable = ratio["n_a_cases"]
        if (
            type(numerator) is not int or type(denominator) is not int or not 0 <= numerator <= denominator
            or type(not_applicable) is not int or not 0 <= not_applicable <= cases
            or not int(bool(denominator)) <= cases - not_applicable <= denominator
            or point != (numerator / denominator if denominator else None)
        ):
            raise ValueError("Generation ratio does not match its observed counts")
        return point

    answer = metrics["answer_completeness"]
    accuracy = metrics["citation_set_accuracy"]
    completeness = metrics["citation_completeness"]
    subtypes = [metrics["citation_accuracy_by_subtype"][key] for key in sorted(ANSWERABLE)]
    gates = [
        (ratio_point(accuracy, 90), 0.98), (ratio_point(metrics["citation_relevance"], 90), 0.95),
        (ratio_point(completeness, 90), 0.98), (answer["point"] if answer["n"] == 45 else None, 0.90),
        *((ratio_point(subtype, 15), 0.95) for subtype in subtypes),
    ]
    supported = sum(subtype["numerator"] for subtype in subtypes)
    unsupported = sum(subtype["denominator"] - subtype["numerator"] for subtype in subtypes)
    if (
        accuracy["numerator"] < supported or accuracy["denominator"] - accuracy["numerator"] < unsupported
        or completeness["numerator"] != accuracy["numerator"] or completeness["denominator"] < accuracy["denominator"]
    ):
        raise ValueError("Overall citation counts disagree with their components")
    if any(point is not None and (type(point) not in {int, float} or not 0 <= point <= 1) for point, _ in gates):
        raise ValueError("Invalid generation metric")
    counts = (
        ("consequential_errors", 0, 0), ("adversarial_violations", 0, 0), ("false_abstentions", 0, 4),
        ("unanswerable_correct", 14, 15), ("ambiguous_correct", 14, 15),
    )
    if (
        not all(type(metrics[key]) is int and lower <= metrics[key] <= upper for key, lower, upper in counts)
        or metrics["anchors_valid"] is not True or metrics["table_qualifiers_preserved"] is not True
        or any(point is not None and point < target for point, target in gates)
    ):
        return False
    return None if any(point is None for point, _ in gates) else True


def summarize_generation(cases: dict, entries: list[Adjudication], outputs: dict) -> dict:
    if len(entries) != len(cases) or {entry.case_id for entry in entries} != cases.keys():
        raise ValueError("Human adjudication must account for every held-out case")
    groups: dict[str, list[Adjudication]] = defaultdict(list)
    for entry in entries:
        output = outputs.get(entry.case_id)
        if output is None or fingerprint(output) != entry.output_sha256:
            raise ValueError("Human adjudication must bind the exact recorded output or task failure")
        answer = output.get("answer")
        observed = answer.get("status") if output.get("state") == "result" and isinstance(answer, dict) else "error"
        if entry.result_status != observed:
            raise ValueError("Human adjudication must match the recorded result status")
        if (
            not entry.sufficient_claims <= entry.cited_material_claims <= entry.material_claims
            or entry.relevant_citation_associations > entry.citation_associations
            or entry.correct_required_claims > entry.required_claims
            or (observed in {"error", "abstain", "clarify"} and any(
                (entry.material_claims, entry.citation_associations, entry.correct_required_claims)
            ))
        ):
            raise ValueError("Invalid human scoring counts")
        groups[cases[entry.case_id]["subtype"]].append(entry)

    def ratio(items: list[Adjudication], count: str, total: str) -> dict:
        numerator = sum(getattr(item, count) for item in items)
        denominator = sum(getattr(item, total) for item in items)
        return {
            "point": numerator / denominator if denominator else None, "numerator": numerator, "denominator": denominator,
            "n_a_cases": sum(not getattr(item, total) for item in items),
        }

    accuracy = ratio(entries, "sufficient_claims", "cited_material_claims")
    subtype_accuracy = {key: ratio(groups[key], "sufficient_claims", "cited_material_claims") for key in sorted(ANSWERABLE)}
    relevance = ratio(entries, "relevant_citation_associations", "citation_associations")
    completeness = ratio(entries, "sufficient_claims", "material_claims")
    answerable = [item for key in ANSWERABLE for item in groups[key]]
    per_question = [
        (cases[item.case_id]["leakage_cluster"], item.correct_required_claims / item.required_claims)
        for item in answerable if item.required_claims
    ]
    answer_completeness = statistics.mean(value for _, value in per_question) if per_question else None
    consequential = sum(item.consequential_error for item in entries)
    unsafe = sum(item.prohibited_adversarial_behavior for item in groups["adversarial"])
    false_abstention = sum(item.result_status == "abstain" for item in answerable)
    unanswerable_correct = sum(item.expected_status_met and item.result_status != "error" for item in groups["unanswerable"])
    ambiguous_correct = sum(item.expected_status_met and item.result_status != "error" for item in groups["ambiguous_conflicting"])
    result = {
        "citation_set_accuracy": accuracy, "citation_accuracy_by_subtype": subtype_accuracy,
        "citation_relevance": relevance, "citation_completeness": completeness,
        "answer_completeness": {"point": answer_completeness, "n": len(per_question), "query_cluster_interval_95": clustered_interval(per_question)},
        "consequential_errors": consequential, "adversarial_violations": unsafe, "false_abstentions": false_abstention,
        "unanswerable_correct": unanswerable_correct, "ambiguous_correct": ambiguous_correct,
        "anchors_valid": all(item.anchors_valid for item in entries),
        "table_qualifiers_preserved": all(item.table_qualifiers_preserved for item in groups["table"]),
        "failed_cases": [item.case_id for item in entries if item.result_status == "error"],
    }
    return {**result, "generation_thresholds_met": generation_gate(result)}


def recorded_budget_outputs(origin: str, operations: list[str]) -> dict:
    with closing(client_for(origin)) as client:
        records = {operation: client.get(f"/generation/{operation}").raise_for_status().json() for operation in operations}
    if any(record["state"] == "pending" for record in records.values()):
        raise ValueError("Wait for the recorded generation operations to finish")
    return records


def refresh_billing(origin: str, execution: dict) -> dict:
    outputs = execution["generated_outputs"]
    records = recorded_budget_outputs(origin, [
        item["operation_id"] for item in outputs.values() if item.get("operation_id")
    ])
    billing = {"actual_nanos", "cost_status", "billing_reference"}
    evaluation_fields = {"state", "failure_code", "answer", "attempts", "finished_at"}
    run_ids = {row["case_id"]: row["run_id"] for row in execution.get("outcomes", [])}
    enriched = {}
    for case, original in outputs.items():
        operation = original.get("operation_id")
        if operation not in records:
            enriched[case] = original
            continue
        updated = records[operation]
        if original.get("failure_code") in EVALUATOR_FAILURES:
            if (
                updated["run_id"] != run_ids.get(case)
                or any(updated.get(key) != value for key, value in original.items() if key not in evaluation_fields)
            ):
                raise ValueError("The recorded operation does not bind its recorded retrieval")
            enriched[case] = {**updated, "state": "error", "failure_code": original["failure_code"], "answer": None}
            continue
        fixed = lambda output: {key: value for key, value in output.items() if key != "attempts"}
        immutable_attempts = lambda output: [
            {key: value for key, value in attempt.items() if key not in billing} for attempt in output["attempts"]
        ]
        if fixed(updated) != fixed(original) or immutable_attempts(updated) != immutable_attempts(original):
            raise ValueError("The recorded operation changed outside billing reconciliation")
        enriched[case] = updated
    return {**execution, "billing_source_sha256": fingerprint(execution), "generated_outputs": enriched}


def assess_budget(outputs: dict) -> tuple[bool | None, int, str | None, list[str]]:
    count = 0
    incomplete_policy = False
    cases: set[str] = set()
    policies: set[str] = set()
    for output in outputs.values():
        checked_case = False
        incomplete_case = False
        if output.get("failure_code") == "provider_token_limit":
            return True, 0, None, []
        attempts = output.get("attempts", () if output.get("operation_id") is None else None)
        if attempts is None:
            incomplete_policy = True
            continue
        for attempt in attempts:
            dispatch = attempt.get("dispatch_at")
            actual = attempt.get("actual_nanos")
            if not dispatch:
                if actual not in (None, 0) or attempt.get("estimated_nanos") not in (None, 0):
                    return True, 0, None, []
                continue
            input_count, output_count = attempt.get("usage_input"), attempt.get("usage_output")
            reserved, estimate = attempt.get("reserved_nanos"), attempt.get("estimated_nanos")
            provider = ProviderInfo(**output["provider"])
            deadline = output.get("deadline_at")
            confirmed, finished = output.get("confirmed_at"), output.get("finished_at")
            cap, committed, prior = (attempt.get(key) for key in ("authority_maximum_nanos", "committed_nanos_at_dispatch", "dispatch_count_before"))
            if (
                not output.get("authority_reference") or not output.get("operation_id") or not output.get("run_id")
                or not confirmed or not finished
                or type(reserved) is not int or reserved != maximum_reservation(provider)
                or (cap is not None and (type(cap) is not int or not 0 < cap <= M1_CAP_NANOS))
                or (committed is not None and (type(committed) is not int or committed < reserved or (cap is not None and committed > cap)))
                or (prior is not None and (type(prior) is not int or not 0 <= prior < ATTEMPT_LIMIT))
                or usage_exceeds_budget(input_count, output_count)
                or (estimate is not None and (type(estimate) is not int or not 0 <= estimate <= reserved))
                or (actual is not None and (type(actual) is not int or not 0 <= actual <= reserved))
                or (deadline and datetime.fromisoformat(dispatch) >= datetime.fromisoformat(deadline))
                or not datetime.fromisoformat(confirmed) <= datetime.fromisoformat(dispatch) <= datetime.fromisoformat(finished)
            ):
                return True, 0, None, []
            policy_recorded = deadline and None not in (cap, committed, prior)
            if policy_recorded and None not in (input_count, output_count, estimate, actual):
                count += 1
                checked_case = True
            else:
                incomplete_policy = True
                incomplete_case = True
        if checked_case and not incomplete_case and output["state"] == "result":
            policies.add(budget_policy_identity(provider, output.get("deadline_seconds", DEADLINE_SECONDS)))
            cases.add(output["budget_case_sha256"])
    return None if incomplete_policy or not count else False, count, next(iter(policies)) if len(policies) == 1 else None, sorted(cases)


def final_report(
    protocol: dict, gold: dict, selection: dict, executions: list[dict],
    production_evidence: dict | None, adjudications: dict | None,
    budget_regressions: list[dict] | None = None,
) -> dict:
    if (
        selection["protocol_sha256"] != protocol["protocol_sha256"]
        or selection["selection_sha256"] != fingerprint({k: v for k, v in selection.items() if k != "selection_sha256"})
        or {item["repetition"] for item in executions} != {1, 2, 3} or len(executions) != 3
        or any(item["split"] != "held_out" or item["candidate"] != selection["candidate"]
               or item["selection_sha256"] != selection["selection_sha256"] for item in executions)
    ):
        raise ValueError("Report requires three distinct held-out repetitions of the one frozen pipeline")
    ordered = sorted(executions, key=lambda item: item["repetition"])
    run_ids = [row["run_id"] for item in ordered for row in item["outcomes"] if row["run_id"]]
    if len(run_ids) != len(set(run_ids)) or any(
        item["started_at"] < selection["frozen_at"] or item["finished_at"] < item["started_at"] for item in ordered
    ):
        raise ValueError("Each repetition requires separate run identities after selection was frozen")
    retrieval = [summarize_retrieval(protocol, gold, run) for run in ordered]
    generation = []
    budget_records = budget_regressions or [{}, {}, {}]
    recovery_ids = [key for repetition in budget_records for key in repetition]
    operation_ids = [
        output["operation_id"] for run in ordered for output in run.get("generated_outputs", {}).values() if output.get("operation_id")
    ]
    all_operations = [*operation_ids, *recovery_ids]
    if (
        len(budget_records) != 3 or any(len(repetition) > 90 for repetition in budget_records)
        or len(all_operations) != len(set(all_operations)) or (recovery_ids and not adjudications)
    ):
        raise ValueError("Use distinct budget regression operations for three adjudicated repetitions")
    if adjudications:
        entries = [Adjudication.model_validate(row) for row in adjudications["reviews"]]
        cases = {case["case_id"]: case for case in protocol["cases"] if case["split"] == "held_out"}
        model_hashes = {
            result["model_hash"] for item in ordered for result in item.get("generated_outputs", {}).values()
            if "model_hash" in result
        }
        if len(model_hashes) != 1 or any(not item.get("hosted_confirmation_manifest_sha256") for item in ordered):
            raise ValueError("Generation assessment needs actual outputs from one frozen model and recorded input manifests")
        for item, recovery in zip(ordered, budget_records, strict=True):
            repetition = item["repetition"]
            actual = item["generated_outputs"]
            reviews = [review for review in entries if review.repetition == repetition]
            retrieved = {outcome["case_id"]: outcome["run_id"] for outcome in item["outcomes"]}
            if any(
                output.get("run_id") != retrieved.get(case)
                or not item["started_at"] <= output["confirmed_at"] <= min(output["finished_at"], item["finished_at"])
                or (output["finished_at"] > item["finished_at"] and (
                    output["state"] != "error" or output.get("failure_code") not in EVALUATOR_FAILURES
                ))
                for case, output in actual.items() if output.get("operation_id") and "confirmed_at" in output
            ):
                raise ValueError("Generated output must bind its recorded retrieval and repetition")
            if any(
                output["model_hash"] not in model_hashes or output["operation_id"] != key
                or output["state"] != "result" or output["confirmed_at"] > output["finished_at"]
                for key, output in recovery.items()
            ):
                raise ValueError("Each budget regression must bind its recorded operation, model and repetition")
            checked_outputs = {**actual, **{f"budget/{key}": output for key, output in recovery.items()}}
            violation, checked, policy, budget_cases = assess_budget(checked_outputs)
            scores = summarize_generation(cases, reviews, actual)
            if violation or (violation is None and scores["generation_thresholds_met"] is True):
                scores["generation_thresholds_met"] = None if violation is None else False
            generation.append({
                "repetition": repetition,
                **scores,
                "budget_policy_violation": violation,
                "regression_run": {
                    "model_hash": next(iter(model_hashes)),
                    "execution_sha256": fingerprint(item),
                    "outputs_sha256": fingerprint(checked_outputs),
                    "started_at": min(item["started_at"], *(output["confirmed_at"] for output in recovery.values()))
                    if recovery else item["started_at"],
                    "finished_at": max(item["finished_at"], *(output["finished_at"] for output in recovery.values()))
                    if recovery else item["finished_at"],
                    "budget_dispatches_checked": checked, "budget_policy_sha256": policy, "budget_case_hashes": budget_cases,
                    "unresolved_cases": sorted(fingerprint(review.case_id) for review in reviews if (
                        not review.expected_status_met or review.result_status == "error" or review.consequential_error
                        or review.prohibited_adversarial_behavior or not review.anchors_valid or not review.table_qualifiers_preserved
                        or review.correct_required_claims != review.required_claims
                        or review.sufficient_claims != review.material_claims
                        or review.relevant_citation_associations != review.citation_associations
                    )),
                },
            })
    production = "NOT_EXECUTED"
    if production_evidence:
        production = production_evidence["status"]
        if production not in {"PASS", "FAIL", "NOT_EXECUTED"} or not production_evidence["evidence_reference"]:
            raise ValueError("Production status requires its separate evidence reference")
    decisions = eligibility(
        production, [item["retrieval_thresholds_met"] for item in retrieval],
        [item["generation_thresholds_met"] for item in generation], bool(adjudications),
    )
    report = {
        "schema_version": "1.1", "evaluation_mode": protocol["mode"], "protocol_sha256": protocol["protocol_sha256"],
        "selection_sha256": selection["selection_sha256"], "candidate": selection["candidate"],
        "custody_reference": protocol["custody_reference"], "builder_exposure": protocol["builder_exposure"],
        "decisions": decisions, "retrieval": retrieval, "generation": generation,
        "production_evidence_reference": production_evidence["evidence_reference"] if production_evidence else None,
        "limitations": [
            "Thresholds use point estimates for each repetition. Repetitions are not independent samples.",
            "Owner acceptance and application release require separate recorded decisions.",
            *([] if generation else ["Generated-answer quality and actual provider results are unmeasured."]),
        ],
        "recorded_at": now(),
    }
    return {**report, "report_sha256": fingerprint(report)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="Read eligible source identities from the running workbench")
    inventory.add_argument("--origin", required=True)
    inventory.add_argument("--snapshot", required=True)
    audit = commands.add_parser("audit", help="Check admitted-source coverage before benchmark freeze")
    audit.add_argument("inventory", type=Path)
    audit.add_argument("coverage", type=Path)
    freeze = commands.add_parser("freeze", help="Bind stratified cases, custody, candidates and label identity")
    freeze.add_argument("source", type=Path)
    freeze.add_argument("audit", type=Path)
    run = commands.add_parser("run", help="Run one retrieval comparison on the live local application")
    run.add_argument("protocol", type=Path)
    run.add_argument("--origin", required=True)
    run.add_argument("--candidate", required=True)
    run.add_argument("--split", choices=["development", "held_out"], required=True)
    run.add_argument("--selection", type=Path)
    run.add_argument("--confirmed-hosted-inputs", type=Path)
    run.add_argument("--repetition", type=int, choices=[1, 2, 3], default=1)
    select = commands.add_parser("select", help="Select the pipeline from complete development results")
    select.add_argument("protocol", type=Path)
    select.add_argument("gold", type=Path)
    select.add_argument("executions", nargs="+", type=Path)
    report = commands.add_parser("report", help="Report each held-out repetition and the D6 eligibility decision")
    report.add_argument("protocol", type=Path)
    report.add_argument("gold", type=Path)
    report.add_argument("selection", type=Path)
    report.add_argument("executions", nargs=3, type=Path)
    report.add_argument("--production-evidence", type=Path)
    report.add_argument("--adjudications", type=Path)
    report.add_argument("--budget-regressions", type=Path, nargs=3)
    billing = commands.add_parser("billing", help="Read settled billing from exact saved local generation operations")
    billing.add_argument("--origin", required=True)
    billing_source = billing.add_mutually_exclusive_group(required=True)
    billing_source.add_argument("--execution", type=Path)
    billing_source.add_argument("--operations", nargs="+")
    for item in (inventory, audit, freeze, run, select, report, billing):
        item.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "inventory":
        with closing(client_for(args.origin)) as client:
            result = client.get(f"/snapshots/{args.snapshot}/evaluation-inventory").raise_for_status().json()
    elif args.command == "audit":
        result = audit_coverage(read_json(args.inventory), read_json(args.coverage))
    elif args.command == "freeze":
        result = freeze_protocol(read_json(args.source), read_json(args.audit))
    elif args.command == "run":
        result = run_comparison(
            read_json(args.protocol), args.origin, args.candidate, args.split,
            read_json(args.selection) if args.selection else None, args.repetition,
            read_json(args.confirmed_hosted_inputs) if args.confirmed_hosted_inputs else None,
        )
    elif args.command == "billing":
        result = refresh_billing(args.origin, read_json(args.execution)) if args.execution else {
            "generated_outputs": recorded_budget_outputs(args.origin, args.operations),
        }
    else:
        protocol = read_json(args.protocol)
        verify_frozen(protocol)
        gold = checked_gold(protocol, args.gold)
        executions = [read_json(path) for path in args.executions]
        result = choose_candidate(protocol, gold, executions) if args.command == "select" else final_report(
            protocol, gold, read_json(args.selection), executions,
            read_json(args.production_evidence) if args.production_evidence else None,
            read_json(args.adjudications) if args.adjudications else None,
            [read_json(path)["generated_outputs"] for path in args.budget_regressions] if args.budget_regressions else None,
        )
    write_json(args.out, result)


if __name__ == "__main__":
    main()
