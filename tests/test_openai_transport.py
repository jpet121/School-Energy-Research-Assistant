import asyncio
from collections import deque
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
from threading import Thread

import httpx
import pytest

from retrofit.generation import ProviderFailure
from retrofit.openai_provider import HostedPolicy, OpenAIProvider, load_policy


def policy_fields():
    return {
        "schema_version": "1.1", "d3_authority_reference": "Local transport fixture",
        "maximum_nanos": 1_000_000, "model_id": "fixture-model", "expected_model_identity": "fixture-version",
        "tokenizer": "cl100k_base", "terms_and_price_reference": "Synthetic rates",
        "verified_on": date.today().isoformat(), "input_nanos_per_token": 10, "output_nanos_per_token": 20,
        "token_count_endpoint_confirmed_nonbillable": True,
    }


@contextmanager
def response_endpoint(*responses):
    remaining = deque(responses)

    def respond(path, payload):
        assert remaining, f"Unexpected request to {path}"
        return remaining.popleft()

    with serve_responses(respond) as server:
        yield server


@contextmanager
def serve_responses(respond):
    received = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers.get("Authorization"), payload))
            status, body = respond(self.path, payload)
            if status is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            content = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpx.URL(f"http://127.0.0.1:{server.server_port}/v1/"), received
        finally:
            server.shutdown()
            thread.join(timeout=5)


def provider(endpoint):
    return OpenAIProvider(HostedPolicy.model_validate(policy_fields()), "synthetic-local-key", endpoint=endpoint)


def invoke(adapter, dispatches):
    return asyncio.run(adapter.infer(
        '{"question":"School lighting?"}', {"type": "object"}, max_output=80, timeout=2,
        before_dispatch=lambda: dispatches.append("authorized"),
    ))


def test_provider_rejects_redirect_body_before_any_inference_dispatch():
    with response_endpoint((302, {"input_tokens": 7})) as (endpoint, received):
        dispatches = []
        with pytest.raises(ProviderFailure) as error:
            invoke(provider(endpoint), dispatches)
        assert error.value.transient is False
        assert [path for path, _, _ in received] == ["/v1/responses/input_tokens"]
        assert dispatches == []


def test_transport_override_cannot_target_another_host():
    with pytest.raises(ValueError, match="local loopback"):
        provider(httpx.URL("https://example.org/v1/"))


def test_hosted_policy_must_be_private_current_and_explicitly_credentialed(tmp_path):
    path = tmp_path / "hosted.json"
    fields = policy_fields()
    path.write_text(json.dumps(fields))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="must be private"):
        load_policy(path)
    path.chmod(0o600)
    path.write_text(json.dumps({**fields, "verified_on": "2000-01-01"}))
    with pytest.raises(ValueError, match="Verify current provider"):
        load_policy(path)
    path.write_text(json.dumps(fields))
    variable = "RETROFIT_OPENAI_API_KEY"
    previous = os.environ.pop(variable, None)
    try:
        with pytest.raises(ValueError, match="Missing restricted provider credential"):
            load_policy(path)
        os.environ[variable] = "synthetic-local-policy-fixture"
        adapter, authority = load_policy(path)
    finally:
        os.environ.pop(variable, None)
        if previous is not None:
            os.environ[variable] = previous
    assert (authority.reference, authority.maximum_nanos) == ("Local transport fixture", 1_000_000)
    assert (adapter.info.model, adapter.info.revision) == ("fixture-model", "fixture-version")


def test_real_http_token_count_and_response_keep_dispatch_policy():
    content = [{"type": "output_text", "text": '{"status":'}, {"type": "output_text", "text": '"abstain"}'}]
    result = {
        "status": "completed", "model": "fixture-version", "usage": {"input_tokens": 44, "output_tokens": 9},
        "output": [{"type": "reasoning"}, {"type": "message", "role": "assistant", "content": content}],
    }
    with response_endpoint((200, {"input_tokens": 44}), (200, result)) as (endpoint, received):
        dispatches = []
        adapter = provider(endpoint)
        assert adapter.tokens("Hello world") == 2
        assert adapter.input_tokens("Lighting.", {"type": "object"}) > 2
        reply = invoke(adapter, dispatches)
        assert (reply.output, reply.usage_input, reply.usage_output, reply.actual_model) == (
            '{"status":"abstain"}', 44, 9, "fixture-version",
        )
        assert dispatches == ["authorized"]
        assert [path for path, _, _ in received] == ["/v1/responses/input_tokens", "/v1/responses"]
        preflight, inference = received
        assert inference[1] == "Bearer synthetic-local-key"
        assert preflight[2]["input"] == inference[2]["input"] == '{"question":"School lighting?"}'
        assert inference[2]["store"] is False and inference[2]["max_output_tokens"] == 80
        assert inference[2]["tools"] == [] and inference[2]["text"]["format"]["strict"] is True


@pytest.mark.parametrize(("status", "body", "transient"), [
    (503, {}, True), (401, {}, False), (200, b"not JSON", False),
    (200, [], False), (200, b"a" * 512_001, False), (None, b"", True),
], ids=["transient-status", "denied", "bad-json", "wrong-shape", "oversize", "disconnect"])
def test_transport_failures_are_classified_without_crossing_dispatch_boundary(status, body, transient):
    with response_endpoint((status, body)) as (endpoint, received):
        dispatches = []
        with pytest.raises(ProviderFailure) as error:
            invoke(provider(endpoint), dispatches)
        assert error.value.transient is transient and dispatches == []
        assert len(received) == 1


@pytest.mark.parametrize("count", [None, True, -1, 100_000])
def test_invalid_token_count_blocks_inference(count):
    with response_endpoint((200, {"input_tokens": count})) as (endpoint, received):
        dispatches = []
        reply = invoke(provider(endpoint), dispatches)
        assert reply.output == "" and reply.actual_model is None
        assert len(received) == 1 and dispatches == []


@pytest.mark.parametrize(("output", "status", "model", "usage"), [
    (None, "completed", None, False),
    ([False], "completed", "fixture-version", {"input_tokens": True, "output_tokens": "1"}),
    ([{"type": "function_call"}], "completed", "fixture-version", None),
    ([{"type": "message", "role": "user"}], "completed", "fixture-version", {}),
    ([{"type": "message", "role": "assistant", "content": None}], "completed", "fixture-version", {}),
    ([{"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}], "completed", "fixture-version", {}),
    ([{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "{}"}]}],
     "incomplete", "fixture-version", {}),
])
def test_unexpected_hosted_output_cannot_become_an_answer(output, status, model, usage):
    with response_endpoint((200, {"input_tokens": 44}), (200, {
        "output": output, "status": status, "model": model, "usage": usage,
    })) as (endpoint, received):
        dispatches = []
        reply = invoke(provider(endpoint), dispatches)
        assert (reply.output, reply.usage_input, reply.usage_output) == ("", None, None)
        assert dispatches == ["authorized"] and len(received) == 2
