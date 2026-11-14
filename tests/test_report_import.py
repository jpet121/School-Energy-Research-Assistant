from copy import deepcopy
import json
import sqlite3

from retrofit.contracts import fingerprint
from test_admission import HEADERS, local_client


def report_fixture():
    metrics = {
        "recall": {"point": 0.91, "n": 45, "query_cluster_interval_95": [0.88, 0.94]},
        "table_recall": {"point": 0.83, "n": 15},
        "returned_precision": {"point": 0.87},
        "ranked_precision": {"point": 0.93},
    }
    return {
        "schema_version": "1.1",
        "evaluation_mode": "SELF_EVALUATED",
        "protocol_sha256": "fixture-protocol",
        "selection_sha256": "fixture-selection",
        "candidate": "lexical",
        "custody_reference": "Synthetic report contract fixture",
        "builder_exposure": "Synthetic inputs",
        "decisions": {
            "production": "PASS", "retrieval": "PASS", "generation": "UNMEASURED",
            "eligible_scope": "WORKBENCH_ONLY", "generation_eligible": False,
            "owner_acceptance": "NOT_RECORDED", "release": "NOT_AUTHORIZED",
        },
        "retrieval": [
            {"repetition": n, "case_count": 90, "failure_cases": [], "case_scores": ["private fixture"],
             "raw_execution_sha256": fingerprint(["retrieval", n]), "incomplete_attempt_cases": [],
             "retrieval_thresholds_met": True, "metrics": deepcopy(metrics)}
            for n in (1, 2, 3)
        ],
        "generation": [],
        "production_evidence_reference": "Synthetic verification record",
        "limitations": ["Fixture for the import boundary only."],
        "recorded_at": "2026-01-01T00:00:00Z",
    }


def submit(client, report, *, identity=None):
    if identity is None:
        identity = fingerprint(report)
    return client.post("/evaluation/reports", headers={**HEADERS, "Content-Type": "application/json"},
                       content=json.dumps({**report, "report_sha256": identity}))


def passing_generation_report():
    report = report_fixture()
    report["decisions"].update(generation="PASS", eligible_scope="WORKBENCH_AND_GENERATION", generation_eligible=True)
    ratio = {"point": 1.0, "numerator": 45, "denominator": 45, "n_a_cases": 45}
    report["generation"] = [
        {"repetition": n, "generation_thresholds_met": True,
         "citation_set_accuracy": dict(ratio),
         "answer_completeness": {"point": 0.95, "n": 45}, "consequential_errors": 0,
         "citation_relevance": dict(ratio), "citation_completeness": dict(ratio),
         "citation_accuracy_by_subtype": {
             key: {**ratio, "numerator": 15, "denominator": 15, "n_a_cases": 0}
             for key in ("direct", "multi_passage", "table")
         },
         "adversarial_violations": 0, "false_abstentions": 4, "unanswerable_correct": 14, "ambiguous_correct": 14,
         "anchors_valid": True, "table_qualifiers_preserved": True, "budget_policy_violation": False,
         "regression_run": {
             "model_hash": "fixture-model", "execution_sha256": report["retrieval"][n - 1]["raw_execution_sha256"],
             "outputs_sha256": fingerprint(["generation", n]), "started_at": "2026-01-01T00:00:00Z",
             "finished_at": "2026-01-01T00:00:01Z", "unresolved_cases": [], "budget_dispatches_checked": 1,
         }}
        for n in (1, 2, 3)
    ]
    return report


def test_report_requires_measured_retrieval_and_generation_denominators(tmp_path):
    with local_client(tmp_path) as client:
        for metric, count in (("recall", 1), ("table_recall", 1), ("recall", True)):
            invalid = report_fixture()
            invalid["retrieval"][0]["metrics"][metric]["n"] = count
            assert submit(client, invalid).json().get("code") == "inconsistent_retrieval_gate"
        for counts in (
            {"numerator": 0, "denominator": 0, "n_a_cases": 90},
            {"numerator": 12, "denominator": 15},
        ):
            invalid = passing_generation_report()
            invalid["generation"][1]["citation_accuracy_by_subtype"]["table"].update(counts)
            assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        invalid = passing_generation_report()
        invalid["generation"][0]["answer_completeness"]["point"] = True
        assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        invalid = passing_generation_report()
        invalid["generation"][0]["budget_policy_violation"] = True
        assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        invalid = passing_generation_report()
        del invalid["generation"][0]["budget_policy_violation"]
        assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        invalid = passing_generation_report()
        invalid["generation"][0]["regression_run"]["budget_dispatches_checked"] = 0
        assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        invalid = passing_generation_report()
        invalid["generation"][1]["citation_set_accuracy"].update(numerator=15, denominator=15)
        assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        invalid = passing_generation_report()
        invalid["generation"][1]["citation_accuracy_by_subtype"]["direct"].update(point=14 / 15, numerator=14)
        assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        invalid = passing_generation_report()
        invalid["generation"][1]["citation_set_accuracy"]["n_a_cases"] = 90
        assert submit(client, invalid).json().get("code") == "inconsistent_generation_gate"
        assert client.get("/evaluation/reports", headers=HEADERS).json() == {"reports": []}
        assert submit(client, passing_generation_report()).raise_for_status().json()["decisions"]["generation"] == "PASS"


def test_workbench_eligibility_requires_distinct_execution_and_complete_attempt_evidence(tmp_path):
    with local_client(tmp_path) as client:
        valid = report_fixture()
        for field, value in (
            ("raw_execution_sha256", None),
            ("raw_execution_sha256", valid["retrieval"][0]["raw_execution_sha256"]),
            ("incomplete_attempt_cases", ["case-1"]),
            ("failure_cases", [f"failed-case-{i}" for i in range(90)]),
        ):
            invalid = deepcopy(valid)
            invalid["retrieval"][1][field] = value
            assert submit(client, invalid).json().get("code") == "inconsistent_retrieval_evidence"
        assert submit(client, valid).raise_for_status().json()["decisions"]["eligible_scope"] == "WORKBENCH_ONLY"


def test_aggregate_import_rejects_non_boolean_gate_evidence(tmp_path):
    with local_client(tmp_path) as client:
        invalid = report_fixture()
        for repetition in invalid["retrieval"]:
            repetition["retrieval_thresholds_met"] = 1
        response = submit(client, invalid)
        assert (response.status_code, response.json().get("code")) == (400, "invalid_report")
        assert client.get("/evaluation/reports", headers=HEADERS).json() == {"reports": []}


def test_report_cannot_assert_pass_against_its_own_metrics(tmp_path):
    with local_client(tmp_path) as client:
        boolean_recall = report_fixture()
        boolean_recall["retrieval"][0]["metrics"]["recall"]["point"] = True
        assert submit(client, boolean_recall).json().get("code") == "inconsistent_retrieval_gate"
        generated = report_fixture()
        generated["decisions"].update(generation="PASS", eligible_scope="WORKBENCH_AND_GENERATION", generation_eligible=True)
        generated["generation"] = [
            {"repetition": n, "generation_thresholds_met": True, "citation_set_accuracy": {"point": 0.71},
             "answer_completeness": {"point": 0.6}, "consequential_errors": 1}
            for n in (1, 2, 3)
        ]
        denied = submit(client, generated)
        assert (denied.status_code, denied.json().get("code")) == (400, "inconsistent_generation_gate")
        generated = passing_generation_report()
        for repetition in generated["generation"]:
            repetition.update(
                adversarial_violations=1, false_abstentions=5, unanswerable_correct=13, ambiguous_correct=13,
            )
        denied = submit(client, generated)
        assert (denied.status_code, denied.json().get("code")) == (400, "inconsistent_generation_gate")
        absent = submit(client, {**report_fixture(), "production_evidence_reference": None})
        assert (absent.status_code, absent.json().get("code")) == (400, "missing_production_evidence")
        assert client.get("/evaluation/reports", headers=HEADERS).json() == {"reports": []}


def test_report_import_preserves_only_verified_aggregate_and_rejects_invalid_records(tmp_path):
    with local_client(tmp_path) as client:
        report = report_fixture()
        imported = submit(client, report).raise_for_status().json()
        assert imported["decisions"]["eligible_scope"] == "WORKBENCH_ONLY"
        assert imported["retrieval"][0]["recall"] == 0.91
        assert all("case_scores" not in item for item in imported["retrieval"])

        invalid = deepcopy(report)
        invalid["retrieval"][0]["metrics"]["recall"]["point"] = float("nan")
        assert submit(client, invalid, identity="invalid").json()["code"] == "invalid_report"
        assert submit(client, report, identity="altered").json()["code"] == "report_identity_mismatch"
        invalid = deepcopy(report)
        del invalid["retrieval"][0]["metrics"]
        assert submit(client, invalid).json()["code"] == "invalid_report"
        invalid = deepcopy(report)
        invalid["retrieval"][0]["repetition"] = 3
        assert submit(client, invalid).json()["code"] == "missing_repetition"
        for metric, value in (("recall", 0.89), ("table_recall", 0.79), ("recall", None)):
            invalid = deepcopy(report)
            invalid["retrieval"][0]["metrics"][metric]["point"] = value
            assert submit(client, invalid).json()["code"] == "inconsistent_retrieval_gate"
        invalid = deepcopy(report)
        invalid["decisions"]["eligible_scope"] = "WITHHELD"
        assert submit(client, invalid).json()["code"] == "inconsistent_scope"
        assert submit(client, report).status_code == 201
    with local_client(tmp_path) as restarted:
        assert restarted.get("/evaluation/reports", headers=HEADERS).json() == {"reports": [imported]}


def test_transitional_report_receipts_stay_private_and_keep_original_execution_dates(tmp_path):
    report = passing_generation_report()
    model = "fixed-model"
    configuration = fingerprint([report["protocol_sha256"], report["selection_sha256"], report["candidate"], model])
    old_runs = []
    for repetition, retrieval in zip(report["generation"], report["retrieval"], strict=True):
        number = repetition["repetition"]
        execution = fingerprint(["execution", number])
        retrieval["raw_execution_sha256"] = execution
        run = {
            "model_hash": model, "execution_sha256": execution, "outputs_sha256": fingerprint(["outputs", number]),
            "started_at": "2026-01-01T00:00:00.000000+00:00", "finished_at": "2026-01-01T00:00:01.000000+00:00",
            "unresolved_cases": [], "budget_dispatches_checked": 1,
        }
        old_runs.append(run)
        repetition["regression_run"] = {**run, "started_at": "2026-01-03T00:00:00Z", "finished_at": "2026-01-03T00:00:01Z"}
    with local_client(tmp_path) as client:
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("INSERT INTO evaluation_reports VALUES (?,?)", ("old-report", json.dumps({
                "report_sha256": "old-report", "regression_configuration": configuration,
                "regression_model": model, "regression_runs": old_runs,
            })))
        visible = client.get("/evaluation/reports", headers=HEADERS).raise_for_status().json()
        assert visible == {"reports": [{"report_sha256": "old-report"}]}
        replay = submit(client, report)
        assert (replay.status_code, replay.json()["code"]) == (409, "regression_identity_changed")
        assert client.get("/evaluation/reports", headers=HEADERS).json() == visible
        for repetition, old in zip(report["generation"], old_runs, strict=True):
            repetition["regression_run"] = old
        assert submit(client, report).status_code == 201


def test_a_legacy_failure_without_case_evidence_remains_unresolved(tmp_path):
    report = passing_generation_report()
    model = "fixture-model"
    configuration = fingerprint([report["protocol_sha256"], report["selection_sha256"], report["candidate"], model])
    for generation, retrieval in zip(report["generation"], report["retrieval"], strict=True):
        number = retrieval["repetition"]
        execution = fingerprint(["legacy-regression", number])
        retrieval["raw_execution_sha256"] = execution
        generation["regression_run"] = {
            "model_hash": model, "execution_sha256": execution, "outputs_sha256": fingerprint(["new-output", number]),
            "started_at": "2026-01-04T00:00:00Z", "finished_at": "2026-01-04T00:00:01Z", "unresolved_cases": [],
            "budget_dispatches_checked": 1,
        }
    with local_client(tmp_path) as client:
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("INSERT INTO generation_holds VALUES (?,?,?,NULL)", ("historic", configuration, "[]"))
        assert submit(client, report).status_code == 201
    with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
        assert db.execute("SELECT resolved_by FROM generation_holds WHERE reference='historic'").fetchone() == (None,)


def test_budget_receipts_can_cover_all_held_out_cases_and_a_targeted_replay(tmp_path):
    report = passing_generation_report()
    for generation, retrieval in zip(report["generation"], report["retrieval"], strict=True):
        number = retrieval["repetition"]
        execution = fingerprint(["full-repetition", number])
        retrieval["raw_execution_sha256"] = execution
        generation["regression_run"] = {
            "model_hash": "fixture-model", "execution_sha256": execution, "outputs_sha256": fingerprint(["outputs", number]),
            "started_at": "2026-01-04T00:00:00Z", "finished_at": "2026-01-04T00:00:01Z", "unresolved_cases": [],
            "budget_dispatches_checked": 181, "budget_policy_sha256": fingerprint("fixture-policy"),
            "budget_case_hashes": [fingerprint(["case", index]) for index in range(91)],
        }
    with local_client(tmp_path) as client:
        accepted = submit(client, report).raise_for_status().json()
        assert accepted["decisions"]["eligible_scope"] == "WORKBENCH_AND_GENERATION"
        assert all("regression_run" not in entry for entry in accepted["generation"])
