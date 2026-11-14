import json
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from threading import Event

import pytest

from retrofit.contracts import ApiError, fingerprint, now
from retrofit.evaluate import (
    assess_budget, choose_candidate, final_report, freeze_protocol, inventory_content_hash,
    recorded_budget_outputs, refresh_billing, run_comparison,
)
from retrofit.generation import PaidAuthority, budget_case_identity, packed_context
from retrofit.openai_provider import HostedPolicy, OpenAIProvider
from test_admission import HEADERS, local_client, make_pdf
from test_briefs import draft
from test_corpus import approve_and_prepare, publish
from test_openai_transport import policy_fields, provider, serve_responses
from test_research import search
from test_report_import import passing_generation_report, report_fixture, submit
from test_evaluator_boundaries import passing_audit, protocol_input
from test_evaluator_scoring import adjudication_fixtures, execution, frozen_candidates
from pdf_fixture import pdf_with_pages, text_stream
from processes import python_module
from test_service_recovery import free_origin, launch, publish_baseline, wait_for


def evidence_run(client):
    manifest, _ = approve_and_prepare(client, make_pdf("Night setback reduced modeled heating demand."))
    publish(client, manifest["snapshot_id"])
    return search(client, "night setback")


def confirmation(client, run):
    path = f"/queries/{run['run_id']}"
    preview = client.get(f"{path}/hosted-input", headers=HEADERS)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    return path, {
        "schema_version": "1.1", "confirmed": True, "confirmation_id": data["confirmation_id"],
        "question": data["question"], "context": data["context"],
    }


def test_exact_input_and_separate_paid_authority_block_dispatch(tmp_path):
    with local_client(tmp_path) as client:
        run = evidence_run(client)
        path, decision = confirmation(client, run)
        assert decision["question"] == "night setback" and decision["context"] == ""
        assert client.post(f"{path}/generate", json={**decision, "question": "edited after confirmation"}, headers=HEADERS).status_code == 409
        assert client.post(f"{path}/generate", json={**decision, "confirmed": False}, headers=HEADERS).status_code == 403
        rejected = client.post(f"{path}/generate", json=decision, headers=HEADERS)
        assert rejected.status_code == 403
        toggle = client.post("/generation", json={"schema_version": "1.1", "enabled": True}, headers=HEADERS)
        assert (toggle.status_code, toggle.json()["code"]) == (403, "paid_execution_not_authorized")
        assert client.post("/generation", json={"schema_version": "1.1", "enabled": False}, headers=HEADERS).json()["enabled"] is False
        assert client.get("/generation", headers=HEADERS).json()["dispatch_count"] == 0
        assert client.get(path, headers=HEADERS).json()["final_bundle_ids"] == run["final_bundle_ids"]
        unavailable = search(client, "night setback", mode="dense")
        preview = client.get(f"/queries/{unavailable['run_id']}/hosted-input", headers=HEADERS)
        assert (preview.status_code, preview.json()["code"]) == (409, "query_not_ready")
        missing_hold = client.post("/generation/missing/recover-budget", headers=HEADERS, json={
            "schema_version": "1.1", "report_sha256": "unverified",
        })
        assert (missing_hold.status_code, missing_hold.json()["code"]) == (404, "missing_budget_hold")


def test_hosted_workbench_rejects_a_deadline_beyond_the_accepted_cap(tmp_path):
    with pytest.raises(ValueError, match="accepted 30-second limit"):
        local_client(tmp_path, generation_deadline=31)


class HostedResponseFixture:
    def __init__(self):
        self.fault = ""
        self.called = Event()
        self.preflight = Event()
        self.released = Event()
        self.calls = 0
        self.answer_transform = json.dumps

    def respond(self, path, request):
        if path == "/v1/responses/input_tokens":
            if self.fault == "preflight" or (self.fault == "transient_retry_preflight" and self.calls == 1):
                self.preflight.set()
                assert self.released.wait(5)
            return 200, {"input_tokens": 240}
        assert path == "/v1/responses"
        self.calls += 1
        self.called.set()
        if self.fault == "transient_near_deadline":
            assert self.released.wait(5)
            return 503, {"error": "Transient failure shortly before the deadline."}
        if self.fault == "transient_always" or (self.fault in {"transient_once", "transient_retry_preflight"} and self.calls == 1):
            return 503, {"error": "Synthetic transient failure"}
        if self.fault == "late":
            assert self.released.wait(5)
        payload = json.loads(request["input"])
        bundle = payload["evidence"][0]
        span = bundle["spans"][0]
        quote = "Night setback"
        if self.fault == "false_quote":
            quote = "Wrong finding"
        output = {
            "status": "answer", "conflicts": [], "missing_information": [],
            "claims": [{
                "text": "The source reports a modeled heating-demand result.",
                "qualifiers": ["Modeled result"],
                "citations": [{
                    "bundle_id": bundle["bundle_id"], "supporting_span_ids": [span["id"]],
                    "quote": {"source_span_id": span["id"], "start": 0, "end": 13, "text": quote},
                }],
            }],
        }
        if self.fault == "tools":
            output["tool_calls"] = [{"name": "write_application"}]
        usage = {
            "over_budget": {"input_tokens": 20_000, "output_tokens": 3_000},
            "extreme_usage": {"input_tokens": 10**4300 - 1, "output_tokens": 3_000},
            "no_usage": {},
        }.get(self.fault, {"input_tokens": 240, "output_tokens": 90})
        return 200, {
            "model": "fixture-version", "status": "completed", "usage": usage,
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": self.answer_transform(output)}]}],
        }


@pytest.fixture
def hosted():
    fixture = HostedResponseFixture()
    with serve_responses(fixture.respond) as (endpoint, _):
        fixture.adapter = provider(endpoint)
        yield fixture


def enable_fixture(client):
    result = client.post("/generation", json={"schema_version": "1.1", "enabled": True}, headers=HEADERS)
    assert result.status_code == 200, result.text


def generate(client, run):
    path, decision = confirmation(client, run)
    result = client.post(f"{path}/generate", json=decision, headers=HEADERS)
    assert result.status_code == 202, result.text
    return result.json()["operation_id"]


def wait_generation(client, operation):
    for _ in range(160):
        record = client.get(f"/generation/{operation}", headers=HEADERS).json()
        if record["state"] != "pending":
            return record
        time.sleep(0.04)
    raise AssertionError("Generation did not reach a terminal state")


def test_output_boundary_checks_quotes_citations_and_forbidden_actions(tmp_path, hosted):
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        good = wait_generation(client, generate(client, run))
        assert good["state"] == "result", good
        assert good["answer"]["claims"][0]["citations"][0]["quote"]["text"] == "Night setback"
        assert good["attempts"][0]["estimated_nanos"] == 4_200
        assert good["attempts"][0]["actual_nanos"] is None
        saved = draft(client, text="", generated_operation_id=good["operation_id"]).raise_for_status().json()
        claims = saved["claims"]
        assert claims[0]["review_status"] == "GENERATED_DRAFT_UNVERIFIED"
        assert claims[0]["citations"][0]["quote"]["text"] == "Night setback"
        exported = client.get(f"/briefs/{saved['brief_id']}/export", headers=HEADERS, params={"format": "json"})
        assert exported.raise_for_status().json()["claims"] == claims
        reconciliation = client.post(f"/generation/{good['operation_id']}/attempts/1/reconcile", json={
            "schema_version": "1.1", "actual_nanos": 4_000, "billing_reference": "fixture-operator-record",
        }, headers=HEADERS)
        assert reconciliation.json()["actual_nanos"] == 4_000
        for fault in ("false_quote", "tools"):
            hosted.fault = fault
            bad = wait_generation(client, generate(client, run))
            assert bad["state"] == "error" and bad["answer"] is None
            denied = draft(client, text="", generated_operation_id=bad["operation_id"])
            assert (denied.status_code, denied.json()["code"]) == (409, "answer_not_available")


def test_canceled_dispatch_keeps_unknown_reservation_and_ignores_late_output(tmp_path, hosted):
    hosted.fault = "late"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        assert hosted.called.wait(2)
        assert client.post(f"/generation/{operation}/cancel", json={}, headers=HEADERS).json()["state"] == "canceled"
        assert client.get("/generation", headers=HEADERS).json()["actual_and_reserved_nanos"] == 124_000
        hosted.released.set()
        for _ in range(120):
            record = client.get(f"/generation/{operation}", headers=HEADERS).json()
            if record["attempts"][0].get("usage_input"):
                break
            time.sleep(0.02)
        assert record["state"] == "canceled" and record["answer"] is None
        assert record["attempts"][0]["usage_input"] == 240


def test_late_invoice_overrun_is_recorded_and_holds_a_canceled_dispatch_until_it_finishes(tmp_path, hosted):
    hosted.fault = "late"
    authority = PaidAuthority("local-test-only", 1_000_000)
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        enable_fixture(client)
        operation = generate(client, evidence_run(client))
        try:
            assert hosted.called.wait(3)
            client.post(f"/generation/{operation}/cancel", headers=HEADERS).raise_for_status()
            charged = 25_000_000_001
            invoice = client.post(f"/generation/{operation}/attempts/1/reconcile", headers=HEADERS, json={
                "schema_version": "1.1", "actual_nanos": charged, "billing_reference": "Fixture invoice above the M1 spending cap.",
            }).raise_for_status().json()
            assert invoice["actual_nanos"] == charged
            blocked = client.post(f"/generation/{operation}/recover-budget", headers=HEADERS, json={
                "schema_version": "1.1", "report_sha256": "unverified",
            })
            assert (blocked.status_code, blocked.json()["code"]) == (409, "billing_reconciliation_required")
        finally:
            hosted.released.set()
        record = wait_for(lambda: (
            item if (item := client.get(f"/generation/{operation}", headers=HEADERS).json())["attempts"][0]["state"] == "received" else None
        ))
        assert record["state"] == "canceled" and record["answer"] is None
        assert record["attempts"][0]["actual_nanos"] == charged
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        status = client.get("/generation", headers=HEADERS).json()
        assert status["budget_holds"] == [operation] and status["actual_and_reserved_nanos"] == charged
        assert not status["enabled"] and hosted.calls == 1


@pytest.mark.parametrize(("action", "expected"), [
    ("cancel", ("canceled", None)),
    ("disable", ("error", "generation_disabled")),
    ("withdraw", ("error", "source_withdrawn")),
])
def test_queued_generation_rechecks_controls_before_reserving(tmp_path, hosted, action, expected):
    hosted.fault = "late"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        first = generate(client, run)
        try:
            assert hosted.called.wait(3)
            assert client.post(f"/generation/{first}/cancel", headers=HEADERS).json()["state"] == "canceled"
            queued = generate(client, run)
            if action == "cancel":
                assert client.post(f"/generation/{queued}/cancel", headers=HEADERS).json()["state"] == "canceled"
            elif action == "disable":
                assert client.post("/generation", headers=HEADERS, json={"schema_version": "1.1", "enabled": False}).json()["enabled"] is False
            else:
                version = run["bundles"][0]["version_id"]
                client.post(f"/editions/{version}/withdraw", headers=HEADERS, json={
                    "schema_version": "1.1", "reason": "Source withdrawn while work was queued.",
                }).raise_for_status()
        finally:
            hosted.released.set()
        outcome = wait_generation(client, queued)
        assert (outcome["state"], outcome.get("failure_code")) == expected
        assert outcome["attempts"] == [] and outcome["answer"] is None and hosted.calls == 1


def test_cancellation_during_preflight_prevents_inference_dispatch(tmp_path, hosted):
    hosted.fault = "preflight"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        operation = generate(client, evidence_run(client))
        try:
            assert hosted.preflight.wait(2)
            assert client.post(f"/generation/{operation}/cancel", json={}, headers=HEADERS).json()["state"] == "canceled"
        finally:
            hosted.released.set()
        for _ in range(120):
            record = client.get(f"/generation/{operation}", headers=HEADERS).json()
            if record["attempts"][0]["state"] == "dispatch_denied":
                break
            time.sleep(0.02)
        assert record["attempts"][0]["state"] == "dispatch_denied"
        assert record["answer"] is None and hosted.calls == 0
        status = client.get("/generation", headers=HEADERS).json()
        assert status["dispatch_count"] == 0 and status["actual_and_reserved_nanos"] == 0


def test_cancel_after_a_transient_error_prevents_retry_dispatch_and_preserves_draft(tmp_path, hosted):
    hosted.fault = "transient_retry_preflight"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        original = draft(client, text="Saved independent draft.").raise_for_status().json()
        operation = generate(client, evidence_run(client))
        path = f"/generation/{operation}"
        try:
            assert hosted.preflight.wait(3), "The retry did not reach its pre-dispatch boundary."
            attempts = client.get(path, headers=HEADERS).raise_for_status().json()["attempts"]
            assert attempts[0]["state"] == "transient_error" and attempts[0]["dispatch_at"]
            assert client.post(f"{path}/cancel", json={}, headers=HEADERS).json()["state"] == "canceled"
        finally:
            hosted.released.set()
        for _ in range(120):
            result = client.get(path, headers=HEADERS).raise_for_status().json()
            if result["attempts"][1]["state"] == "dispatch_denied":
                break
            time.sleep(0.02)
        assert result["state"] == "canceled" and result["answer"] is None
        assert [attempt["state"] for attempt in result["attempts"]] == ["transient_error", "dispatch_denied"]
        assert "dispatch_at" not in result["attempts"][1] and hosted.calls == 1
        assert client.get("/generation", headers=HEADERS).json()["dispatch_count"] == 1
        assert client.get(f"/briefs/{original['brief_id']}", headers=HEADERS).json() == original


def test_unresolved_first_attempt_consumes_budget_before_retry(tmp_path, hosted):
    hosted.fault = "transient_once"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 124_000)) as client:
        enable_fixture(client)
        result = wait_generation(client, generate(client, evidence_run(client)))
        assert (result["state"], result["failure_code"], result["answer"]) == ("error", "budget_limit", None)
        assert [item["state"] for item in result["attempts"]] == ["transient_error"]
        status = client.get("/generation", headers=HEADERS).raise_for_status().json()
        assert (status["dispatch_count"], status["actual_and_reserved_nanos"], hosted.calls) == (1, 124_000, 1)


def test_local_controls_can_find_and_reconcile_ordinary_billing_after_restart(tmp_path, hosted):
    hosted.fault = "no_usage"
    authority = PaidAuthority("local-test-only", 1_000_000)
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        enable_fixture(client)
        operation = generate(client, evidence_run(client))
        assert wait_generation(client, operation)["failure_code"] == "usage_unresolved"
    with local_client(tmp_path) as client:
        status = client.get("/generation", headers=HEADERS).raise_for_status().json()
        assert not status["enabled"] and status["budget_holds"] == []
        assert status["unreconciled_attempts"] == [{
            "operation_id": operation, "attempt": 1, "reserved_nanos": 124_000,
        }]
        client.post(f"/generation/{operation}/attempts/1/reconcile", headers=HEADERS, json={
            "schema_version": "1.1", "actual_nanos": 2_500, "billing_reference": "fixture-invoice",
        }).raise_for_status()
        status = client.get("/generation", headers=HEADERS).raise_for_status().json()
        assert status["unreconciled_attempts"] == [] and status["actual_and_reserved_nanos"] == 2_500
        assert hosted.calls == 1


@pytest.mark.parametrize("fault", ["over_budget", "malformed_output", "extreme_usage"])
def test_usage_limit_violation_persists_even_with_malformed_provider_data(tmp_path, hosted, fault):
    hosted.fault = "over_budget" if fault == "malformed_output" else fault
    if fault == "malformed_output":
        hosted.answer_transform = lambda _: "\ud800"
    authority = PaidAuthority("local-test-only", 1_000_000)
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        enable_fixture(client)
        run = evidence_run(client)
        record = wait_generation(client, generate(client, run))
        assert (record["state"], record["failure_code"], record["answer"]) == ("error", "provider_token_limit", None)
        assert record["attempts"][0]["usage_input"] == (10**4300 - 1 if fault == "extreme_usage" else 20_000)
        assert record["attempts"][0]["usage_output"] == 3_000
        status = client.get("/generation", headers=HEADERS).raise_for_status().json()
        assert (status["enabled"], status["dispatch_count"]) == (False, 1)
        assert status["budget_holds"] == [record["operation_id"]]
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        result = client.post("/generation", headers=HEADERS, json={"schema_version": "1.1", "enabled": True})
        assert (result.status_code, result.json()["code"]) == (409, "generation_regressions_pending")
        assert hosted.calls == 1


def test_provider_budget_hold_requires_billing_and_matching_regressions_before_manual_enable(tmp_path, hosted):
    authority = PaidAuthority("local-test-only", 300_000)
    switch = {"schema_version": "1.1", "enabled": True}
    hosted.fault = "over_budget"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        incident = wait_generation(client, operation)
        assert incident["failure_code"] == "provider_token_limit"
        assert incident["attempts"][0]["reserved_nanos"] == 124_000
        assert incident["attempts"][0]["actual_nanos"] is None
        changed = deepcopy(run["bundles"])
        changed[0]["spans"][0]["normalized_text"] = "Different text under the same legacy chunk ID."
        assert budget_case_identity(run, changed) != incident["budget_case_sha256"]

    recovery = f"/generation/{operation}/recover-budget"
    with local_client(tmp_path) as client:
        denied = client.post(recovery, json={"schema_version": "1.1", "report_sha256": "unverified"}, headers=HEADERS)
        assert (denied.status_code, denied.json()["code"]) == (403, "paid_execution_not_authorized")
    current_policy = policy_fields()
    changed_price = OpenAIProvider(HostedPolicy.model_validate({
        **current_policy, "input_nanos_per_token": current_policy["input_nanos_per_token"] + 1,
    }), "synthetic-local-key", endpoint=hosted.adapter.endpoint)
    with local_client(tmp_path, provider=changed_price, paid_authority=authority) as client:
        denied = client.post(recovery, json={"schema_version": "1.1", "report_sha256": "unverified"}, headers=HEADERS)
        assert (denied.status_code, denied.json()["code"]) == (409, "budget_configuration_changed")
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        assert client.get("/generation", headers=HEADERS).json()["budget_holds"] == [operation]
        denied = client.post(recovery, json={"schema_version": "1.1", "report_sha256": "unverified"}, headers=HEADERS)
        assert (denied.status_code, denied.json().get("code")) == (409, "billing_reconciliation_required")
        billing = {"schema_version": "1.1", "actual_nanos": 175_000, "billing_reference": "fixture invoice"}
        cost = client.post(f"/generation/{operation}/attempts/1/reconcile", json=billing, headers=HEADERS)
        assert cost.raise_for_status().json()["actual_nanos"] == 175_000
        assert client.get("/generation", headers=HEADERS).json()["actual_and_reserved_nanos"] == 175_000
        assert client.post("/generation", json=switch, headers=HEADERS).json()["code"] == "generation_regressions_pending"

    protocol, gold = frozen_candidates()
    selection = choose_candidate(protocol, gold, [execution(protocol, name) for name in protocol["candidates"]])
    runs, reviews, targeted = [], [], []
    cases = {case["case_id"]: case for case in protocol["cases"] if case["split"] == "held_out"}
    assert assess_budget({"incident": incident}) == (True, 0, None, [])
    hosted.fault = ""
    with local_client(tmp_path / "regression", provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as checker:
        enable_fixture(checker)
        indexed = evidence_run(checker)
        candidate = search(checker, indexed["question"], snapshot_id=indexed["snapshot_id"])
        for repetition in (1, 2, 3):
            start = datetime.now(timezone.utc).isoformat()
            output = wait_generation(checker, generate(checker, candidate))
            assert output["state"] == "result" and output["model_hash"] == incident["model_hash"]
            assert assess_budget({"fixture": output}) == (None, 0, None, [])
            checker.post(f"/generation/{output['operation_id']}/attempts/1/reconcile", headers=HEADERS, json={
                "schema_version": "1.1", "actual_nanos": 4_200, "billing_reference": "Corrective fixture invoice.",
            }).raise_for_status()
            output = checker.get(f"/generation/{output['operation_id']}", headers=HEADERS).raise_for_status().json()
            violation, checked, policy, checked_cases = assess_budget({"fixture": output})
            assert (violation, checked, policy, checked_cases) == (
                False, 1, incident["budget_policy_sha256"], [incident["budget_case_sha256"]],
            )
            original_attempt = output["attempts"][0]
            for update in (
                {"reserved_nanos": 123_999}, {"actual_nanos": 124_001},
                {"dispatch_at": output["deadline_at"]}, {"dispatch_at": None},
                {"dispatch_at": "2000-01-01T00:00:00+00:00"},
                {"committed_nanos_at_dispatch": 1_000_001}, {"dispatch_count_before": 1_600},
            ):
                assert assess_budget({"fixture": {**output, "attempts": [{**original_attempt, **update}]}}) == (True, 0, None, [])
            assert assess_budget({"fixture": {**output, "authority_reference": None}}) == (True, 0, None, [])
            assert assess_budget({"fixture": {**output, "operation_id": None}}) == (True, 0, None, [])
            assert assess_budget({"fixture": {**output, "deadline_at": None}}) == (None, 0, None, [])
            measured_run = execution(protocol, selection["candidate"], split="held_out", repetition=repetition, selection=selection)
            adjudications, generated = adjudication_fixtures(cases, repetition)
            for adjudication in adjudications:
                observation = generated[adjudication["case_id"]]
                observation["model_hash"] = output["model_hash"]
                adjudication["output_sha256"] = fingerprint(observation)
            measured_run.update(
                started_at=now(), finished_at=now(), generated_outputs=generated,
                hosted_confirmation_manifest_sha256=fingerprint(["controlled fixture", repetition]),
            )
            runs.append(measured_run)
            reviews.extend(adjudications)
            targeted.append({output["operation_id"]: output})
    production = {"status": "PASS", "evidence_reference": "Synthetic local gate fixture only"}
    with pytest.raises(ValueError, match="distinct budget regression operations"):
        final_report(protocol, gold, selection, runs, production, {"reviews": reviews}, [targeted[0]] * 3)
    invalid = [{**targeted[0], "different-operation": next(iter(targeted[0].values()))}, *targeted[1:]]
    with pytest.raises(ValueError, match="bind its recorded operation"):
        final_report(protocol, gold, selection, runs, production, {"reviews": reviews}, invalid)
    report = final_report(protocol, gold, selection, runs, production, {"reviews": reviews}, targeted)
    report.pop("report_sha256")
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 298_999)) as client:
        receipt = submit(client, report).raise_for_status().json()["report_sha256"]
        request = {"schema_version": "1.1", "report_sha256": receipt}
        blocked = client.post(recovery, json=request, headers=HEADERS)
        assert (blocked.status_code, blocked.json()["code"]) == (409, "budget_limit")
    reverified = OpenAIProvider(HostedPolicy.model_validate({
        **current_policy, "verified_on": (date.today() + timedelta(days=1)).isoformat(),
    }), "synthetic-local-key", endpoint=hosted.adapter.endpoint)
    with local_client(tmp_path, provider=reverified, paid_authority=authority) as client:
        unrelated = deepcopy(report)
        for repetition, retrieval in zip(unrelated["generation"], unrelated["retrieval"], strict=True):
            run_receipt = repetition["regression_run"]
            run_receipt.update(
                execution_sha256=fingerprint([run_receipt["execution_sha256"], "unrelated"]),
                outputs_sha256=fingerprint([run_receipt["outputs_sha256"], "unrelated"]),
                budget_case_hashes=[fingerprint("unrelated scenario")],
            )
            retrieval["raw_execution_sha256"] = run_receipt["execution_sha256"]
        unrelated_id = submit(client, unrelated).raise_for_status().json()["report_sha256"]
        rejected = client.post(recovery, json={**request, "report_sha256": unrelated_id}, headers=HEADERS)
        assert (rejected.status_code, rejected.json()["code"]) == (409, "budget_regression_unverified")
        wrong = client.post(recovery, json={**request, "report_sha256": "unverified"}, headers=HEADERS)
        assert (wrong.status_code, wrong.json()["code"]) == (409, "budget_regression_unverified")
        cleared = client.post(recovery, json=request, headers=HEADERS).raise_for_status().json()
        assert cleared["resolved_by"] == receipt and cleared["enabled"] is False
        assert client.get("/generation", headers=HEADERS).json()["budget_holds"] == []
        assert hosted.calls == 4

    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        assert client.get("/generation", headers=HEADERS).json()["enabled"] is False
        assert client.post(recovery, json=request, headers=HEADERS).raise_for_status().json() == cleared
        hosted.fault = ""
        enable_fixture(client)
        result = wait_generation(client, generate(client, run))
        assert result["state"] == "result"
        state = client.get("/generation", headers=HEADERS).json()
        assert state["actual_and_reserved_nanos"] == 299_000 and hosted.calls == 5
        denied = wait_generation(client, generate(client, run))
        assert denied["failure_code"] == "budget_limit" and denied["attempts"] == [] and hosted.calls == 5


def test_preflight_blocks_competing_work_and_cannot_be_billed_before_dispatch(tmp_path, hosted):
    hosted.fault = "preflight"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        try:
            assert hosted.preflight.wait(3)
            path, decision = confirmation(client, run)
            assert client.post(f"{path}/generate", json=decision, headers=HEADERS).json()["code"] == "generation_busy"
            compete = client.post("/queries", json={"schema_version": "1.1", "question": "setback", "mode": "lexical"}, headers=HEADERS)
            assert (compete.status_code, compete.json()["code"]) == (429, "query_busy")
            billing = {"schema_version": "1.1", "actual_nanos": 8, "billing_reference": "Fixture billing"}
            attempt = f"/generation/{operation}/attempts/1/reconcile"
            assert client.post(attempt, json=billing, headers=HEADERS).json()["code"] == "not_dispatched"
            assert client.post(attempt.replace("/1/", "/2/"), json=billing, headers=HEADERS).json()["code"] == "missing_attempt"
            assert client.post("/generation", json={"schema_version": "1.1", "enabled": False}, headers=HEADERS).json()["enabled"] is False
        finally:
            hosted.released.set()
        final = wait_generation(client, operation)
        assert (final["state"], final["failure_code"], hosted.calls) == ("error", "generation_unavailable", 0)
        assert final["attempts"][0]["actual_nanos"] == 0
        assert client.post(attempt, json=billing, headers=HEADERS).json()["code"] == "already_reconciled"
        for method, url in ((client.get, "/generation/missing"), (client.post, "/generation/missing/cancel")):
            assert method(url, headers=HEADERS).json()["code"] == "missing_operation"
        assert client.post(f"/generation/{operation}/cancel", headers=HEADERS).json()["state"] == "already_completed"


@pytest.mark.parametrize(("damage", "code", "cost"), [
    ("already dispatched", "dispatch_already_recorded", "unresolved_reservation_held"),
    ("insufficient", "budget_limit", "not_dispatched"),
])
def test_a_damaged_dispatch_reservation_does_not_call_inference(tmp_path, hosted, damage, code, cost):
    hosted.fault = "preflight"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        try:
            assert hosted.preflight.wait(3)
            with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
                if damage == "already dispatched":
                    db.execute(
                        "UPDATE provider_attempts SET payload=json_set(payload,'$.state','dispatched','$.dispatch_at','recorded') "
                        "WHERE operation_id=? AND attempt=1", (operation,),
                    )
                else:
                    db.execute("UPDATE provider_attempts SET reservation=1 WHERE operation_id=? AND attempt=1", (operation,))
        finally:
            hosted.released.set()
        outcome = wait_generation(client, operation)
        assert outcome["failure_code"] == code and outcome["answer"] is None
        assert outcome["attempts"][0]["cost_status"] == cost and hosted.calls == 0


@pytest.mark.parametrize("delay", ["transport", "ledger"])
def test_generation_deadline_keeps_diagnostics_without_replacing_failed_result(tmp_path, hosted, delay):
    hosted.fault = "late"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000),
                      generation_deadline=0.8) as client:
        enable_fixture(client)
        operation = generate(client, evidence_run(client))
        try:
            assert hosted.called.wait(3)
            if delay == "ledger":
                with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
                    db.execute("BEGIN IMMEDIATE")
                    hosted.released.set()
                    time.sleep(0.9)
            result = wait_generation(client, operation)
        finally:
            hosted.released.set()
        assert (result["state"], result["failure_code"], result["answer"]) == ("error", "generation_deadline", None)
        assert result["attempts"][0]["cost_status"] == "unresolved_reservation_held"
        assert result["attempts"][0]["usage_input"] == (240 if delay == "ledger" else None)
        assert hosted.calls == 1


def test_transient_attempt_at_deadline_never_reserves_a_retry(tmp_path, hosted):
    hosted.fault = "transient_near_deadline"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000),
                      generation_deadline=0.9) as client:
        enable_fixture(client)
        operation = generate(client, evidence_run(client))
        try:
            assert hosted.called.wait(3)
            pending = client.get(f"/generation/{operation}", headers=HEADERS).raise_for_status().json()
            remaining = (datetime.fromisoformat(pending["deadline_at"]) - datetime.now(timezone.utc)).total_seconds()
            assert remaining > 0.12
            time.sleep(remaining - 0.11)
        finally:
            hosted.released.set()
        outcome = wait_generation(client, operation)
        assert (outcome["state"], outcome["failure_code"], outcome["answer"]) == ("error", "generation_deadline", None)
        assert [item["state"] for item in outcome["attempts"]] == ["transient_error"]
        assert hosted.calls == 1 and client.get("/generation", headers=HEADERS).json()["actual_and_reserved_nanos"] == 124_000


def test_local_ledger_lock_failure_prevents_dispatch_and_frees_undispatched_reservation(tmp_path, hosted):
    hosted.fault = "preflight"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        operation = generate(client, evidence_run(client))
        try:
            assert hosted.preflight.wait(3)
            with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
                db.execute("BEGIN IMMEDIATE")
                hosted.released.set()
                time.sleep(5.3)
        finally:
            hosted.released.set()
        outcome = wait_generation(client, operation)
        assert (outcome["state"], outcome["failure_code"], outcome["answer"]) == ("error", "provider_failed", None)
        assert outcome["attempts"][0]["cost_status"] == "not_dispatched"
        assert client.get("/generation", headers=HEADERS).json()["actual_and_reserved_nanos"] == 0 and hosted.calls == 0


@pytest.mark.parametrize("dispatched", [False, True])
def test_abrupt_generation_restart_retains_only_the_dispatched_reservation(tmp_path, hosted, dispatched):
    origin = free_origin()
    root = tmp_path / "store"
    hosted.fault = "late" if dispatched else "preflight"
    try:
        with launch(root, origin, hosted_endpoint=hosted.adapter.endpoint) as (process, client):
            snapshot = publish_baseline(client)
            client.post("/generation", json={"schema_version": "1.1", "enabled": True}).raise_for_status()
            run = client.post("/queries", json={
                "schema_version": "1.1", "question": "established baseline", "mode": "lexical",
            }).raise_for_status().json()["run_id"]
            path = f"/queries/{run}"
            wait_for(lambda: client.get(path).json()["status"] == "result")
            preview = client.get(f"{path}/hosted-input").raise_for_status().json()
            operation = client.post(f"{path}/generate", json={
                "schema_version": "1.1", "confirmed": True,
                **{key: preview[key] for key in ("confirmation_id", "question", "context")},
            }).raise_for_status().json()["operation_id"]
            assert (hosted.called if dispatched else hosted.preflight).wait(3)
            with pytest.raises(ValueError, match="operations to finish"):
                recorded_budget_outputs(origin, [operation])
            process.kill()
            process.wait(timeout=10)
    finally:
        hosted.released.set()

    with launch(root, origin) as (_, client):
        recovered = client.get(f"/generation/{operation}").raise_for_status().json()
        attempt = recovered["attempts"][0]
        assert (recovered["state"], recovered["failure_code"], recovered["answer"]) == ("error", "interrupted", None)
        assert attempt["state"] == "interrupted"
        assert attempt["actual_nanos"] == (None if dispatched else 0)
        assert attempt["cost_status"] == ("unresolved_reservation_held" if dispatched else "not_dispatched")
        assert assess_budget({"recovered": recovered}) == (None, 0, None, [])
        assert client.get("/generation").json()["actual_and_reserved_nanos"] == (124_000 if dispatched else 0)
        assert client.get("/snapshots").json()["active_snapshot_id"] == snapshot


def test_hosted_evaluator_rejects_real_eligibility_change_during_a_comparison(tmp_path, hosted):
    origin = free_origin()
    hosted.fault = "late"
    with launch(tmp_path / "store", origin, hosted_endpoint=hosted.adapter.endpoint) as (_, client):
        snapshot = publish_baseline(client)
        inventory = client.get(f"/snapshots/{snapshot}/evaluation-inventory").raise_for_status().json()
        source = protocol_input()
        source["candidates"]["lexical"]["snapshot_id"] = snapshot
        cases = [case for case in source["cases"] if case["split"] == "development"]
        cases[0]["question"] = "Established baseline"
        protocol = freeze_protocol(source, {
            **passing_audit(), "inventory_content_sha256": inventory_content_hash(inventory),
            "eligibility_revision": inventory["eligibility_revision"],
        })
        manifest = {
            "protocol_sha256": protocol["protocol_sha256"], "split": "development",
            "operator_confirmation_reference": "Loopback fixture only.",
            "pairs": [{key: case[key] for key in ("case_id", "question", "context")} for case in cases],
        }
        client.post("/generation", json={"schema_version": "1.1", "enabled": True}).raise_for_status()
        with ThreadPoolExecutor(max_workers=1) as worker:
            comparison = worker.submit(run_comparison, protocol, origin, "lexical", "development", None, 1, manifest)
            try:
                assert hosted.called.wait(4)
                version = inventory["documents"][0]["version_id"]
                client.post(f"/editions/{version}/withdraw", json={
                    "schema_version": "1.1", "reason": "Withdraw during the controlled comparison.",
                }).raise_for_status()
            finally:
                hosted.released.set()
            with pytest.raises(ValueError, match="eligible corpus changed"):
                comparison.result(timeout=35)
        assert hosted.calls == 1


def test_evaluator_cli_captures_settled_billing_without_rewriting_original_execution(tmp_path, hosted):
    data = tmp_path / "store"
    with local_client(data, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        operation = generate(client, evidence_run(client))
        observed = wait_generation(client, operation)
        assert observed["state"] == "result" and observed["attempts"][0]["actual_nanos"] is None
        client.post(f"/generation/{operation}/attempts/1/reconcile", headers=HEADERS, json={
            "schema_version": "1.1", "actual_nanos": 4_200, "billing_reference": "Controlled fixture bill.",
        }).raise_for_status()
    raw = {"generated_outputs": {
        "observed": observed, "not_dispatched": {"state": "error", "operation_id": None},
        "timed_out": {"state": "error", "operation_id": operation, "failure_code": "evaluation_deadline"},
        "observed_at_timeout": {**observed, "state": "error", "failure_code": "evaluation_deadline", "answer": None},
    }, "outcomes": [{"case_id": key, "run_id": observed["run_id"]} for key in ("timed_out", "observed_at_timeout")]}
    original = tmp_path / "unbilled.json"
    original.write_text(json.dumps(raw), encoding="utf-8")
    origin = free_origin()
    with launch(data, origin):
        results = {}
        for kind, value in (("execution", original), ("operations", operation)):
            destination = tmp_path / f"{kind}.json"
            command = python_module("retrofit.evaluate", "billing", "--origin", origin, f"--{kind}", value, "--out", destination)
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stderr
            results[kind] = json.loads(destination.read_bytes())
        tampered = {"generated_outputs": {"observed": {**observed, "snapshot_id": "different"}}}
        with pytest.raises(ValueError, match="changed outside billing"):
            refresh_billing(origin, tampered)
        wrong_run = {**raw, "outcomes": []}
        with pytest.raises(ValueError, match="recorded retrieval"):
            refresh_billing(origin, wrong_run)
    capture = results["execution"]
    assert capture["billing_source_sha256"] == fingerprint(raw)
    assert capture["generated_outputs"]["not_dispatched"] == raw["generated_outputs"]["not_dispatched"]
    updated = capture["generated_outputs"]["observed"]
    recovered = capture["generated_outputs"]["timed_out"]
    assert (recovered["state"], recovered["failure_code"], recovered["answer"]) == ("error", "evaluation_deadline", None)
    assert recovered["attempts"] == updated["attempts"]
    assert capture["generated_outputs"]["observed_at_timeout"] == recovered
    assert results["operations"]["generated_outputs"] == {operation: updated}
    assert updated["attempts"][0]["actual_nanos"] == 4_200
    assert json.loads(original.read_bytes()) == raw


@pytest.mark.parametrize("timing", ["preflight", "late", "completed"])
def test_withdrawal_hides_result_and_blocks_new_draft_use(tmp_path, hosted, timing):
    if timing != "completed":
        hosted.fault = timing
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        try:
            if timing == "completed":
                assert wait_generation(client, operation)["state"] == "result"
            else:
                assert (hosted.called if timing == "late" else hosted.preflight).wait(3)
            version = run["bundles"][0]["version_id"]
            client.post(f"/editions/{version}/withdraw", headers=HEADERS, json={
                "schema_version": "1.1", "reason": "Fixture withdrawn during generation.",
            }).raise_for_status()
        finally:
            hosted.released.set()
        record = wait_generation(client, operation)
        assert (record["state"], record["failure_code"], record["answer"]) == ("error", "source_withdrawn", None)
        assert hosted.calls == (0 if timing == "preflight" else 1)
        rejected = draft(client, text="", generated_operation_id=operation)
        assert (rejected.status_code, rejected.json()["code"]) == (409, "answer_not_available")


def test_local_abstention_accounts_for_group_limit_without_inference(tmp_path, hosted):
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        evidence_run(client)
        run = search(client, "no-such-evidence-fixture")
        assert run["result_kind"] == "no_evidence"
        client.headers.update(HEADERS)
        abstained = wait_generation(client, generate(client, run))
        assert abstained["answer"]["status"] == "abstain" and not abstained["attempts"]
        path, decision = confirmation(client, run)
        for _ in range(149):
            operation = client.post(f"{path}/generate", json=decision).raise_for_status().json()["operation_id"]
            record = wait_generation(client, operation)
            assert record["state"] == "result" and record["answer"]["status"] == "abstain"
        over = client.post(f"{path}/generate", json=decision)
        assert (over.status_code, over.json()["code"]) == (409, "request_count_limit")
        assert hosted.calls == 0 and client.get("/generation").json()["dispatch_count"] == 0


def test_provider_answer_contract_rejects_wrong_status_unknown_sources_and_offsets(tmp_path, hosted):
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 3_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)

        def changed_citation(answer, **changes):
            answer["claims"][0]["citations"][0].update(changes)
            return json.dumps(answer)

        for transform, code in (
            (lambda answer: json.dumps({**answer, "claims": []}), "invalid_provider_output"),
            (lambda answer: json.dumps({**answer, "status": "abstain"}), "invalid_provider_output"),
            (lambda answer: json.dumps({**answer, "status": "clarify", "claims": []}), "invalid_provider_output"),
            (lambda answer: json.dumps({**answer, "status": "partial", "claims": []}), "invalid_provider_output"),
            (lambda answer: changed_citation(answer, bundle_id="invented-bundle"), "citation_outside_evidence"),
            (lambda answer: changed_citation(answer, supporting_span_ids=["invented-span"]), "citation_outside_evidence"),
            (lambda answer: changed_citation(answer, quote={"source_span_id": "invented-span", "start": 0, "end": 1, "text": "N"}),
             "invalid_quote_anchor"),
            (lambda answer: "x" * 128_001, "invalid_provider_output"),
        ):
            hosted.answer_transform = transform
            rejected = wait_generation(client, generate(client, run))
            assert (rejected["state"], rejected["failure_code"], rejected["answer"]) == ("error", code, None)

        def deduplicate(answer):
            claim = answer["claims"][0]
            claim["qualifiers"] = []
            citation = claim["citations"][0]
            citation["quote"] = None
            claim["citations"].append(citation)
            return json.dumps(answer)

        hosted.answer_transform = deduplicate
        good = wait_generation(client, generate(client, run))
        assert len(good["answer"]["claims"][0]["citations"]) == 1
        saved = draft(client, text="", generated_operation_id=good["operation_id"]).raise_for_status().json()
        assert saved["claims"][0]["qualifiers"] == []
        assert saved["claims"][0]["citations"][0]["quote"] is None
        hosted.fault = "no_usage"
        bad = wait_generation(client, generate(client, run))
        assert (bad["failure_code"], bad["answer"]) == ("usage_unresolved", None)
        assert bad["attempts"][0]["cost_status"] == "unresolved_reservation_held"


def test_pack_keeps_bundle_atomic_when_evidence_overflows_the_budget(hosted):
    huge = {"bundle_id": "oversize", "mandatory_qualifier_span_ids": ["note"],
            "spans": [{"span_id": "value", "normalized_text": "🧬" * 4000},
                      {"span_id": "note", "normalized_text": "Keep mandatory context."}]}
    small = {"bundle_id": "fits", "mandatory_qualifier_span_ids": [],
             "spans": [{"span_id": "fits", "normalized_text": "Reviewed evidence."}]}
    text, included, tokens, incomplete = packed_context(hosted.adapter, {
        "question": "Which source qualifies?", "context": "", "bundles": [huge, small],
    })
    assert [bundle["bundle_id"] for bundle in included] == ["fits"]
    assert [item["bundle_id"] for item in json.loads(text)["evidence"]] == ["fits"]
    assert tokens < 10_000 and incomplete
    assert hosted.calls == 0
    with pytest.raises(ApiError, match="generator_input_too_long"):
        packed_context(hosted.adapter, {"question": "🧬" * 2000, "context": "🧬" * 2000, "bundles": []})


def test_two_transient_responses_exhaust_the_single_retry(tmp_path, hosted):
    hosted.fault = "transient_always"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        failed = wait_generation(client, generate(client, run))
        assert (failed["state"], failed["failure_code"], hosted.calls) == ("error", "provider_failed", 2)
        assert [item["state"] for item in failed["attempts"]] == ["transient_error", "transient_error"]
        assert search(client, "night setback")["result_kind"] == "evidence"


def test_late_reply_after_generation_is_disabled_preserves_reconciled_cost(tmp_path, hosted):
    hosted.fault = "late"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        try:
            assert hosted.called.wait(3)
            response = client.post(f"/generation/{operation}/attempts/1/reconcile", headers=HEADERS, json={
                "schema_version": "1.1", "actual_nanos": 4_000, "billing_reference": "Synthetic billing record.",
            })
            assert response.raise_for_status().json()["actual_nanos"] == 4_000
            client.post("/generation", headers=HEADERS, json={"schema_version": "1.1", "enabled": False}).raise_for_status()
        finally:
            hosted.released.set()
        record = wait_generation(client, operation)
        assert (record["state"], record["failure_code"], record["answer"]) == ("error", "generation_disabled", None)
        assert (record["attempts"][0]["actual_nanos"], record["attempts"][0]["cost_status"]) == (4_000, "reconciled")


def test_damaged_saved_run_cannot_supply_context_to_a_new_hosted_request(tmp_path, hosted):
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            raw, = db.execute("SELECT payload FROM runs WHERE id=?", (run["run_id"],)).fetchone()
            damaged = json.loads(raw)
            bundle = damaged["bundles"][0]
            bundle["spans"][0]["normalized_text"] = "Unverified replacement text"
            bundle["rendered_context"] = "Unverified replacement text"
            db.execute("UPDATE runs SET payload=? WHERE id=?", (json.dumps(damaged), run["run_id"]))
        outcome = wait_generation(client, generate(client, run))
        assert outcome["answer"]["status"] == "abstain" and outcome["incomplete_coverage"]
        assert outcome["attempts"] == [] and hosted.calls == 0
        view = client.get(f"/queries/{run['run_id']}", headers=HEADERS).raise_for_status().json()
        assert view["bundles"] == [] and view["incomplete_coverage"]
        assert "Unverified replacement text" not in json.dumps(view)


def test_generated_answer_checks_saved_evidence_again_before_display(tmp_path, hosted):
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        assert wait_generation(client, operation)["state"] == "result"
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE runs SET payload=json_set(payload,'$.bundles[0]',NULL) WHERE id=?", (run["run_id"],))
        answer = client.get(f"/generation/{operation}", headers=HEADERS).raise_for_status().json()
        assert (answer["state"], answer["failure_code"], answer["answer"]) == ("error", "saved_evidence_unavailable", None)
        view = client.get(f"/queries/{run['run_id']}", headers=HEADERS).raise_for_status().json()
        assert view["bundles"] == [] and view["incomplete_coverage"] and hosted.calls == 1
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("DELETE FROM snapshots WHERE id=?", (run["snapshot_id"],))
        answer = client.get(f"/generation/{operation}", headers=HEADERS).raise_for_status().json()
        assert (answer["state"], answer["failure_code"], answer["answer"]) == ("error", "saved_evidence_unavailable", None)


def test_generated_quote_with_only_a_normalized_gap_cannot_open_an_original_region(tmp_path, hosted):
    def gap_quote(output):
        citation = output["claims"][0]["citations"][0]
        citation["quote"].update(start=5, end=6, text=" ")
        return json.dumps(output)

    hosted.answer_transform = gap_quote
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        enable_fixture(client)
        pdf = pdf_with_pages(text_stream("Night", x=50) + text_stream("setback reduces demand.", x=85))
        manifest, _ = approve_and_prepare(client, pdf)
        publish(client, manifest["snapshot_id"])
        run = search(client, "night setback")
        span = run["bundles"][0]["spans"][0]
        assert span["normalized_text"].startswith("Night setback") and span["normalization_map"][5] is None
        outcome = wait_generation(client, generate(client, run))
        assert (outcome["state"], outcome["failure_code"], outcome["answer"]) == ("error", "invalid_quote_anchor", None)


@pytest.mark.parametrize("correction_arrives_first", [False, True])
def test_importing_failed_generation_evidence_disables_local_generation_control(tmp_path, hosted, correction_arrives_first):
    def bind_runs(report, *, day, execution):
        for repetition, retrieval in zip(report["generation"], report["retrieval"], strict=True):
            number = repetition["repetition"]
            raw = fingerprint([execution, number])
            retrieval["raw_execution_sha256"] = raw
            repetition["regression_run"] = {
                "model_hash": "fixed-model", "execution_sha256": raw,
                "outputs_sha256": fingerprint([execution, "outputs", number]),
                "started_at": f"2026-01-{day:02d}T00:00:00Z", "finished_at": f"2026-01-{day:02d}T00:00:01Z",
                "unresolved_cases": [], "budget_dispatches_checked": 1,
            }

    authority = PaidAuthority("local-test-only", 1_000_000)
    switch = {"schema_version": "1.1", "enabled": True}
    corrected = passing_generation_report()
    bind_runs(corrected, day=4, execution="corrected")
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        enable_fixture(client)
        workbench_only = submit(client, report_fixture()).raise_for_status().json()
        assert workbench_only["decisions"]["eligible_scope"] == "WORKBENCH_ONLY"
        assert not client.get("/generation", headers=HEADERS).raise_for_status().json()["enabled"]
        enable_fixture(client)
        report = report_fixture()
        report["decisions"]["generation"] = "FAIL"
        report["generation"] = [
            {"repetition": number, "generation_thresholds_met": False, "citation_set_accuracy": {"point": 0.97},
             "answer_completeness": {"point": 0.90}, "consequential_errors": 1}
            for number in (1, 2, 3)
        ]
        bind_runs(report, day=2, execution="failed")
        problem = fingerprint("affected case")
        report["generation"][0]["regression_run"]["unresolved_cases"] = [problem]
        if correction_arrives_first:
            earlier_pass = passing_generation_report()
            bind_runs(earlier_pass, day=1, execution="obsolete-correction")
            assert submit(client, earlier_pass).status_code == 201
            assert submit(client, corrected).status_code == 201
        saved = submit(client, report).raise_for_status().json()
        assert saved["decisions"]["eligible_scope"] == "WORKBENCH_ONLY"
        assert all("regression_run" not in repetition for repetition in saved["generation"])
        state = client.get("/generation", headers=HEADERS).raise_for_status().json()
        assert state["enabled"] is correction_arrives_first and state["dispatch_count"] == 0 and hosted.calls == 0
        if correction_arrives_first:
            assert client.post("/generation", headers=HEADERS, json=switch).status_code == 200
            return
        blocked = client.post("/generation", headers=HEADERS, json=switch)
        assert (blocked.status_code, blocked.json().get("code")) == (409, "generation_regressions_pending")
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=authority) as client:
        unrelated = passing_generation_report()
        bind_runs(unrelated, day=4, execution="unrelated")
        unrelated.update(protocol_sha256="different-protocol", recorded_at="2026-01-03T00:00:00Z")
        assert submit(client, unrelated).status_code == 201
        reassigned = submit(client, {**unrelated, "protocol_sha256": report["protocol_sha256"]})
        assert (reassigned.status_code, reassigned.json()["code"]) == (409, "regression_identity_changed")
        old = passing_generation_report()
        bind_runs(old, day=1, execution="old")
        assert submit(client, old).status_code == 201
        assert submit(client, {**old, "recorded_at": "2026-01-04T00:00:00Z"}).status_code == 201
        bind_runs(old, day=5, execution="old")
        replay = submit(client, old)
        assert (replay.status_code, replay.json()["code"]) == (409, "regression_identity_changed")
        incomplete = passing_generation_report()
        bind_runs(incomplete, day=3, execution="same-case-failed")
        incomplete["generation"][2]["regression_run"]["unresolved_cases"] = [problem]
        assert submit(client, incomplete).status_code == 201
        incomplete["generation"][2]["regression_run"]["unresolved_cases"] = []
        altered = submit(client, incomplete)
        assert (altered.status_code, altered.json()["code"]) == (409, "regression_identity_changed")
        incomplete["retrieval"][2]["raw_execution_sha256"] = "mismatched"
        assert submit(client, incomplete).json()["code"] == "invalid_report"
        assert client.post("/generation", headers=HEADERS, json=switch).status_code == 409
        assert submit(client, corrected).status_code == 201
        assert client.get("/generation", headers=HEADERS).json()["enabled"] is False
        enable_fixture(client)
        assert submit(client, report).status_code == 201
        assert client.get("/generation", headers=HEADERS).json()["enabled"] is True and hosted.calls == 0


def test_previous_hold_table_upgrades_without_dropping_a_safety_disable(tmp_path, hosted):
    with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
        db.execute(
            "CREATE TABLE generation_holds (report_id TEXT PRIMARY KEY, frozen_configuration TEXT NOT NULL, "
            "failed_at TEXT NOT NULL, resolved_by TEXT)"
        )
        db.execute("INSERT INTO generation_holds VALUES ('prior-report','prior-config','2026-01-01T00:00:00Z',NULL)")
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("local-test-only", 1_000_000)) as client:
        response = client.post("/generation", headers=HEADERS, json={"schema_version": "1.1", "enabled": True})
        assert (response.status_code, response.json()["code"], hosted.calls) == (409, "generation_regressions_pending", 0)
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            assert db.execute("SELECT reference,resolved_by FROM generation_holds").fetchall() == [("prior-report", None)]
