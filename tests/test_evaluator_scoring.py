from copy import deepcopy

import pytest

from retrofit.contracts import fingerprint
from retrofit.evaluate import (
    ANSWERABLE, Adjudication, choose_candidate, final_report, freeze_protocol,
    summarize_generation, summarize_retrieval,
)
from test_evaluator_boundaries import passing_audit, protocol_input


def frozen_candidates():
    source = protocol_input()
    source["candidates"].update({
        "small": {"snapshot_id": "vector-small", "mode": "dense"},
        "base": {"snapshot_id": "vector-base", "mode": "dense"},
        "hybrid": {"snapshot_id": "vector-hybrid", "mode": "hybrid"},
    })
    protocol = freeze_protocol(source, passing_audit())
    gold = {"cases": {
        case["case_id"]: {"relevance_sets": [["qualified"]], "required_units": [[["qualified"]]]}
        for case in protocol["cases"]
    }}
    return protocol, gold


def execution(protocol, label, *, split="development", repetition=1, selection=None):
    candidate = protocol["candidates"][label]
    cases = [case for case in protocol["cases"] if case["split"] == split]
    outcomes = []
    for index, case in enumerate(cases):
        eligible = case["subtype"] in ANSWERABLE and (label != "lexical" or index % 5 != 0)
        outcomes.append({
            "case_id": case["case_id"], "run_id": f"{label}-{split}-{repetition}-{index}",
            "status": "result", "eligibility_revision": 2, "attempts": [{"status": "result"}],
            "elapsed_seconds": {"lexical": 0.3, "small": 0.15, "base": 0.25, "hybrid": 0.6}[label],
            "bundles": [{"span_ids": ["qualified"]}] if eligible else [],
        })
    return {
        "protocol_sha256": protocol["protocol_sha256"], "candidate": label, "config": candidate,
        "observed_pipeline": {
            "embedding": None if label == "lexical" else {"key": "bge-base" if label == "base" else "bge-small"},
            "chunking": {"strategy": "fixed" if label == "hybrid" else "structure"},
        },
        "eligibility_revision": 2, "split": split, "repetition": repetition, "outcomes": outcomes,
        "selection_sha256": selection["selection_sha256"] if selection else None,
        "started_at": "2099-01-01T00:00:00Z", "finished_at": "2099-01-01T00:00:01Z",
    }


def test_real_scoring_selects_best_valid_pinned_candidate_and_rejects_incomplete_development():
    protocol, gold = frozen_candidates()
    runs = [execution(protocol, name) for name in protocol["candidates"]]
    selection = choose_candidate(protocol, gold, runs)
    assert selection["candidate"] == "small" and selection["lexical_baseline"] == "lexical"
    comparisons = selection["embedding_candidate_comparisons"]
    assert comparisons == [{
        "small": "small", "base": "base", "absolute_recall_difference": 0.0,
        "small_preferred_at_measured_development_p95": True,
    }]
    assert selection["comparison"]["lexical"]["summary"]["metrics"]["recall"]["point"] == 0.8
    assert selection["comparison"]["small"]["paired_recall_difference"] == pytest.approx(0.2)
    with pytest.raises(ValueError, match="every declared candidate"):
        choose_candidate(protocol, gold, runs[:-1])
    with pytest.raises(ValueError, match="one complete development"):
        choose_candidate(protocol, gold, [*runs, runs[0]])
    without_baseline = {**protocol, "candidates": {name: cfg for name, cfg in protocol["candidates"].items() if name != "lexical"}}
    with pytest.raises(ValueError, match="measured lexical baseline"):
        choose_candidate(without_baseline, gold, runs[1:])
    unmeasured = {"cases": {key: {"relevance_sets": [], "required_units": []} for key in gold["cases"]}}
    with pytest.raises(ValueError, match="missing recall evidence"):
        choose_candidate(protocol, unmeasured, runs)


def test_predeclared_equal_quality_candidate_must_beat_measured_lexical_latency():
    protocol, gold = frozen_candidates()
    protocol["predeclared_equal_quality_latency_win"] = True
    runs = [execution(protocol, name) for name in protocol["candidates"]]
    lexical, small = runs[:2]
    for control, challenger in zip(lexical["outcomes"], small["outcomes"], strict=True):
        control["bundles"] = challenger["bundles"]
        challenger["elapsed_seconds"] = 0.4
    selected = choose_candidate(protocol, gold, runs)
    assert selected["candidate"] == "base"
    assert selected["comparison"]["base"]["paired_recall_difference"] == 0
    assert selected["embedding_candidate_comparisons"][0]["small_preferred_at_measured_development_p95"] is False


def test_retrieval_gates_count_actual_failed_and_missing_case_evidence():
    protocol, gold = frozen_candidates()
    original = execution(protocol, "small")
    missing = {**original, "outcomes": original["outcomes"][:-1]}
    duplicate = {**original, "outcomes": [*original["outcomes"], original["outcomes"][0]]}
    for payload in (missing, duplicate):
        with pytest.raises(ValueError, match="exactly one outcome"):
            summarize_retrieval(protocol, gold, payload)
    for field, value in (("run_id", None), ("attempts", []), ("eligibility_revision", 3)):
        bad = deepcopy(original)
        bad["outcomes"][0][field] = value
        with pytest.raises(ValueError, match="recorded attempt"):
            summarize_retrieval(protocol, gold, bad)
    for field, value in (("protocol_sha256", "wrong"), ("config", {}), ("eligibility_revision", 3)):
        with pytest.raises(ValueError, match="bind the frozen"):
            summarize_retrieval(protocol, gold, {**original, field: value})
    failed = deepcopy(original)
    for row in failed["outcomes"][:5]:
        row.update(status="error", bundles=[])
    scored = summarize_retrieval(protocol, gold, failed)
    assert len(scored["failure_cases"]) == 5
    assert scored["metrics"]["recall"]["n"] == 45
    assert scored["metrics"]["recall"]["point"] == pytest.approx(40 / 45)
    assert scored["retrieval_thresholds_met"] is False
    unknown = deepcopy(original)
    missing_attempt = unknown["outcomes"][0]
    missing_attempt.update(status="error", bundles=[], attempts=None)
    scored = summarize_retrieval(protocol, gold, unknown)
    assert scored["metrics"]["recall"]["point"] == pytest.approx(44 / 45)
    assert scored["incomplete_attempt_cases"] == [missing_attempt["case_id"]]
    assert scored["retrieval_thresholds_met"] is None


def adjudication_fixtures(cases, repetition):
    reviews = []
    outputs = {}
    for case_id, case in cases.items():
        subtype = case["subtype"]
        answerable = subtype in ANSWERABLE
        status = "answer" if answerable else "clarify" if subtype == "ambiguous_conflicting" else "abstain"
        output = {"state": "result", "answer": {"status": status}, "model_hash": "fixture-model"}
        outputs[case_id] = output
        count = int(answerable)
        reviews.append({
            "case_id": case_id, "repetition": repetition, "output_sha256": fingerprint(output),
            "reviewer_reference": "Synthetic arithmetic fixture", "result_status": status,
            "material_claims": count, "cited_material_claims": count, "sufficient_claims": count,
            "citation_associations": count, "relevant_citation_associations": count,
            "required_claims": count, "correct_required_claims": count, "expected_status_met": True,
            "consequential_error": False, "prohibited_adversarial_behavior": False,
            "anchors_valid": True, "table_qualifiers_preserved": True,
        })
    return reviews, outputs


def measured(cases, reviews, outputs):
    return summarize_generation(cases, [Adjudication.model_validate(row) for row in reviews], outputs)


def test_generation_metrics_distinguish_failed_inconsistent_and_unmeasured_evidence():
    protocol, _ = frozen_candidates()
    cases = {case["case_id"]: case for case in protocol["cases"] if case["split"] == "held_out"}
    reviews, outputs = adjudication_fixtures(cases, 1)
    good = measured(cases, reviews, outputs)
    assert good["generation_thresholds_met"] is True
    assert good["answer_completeness"]["point"] == 1.0 and good["answer_completeness"]["n"] == 45
    assert good["citation_set_accuracy"] == {"point": 1.0, "numerator": 45, "denominator": 45, "n_a_cases": 45}
    assert good["unanswerable_correct"] == good["ambiguous_correct"] == 15
    assert not good["failed_cases"]
    with pytest.raises(ValueError, match="account for every"):
        measured(cases, reviews[:-1], outputs)
    missing = dict(outputs)
    missing.pop(reviews[0]["case_id"])
    with pytest.raises(ValueError, match="exact recorded output"):
        measured(cases, reviews, missing)
    for field, value in (("sufficient_claims", 2), ("relevant_citation_associations", 2), ("correct_required_claims", 2)):
        bad = [{**reviews[0], field: value}, *reviews[1:]]
        with pytest.raises(ValueError, match="Invalid human scoring"):
            measured(cases, bad, outputs)
    bad = [{**reviews[0], "sufficient_claims": 0}, *reviews[1:]]
    assert measured(cases, bad, outputs)["generation_thresholds_met"] is False
    unmeasured = [{**row, "required_claims": 0, "correct_required_claims": 0} for row in reviews]
    metrics = measured(cases, unmeasured, outputs)
    assert metrics["generation_thresholds_met"] is None
    assert metrics["answer_completeness"] == {"point": None, "n": 0, "query_cluster_interval_95": None}


def test_adjudication_cannot_score_a_failed_output_as_an_answer():
    protocol, _ = frozen_candidates()
    cases = {case["case_id"]: case for case in protocol["cases"] if case["split"] == "held_out"}
    reviews, outputs = adjudication_fixtures(cases, 1)
    case_id = reviews[0]["case_id"]
    failure = {"state": "error", "failure_code": "provider_failed", "model_hash": "fixture-model"}
    outputs[case_id] = failure
    reviews[0]["output_sha256"] = fingerprint(failure)
    with pytest.raises(ValueError, match="recorded result status"):
        measured(cases, reviews, outputs)
    reviews[0].update(
        result_status="error", expected_status_met=False, material_claims=0, cited_material_claims=0,
        sufficient_claims=0, citation_associations=0, relevant_citation_associations=0, correct_required_claims=0,
    )
    with pytest.raises(ValueError, match="Invalid human scoring"):
        measured(cases, [{**reviews[0], "correct_required_claims": 1}, *reviews[1:]], outputs)
    score = measured(cases, reviews, outputs)
    assert score["failed_cases"] == [case_id]
    assert score["answer_completeness"]["n"] == 45
    assert score["answer_completeness"]["point"] == pytest.approx(44 / 45)
    abstain = {"state": "result", "answer": {"status": "abstain"}, "model_hash": "fixture-model"}
    outputs[case_id] = abstain
    reviews[0].update(result_status="abstain", output_sha256=fingerprint(abstain))
    with pytest.raises(ValueError, match="Invalid human scoring"):
        measured(cases, [{**reviews[0], "correct_required_claims": 1}, *reviews[1:]], outputs)
    score = measured(cases, reviews, outputs)
    assert score["false_abstentions"] == 1 and score["answer_completeness"]["point"] == pytest.approx(44 / 45)


def test_answer_completeness_threshold_uses_the_accepted_boundary():
    protocol, _ = frozen_candidates()
    cases = {case["case_id"]: case for case in protocol["cases"] if case["split"] == "held_out"}
    original, outputs = adjudication_fixtures(cases, 1)
    for count, passing in ((900, True), (899, False)):
        reviews = [
            {**row, "required_claims": 1000, "correct_required_claims": count}
            if cases[row["case_id"]]["subtype"] in ANSWERABLE else row for row in original
        ]
        result = measured(cases, reviews, outputs)
        assert result["answer_completeness"]["point"] == count / 1000
        assert result["generation_thresholds_met"] is passing


def test_final_report_requires_bound_repetitions_outputs_and_production_evidence():
    protocol, gold = frozen_candidates()
    selection = choose_candidate(protocol, gold, [execution(protocol, name) for name in protocol["candidates"]])
    runs = [execution(protocol, "small", split="held_out", repetition=n, selection=selection) for n in (1, 2, 3)]
    evidence = {"status": "PASS", "evidence_reference": "Synthetic scoring fixture only"}
    with pytest.raises(ValueError, match="separate evidence reference"):
        final_report(protocol, gold, selection, runs, {**evidence, "evidence_reference": ""}, None)
    for key, value in (("candidate", "base"), ("repetition", 2), ("selection_sha256", "wrong")):
        with pytest.raises(ValueError, match="three distinct held-out"):
            final_report(protocol, gold, selection, [{**runs[0], key: value}, *runs[1:]], evidence, None)
    with pytest.raises(ValueError, match="after selection was frozen"):
        final_report(protocol, gold, selection, [{**runs[0], "finished_at": "2020-01-01"}, *runs[1:]], evidence, None)
    with pytest.raises(ValueError, match="actual outputs from one frozen model"):
        final_report(protocol, gold, selection, runs, evidence, {"reviews": []})
    cases = {case["case_id"]: case for case in protocol["cases"] if case["split"] == "held_out"}
    reviews = []
    for run in runs:
        observed, outputs = adjudication_fixtures(cases, run["repetition"])
        reviews.extend(observed)
        run.update(generated_outputs=outputs, hosted_confirmation_manifest_sha256="synthetic-input-manifest")
    report = final_report(protocol, gold, selection, runs, evidence, {"reviews": reviews})
    assert report["decisions"]["eligible_scope"] == "WORKBENCH_ONLY"
    assert report["decisions"]["generation"] == "UNMEASURED"
    assert report["decisions"]["owner_acceptance"] == "NOT_RECORDED"
    assert [entry["repetition"] for entry in report["generation"]] == [1, 2, 3]
    assert all(entry["generation_thresholds_met"] is None for entry in report["generation"])
    assert all(entry["budget_policy_violation"] is None for entry in report["generation"])
    for scored, retrieved, run in zip(report["generation"], report["retrieval"], runs, strict=True):
        binding = scored["regression_run"]
        assert binding["execution_sha256"] == retrieved["raw_execution_sha256"] == fingerprint(run)
        assert binding["outputs_sha256"] == fingerprint(run["generated_outputs"])
        assert binding["unresolved_cases"] == []
    observed = runs[0]["outcomes"][0]
    received = runs[0]["generated_outputs"][observed["case_id"]]
    received.update(operation_id="recorded-operation", run_id="different-query",
                    confirmed_at=runs[0]["started_at"], finished_at=runs[0]["finished_at"])
    with pytest.raises(ValueError, match="recorded retrieval and repetition"):
        final_report(protocol, gold, selection, runs, evidence, {"reviews": reviews})
    received["run_id"] = observed["run_id"]
    received["finished_at"] = "2099-01-01T00:00:02Z"
    with pytest.raises(ValueError, match="recorded retrieval and repetition"):
        final_report(protocol, gold, selection, runs, evidence, {"reviews": reviews})
    received.update(state="error", failure_code="evaluation_deadline", answer=None, attempts=None)
    review = next(row for row in reviews if row["repetition"] == 1 and row["case_id"] == observed["case_id"])
    review.update(
        output_sha256=fingerprint(received), result_status="error", expected_status_met=False,
        material_claims=0, cited_material_claims=0, sufficient_claims=0, citation_associations=0,
        relevant_citation_associations=0, correct_required_claims=0,
    )
    late = final_report(protocol, gold, selection, runs, evidence, {"reviews": reviews})
    assert observed["case_id"] in late["generation"][0]["failed_cases"]
    received["finished_at"] = runs[0]["finished_at"]
    # Perfect model-quality scores cannot make a measured policy violation pass.
    result = next(iter(runs[0]["generated_outputs"].values()))
    result["failure_code"] = "provider_token_limit"
    for review in reviews:
        if review["repetition"] == 1:
            review["output_sha256"] = fingerprint(runs[0]["generated_outputs"][review["case_id"]])
    withheld = final_report(protocol, gold, selection, runs, evidence, {"reviews": reviews})
    assert withheld["generation"][0]["budget_policy_violation"] is True
    assert withheld["decisions"]["eligible_scope"] == "WORKBENCH_ONLY" and withheld["decisions"]["generation"] == "FAIL"
