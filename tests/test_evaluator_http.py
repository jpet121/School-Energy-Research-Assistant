import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest

from retrofit.evaluate import client_for, evaluate_cases, generation_case, query_case


@contextmanager
def evaluator_endpoint(*, delayed=False, cancel_status=200, terminal=False, slow_submit=False, pending_once=False, recovery=False):
    calls = []

    class Endpoint(BaseHTTPRequestHandler):
        def send_body(self, payload, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass

        def do_GET(self):
            calls.append(("GET", self.path))
            if self.path == "/":
                self.send_body(b"<html>Missing session metadata</html>", "text/html")
                return
            if self.path.endswith("/hosted-input"):
                self.send_body(b'{"question":"Lighting","context":"","confirmation_id":"fixture"}', "application/json")
                return
            if recovery and ("POST", self.path + "/cancel") in calls:
                self.send_body(b'{"status":"canceled","state":"canceled","attempts":[{"state":"canceled"}]}', "application/json")
                return
            if delayed == "trickle":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                for _ in range(10):
                    try:
                        self.wfile.write(b" " * 10)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    sleep(0.05)
                return
            if delayed:
                sleep(0.5)
            if self.path.startswith("/generation/"):
                finished = terminal and (not pending_once or calls.count(("GET", self.path)) > 1)
                payload = b'{"state":"result","answer":{"status":"abstain"}}' if finished else b'{"state":"pending"}'
            elif terminal:
                payload = json.dumps({
                    "status": "result", "snapshot_id": "fixture", "mode_requested": "lexical",
                    "ranked_candidates": [], "bundles": [], "eligibility_revision": 2, "attempts": [{"status": "result"}],
                }).encode()
            else:
                payload = b'{"status":"pending"}'
            self.send_body(payload, "application/json")

        def do_POST(self):
            calls.append(("POST", self.path))
            self.rfile.read(int(self.headers["Content-Length"]))
            if slow_submit:
                sleep(0.5)
            status = cancel_status if self.path.endswith("/cancel") else 200
            payload = {"operation_id": "fixture-op"} if self.path.endswith("/generate") else {"run_id": "fixture-run"}
            self.send_body(json.dumps(payload).encode(), "application/json", status)

    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_evaluator_denies_a_local_page_without_the_workbench_session():
    with evaluator_endpoint() as (origin, calls):
        with pytest.raises(ValueError, match="Local session unavailable"):
            client_for(origin)
        assert calls == [("GET", "/")]


@pytest.mark.parametrize(("delayed", "recovery"), [(False, True), (False, False), (True, False), ("trickle", False)])
def test_evaluator_deadline_is_bounded_by_remaining_time_and_cancels_started_query(delayed, recovery):
    with evaluator_endpoint(delayed=delayed, recovery=recovery) as (origin, calls):
        async def evaluate():
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                return await query_case(
                    client, {"snapshot_id": "fixture", "mode": "lexical"},
                    {"case_id": "fixture-case", "question": "Lighting", "context": ""}, deadline_seconds=0.2,
                )

        started = monotonic()
        result = asyncio.run(evaluate())
        assert monotonic() - started < 0.43
        assert (result["status"], result["failure_code"], result["run_id"]) == ("error", "evaluation_deadline", "fixture-run")
        assert calls[0] == ("POST", "/queries")
        assert calls[-2:] == [("POST", "/queries/fixture-run/cancel"), ("GET", "/queries/fixture-run")]
        assert not result["bundles"]
        assert result["attempts"] == ([{"state": "canceled"}] if recovery else None)


@pytest.mark.parametrize(("cancel_status", "code"), [(200, "evaluation_deadline"), (503, "evaluation_cancel_failed")])
def test_hosted_evaluator_cancels_its_operation_at_deadline_and_retains_cancel_failures(cancel_status, code):
    with evaluator_endpoint(delayed="trickle", cancel_status=cancel_status, recovery=cancel_status == 200) as (origin, calls):
        async def evaluate():
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                return await generation_case(
                    client, {"status": "result", "run_id": "fixture-run"}, {"question": "Lighting", "context": ""},
                    "held_out", deadline_seconds=0.2,
                )

        result = asyncio.run(evaluate())
        assert (result["state"], result["failure_code"], result["operation_id"]) == ("error", code, "fixture-op")
        assert result["attempts"] == ([{"state": "canceled"}] if cancel_status == 200 else None)
        assert result.get("answer") is None
        assert calls[-2:] == [("POST", "/generation/fixture-op/cancel"), ("GET", "/generation/fixture-op")]


def test_hosted_evaluator_checks_exact_preview_and_retains_the_terminal_result():
    with evaluator_endpoint(terminal=True, pending_once=True) as (origin, calls):
        async def evaluate():
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                failed = {"status": "error"}
                ready = {"status": "result", "run_id": "fixture-run"}
                case = {"question": "Lighting", "context": ""}
                assert (await generation_case(client, failed, case, "held_out"))["failure_code"] == "retrieval_failed"
                assert (await generation_case(client, ready, {**case, "question": "Changed"}, "held_out"))["failure_code"] == "hosted_evaluation_failed"
                return await generation_case(client, ready, case, "held_out")

        result = asyncio.run(evaluate())
        assert result["answer"]["status"] == "abstain"
        assert calls.count(("POST", "/queries/fixture-run/generate")) == 1
        assert calls.count(("GET", "/generation/fixture-op")) == 2


def test_evaluator_deadline_before_submission_response_never_cancels_an_unknown_operation():
    case = {"case_id": "fixture-case", "question": "Lighting", "context": ""}
    with evaluator_endpoint(slow_submit=True) as (origin, calls):
        async def evaluate():
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                retrieval = await query_case(client, {"snapshot_id": "fixture", "mode": "lexical"}, case, deadline_seconds=0.15)
                hosted = await generation_case(
                    client, {"status": "result", "run_id": "fixture-run"}, case, "held_out", deadline_seconds=0.15,
                )
                return retrieval, hosted

        retrieval, hosted = asyncio.run(evaluate())
        assert (retrieval["status"], retrieval["failure_code"], retrieval["run_id"]) == ("error", "evaluation_deadline", None)
        assert retrieval["attempts"] is None
        assert hosted == {"state": "error", "failure_code": "evaluation_deadline", "operation_id": None, "attempts": None, "answer": None}
        assert calls == [("POST", "/queries"), ("GET", "/queries/fixture-run/hosted-input"), ("POST", "/queries/fixture-run/generate")]


def test_evaluator_keeps_hosted_outcome_separate_from_recorded_retrieval():
    case = {"case_id": "fixture-case", "question": "Lighting", "context": ""}
    with evaluator_endpoint(terminal=True) as (origin, calls):
        outcomes, generated = asyncio.run(evaluate_cases(
            origin, httpx.Headers(), {"snapshot_id": "fixture", "mode": "lexical"}, [case], "held_out", True,
        ))
        assert outcomes[0]["case_id"] == "fixture-case" and outcomes[0]["run_id"] == "fixture-run"
        assert outcomes[0]["bundles"] == [] and generated["fixture-case"]["answer"]["status"] == "abstain"
        outcomes, generated = asyncio.run(evaluate_cases(
            origin, httpx.Headers(), {"snapshot_id": "different", "mode": "lexical"}, [case], "held_out", False,
        ))
        assert outcomes[0]["failure_code"] == "local_dependency_error" and generated == {}
        assert calls.count(("POST", "/queries/fixture-run/generate")) == 1
