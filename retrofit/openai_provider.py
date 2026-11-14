from collections.abc import Callable
from datetime import date
from importlib.metadata import version
import json
import os
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import Field
import tiktoken

from .contracts import ShortText, Versioned, canonical
from .hosted_policy import (
    INPUT_TOKENS, INSTRUCTIONS, M1_CAP_NANOS, PaidAuthority, ProviderFailure, ProviderInfo, ProviderReply,
)


class HostedPolicy(Versioned):
    d3_authority_reference: ShortText
    maximum_nanos: Annotated[int, Field(gt=0, le=M1_CAP_NANOS)]
    model_id: ShortText
    expected_model_identity: ShortText
    tokenizer: Literal["o200k_base", "cl100k_base"]
    terms_and_price_reference: ShortText
    verified_on: date
    input_nanos_per_token: Annotated[int, Field(gt=0, le=M1_CAP_NANOS)]
    output_nanos_per_token: Annotated[int, Field(gt=0, le=M1_CAP_NANOS)]
    token_count_endpoint_confirmed_nonbillable: Literal[True]


class OpenAIProvider:
    def __init__(self, policy: HostedPolicy, key: str, *, endpoint: httpx.URL | None = None) -> None:
        if endpoint is not None and (endpoint.scheme, endpoint.host) != ("http", "127.0.0.1"):
            raise ValueError("A supplied provider endpoint must be local loopback")
        self.endpoint = endpoint or httpx.URL("https://api.openai.com/v1/")
        self.info = ProviderInfo(
            policy.model_id, policy.expected_model_identity,
            f"{policy.tokenizer}/tiktoken-{version('tiktoken')}",
            f"{policy.verified_on.isoformat()} {policy.terms_and_price_reference}",
            policy.input_nanos_per_token, policy.output_nanos_per_token,
        )
        self.key = key
        self.encoding = tiktoken.get_encoding(policy.tokenizer)

    def tokens(self, text: str) -> int:
        return len(self.encoding.encode_ordinary(text))

    def input_tokens(self, text: str, schema: dict) -> int:
        # The provider's count endpoint verifies complete framing before inference.
        return self.tokens(text) + self.tokens(canonical(schema)) + self.tokens(INSTRUCTIONS)

    async def _post(self, client: httpx.AsyncClient, path: str, request: dict) -> dict:
        try:
            async with client.stream("POST", path, json=request) as response:
                if not response.is_success:
                    raise ProviderFailure(transient=response.status_code in {408, 429, 500, 502, 503, 504})
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 512_000:
                        raise ProviderFailure(transient=False)
                result = json.loads(body)
        except httpx.TransportError as exc:
            raise ProviderFailure(transient=True) from exc
        except ValueError as exc:
            raise ProviderFailure(transient=False) from exc
        if not isinstance(result, dict):
            raise ProviderFailure(transient=False)
        return result

    async def infer(
        self, text: str, schema: dict, *, max_output: int, timeout: float, before_dispatch: Callable[[], None],
    ) -> ProviderReply:
        payload = {
            "model": self.info.model,
            "instructions": INSTRUCTIONS,
            "input": text,
            "tools": [],
            "truncation": "disabled",
            "text": {"format": {"type": "json_schema", "name": "retrofit_answer", "strict": True, "schema": schema}},
            "reasoning": {"effort": "none"},
        }
        async with httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {self.key}"},
            timeout=timeout, follow_redirects=False, trust_env=False,
        ) as client:
            count = await self._post(client, "responses/input_tokens", payload)
            total = count.get("input_tokens")
            if type(total) is not int or not 0 <= total <= INPUT_TOKENS:
                return ProviderReply("", 0, 0, None)
            before_dispatch()
            result = await self._post(client, "responses", {
                **payload, "store": False, "max_output_tokens": max_output,
            })
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        actual_model = result.get("model")
        text_parts = []
        valid = result.get("status") == "completed" and actual_model == self.info.revision
        output = result.get("output")
        if not isinstance(output, list):
            output, valid = [], False
        for item in output:
            if not isinstance(item, dict):
                valid = False
                continue
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                valid = False
                continue
            content = item.get("content")
            if not isinstance(content, list):
                valid = False
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                else:
                    valid = False
        return ProviderReply(
            "".join(text_parts) if valid else "",
            usage.get("input_tokens") if type(usage.get("input_tokens")) is int else None,
            usage.get("output_tokens") if type(usage.get("output_tokens")) is int else None,
            actual_model.encode("utf-8", "backslashreplace").decode("utf-8") if isinstance(actual_model, str) else None,
        )


def load_policy(path: Path) -> tuple[OpenAIProvider, PaidAuthority]:
    if path.stat().st_mode & 0o077:
        raise ValueError("The hosted policy file must be private")
    policy = HostedPolicy.model_validate_json(path.read_bytes())
    if policy.verified_on != date.today():
        raise ValueError("Verify current provider, tokenizer, model, rates and terms before paid execution")
    key = os.environ.get("RETROFIT_OPENAI_API_KEY")
    if not key:
        raise ValueError("Missing restricted provider credential")
    return OpenAIProvider(policy, key), PaidAuthority(policy.d3_authority_reference, policy.maximum_nanos)
