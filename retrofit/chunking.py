from itertools import groupby
import json
from typing import Literal

from tokenizers import Tokenizer

from .contracts import ApiError, fingerprint
from .embeddings import CHUNK_INPUT_LIMIT, MODEL_INPUT_LIMIT


Strategy = Literal["span", "fixed", "structure"]
OVERLAP = 40
CHUNK_PAYLOAD_IDENTITY = "sha256-payload-v1"


def fits(tokenizer: Tokenizer, text: str) -> bool:
    return (
        len(tokenizer.encode(text, add_special_tokens=False).ids) <= CHUNK_INPUT_LIMIT
        and len(tokenizer.encode(text).ids) <= MODEL_INPUT_LIMIT
    )


def section_prefix(prefix: str, span: dict) -> str:
    section = span.get("section_path", [])
    return prefix + (" / ".join(section) + "\n" if section else "")


def source_window(spans: list[dict]) -> tuple[str, list[tuple[int, int, dict]]]:
    ranges = []
    offset = 0
    for span in spans:
        end = offset + len(span["normalized_text"])
        ranges.append((offset, end, span))
        offset = end + 1
    return "\n".join(span["normalized_text"] for span in spans), ranges


def windows(spans: list[dict], strategy: Strategy, tokenizer: Tokenizer, prefix: str) -> list[tuple[str, list[dict]]]:
    target = 320 if strategy == "fixed" else 350
    groups = [spans] if strategy == "fixed" else [
        list(group) for _, group in groupby(spans, key=lambda span: tuple(span.get("section_path", [])))
    ]
    result = []
    for group in groups:
        context_prefix = section_prefix(prefix, group[0]) if strategy == "structure" else prefix
        text, ranges = source_window(group)
        offsets = tokenizer.encode(text, add_special_tokens=False).offsets
        start = 0
        while start < len(offsets):
            end = min(len(offsets), start + target)
            left = offsets[start][0]
            right = offsets[end - 1][1]
            while end > start and not fits(tokenizer, context_prefix + text[left:right]):
                end -= 1
                right = offsets[end - 1][1] if end > start else left
            if end == start:
                raise ApiError(400, "chunk_prefix_too_large", "The complete document prefix leaves no space for a chunk.")
            selected = [span for first, last, span in ranges if first < right and last > left]
            result.append((text[left:right], selected))
            if end == len(offsets):
                break
            start = max(start + 1, end - OVERLAP)
    return result


def make_chunk(
    spans: list[dict], version: str, *, row: dict | None = None, text: str | None = None,
    tokenizer: Tokenizer | None = None, prefix: str = "", strategy: Strategy = "span",
) -> dict:
    span_ids = [span["span_id"] for span in spans]
    section = spans[0].get("section_path", [])
    effective_prefix = section_prefix(prefix, spans[0]) if tokenizer and strategy == "structure" else prefix
    config_hash = fingerprint({"strategy": strategy, "overlap": OVERLAP, "limit": CHUNK_INPUT_LIMIT, "prefix": effective_prefix})
    context = "\n".join(span["normalized_text"] for span in spans)
    chunk_text = context if text is None else text
    if row:
        primary = set(row["cell_span_ids"] + row["row_header_span_ids"] + row["column_header_span_ids"])
        chunk_text = "\n".join(span["normalized_text"] for span in spans if span["span_id"] in primary)
    embedding_text = effective_prefix + chunk_text
    tokens = len(tokenizer.encode(embedding_text).ids) if tokenizer else None
    if tokenizer and not fits(tokenizer, embedding_text):
        raise ApiError(400, "chunk_token_limit", "The complete embedding input exceeds the token limit. Narrow the source scope.")
    payload = {
        "source_span_ids": span_ids, "section_path": section, "normalized_text": chunk_text, "rendered_context": context,
        "embedding_token_count": tokens, "embedding_text": embedding_text, "chunk_config_hash": config_hash,
        "table_row_id": row["row_id"] if row else None, "table_row": row, "spans": spans,
    }
    return {**payload, "chunk_id": fingerprint([version, payload])}


def required_qualifiers(row: dict | None) -> list[str]:
    return list(dict.fromkeys(
        row["row_header_span_ids"] + row["column_header_span_ids"] + row["unit_span_ids"] + row["mandatory_note_span_ids"]
    )) if row else []


def read_snapshot_chunk(raw: str, chunk_id: str, version: str, payload_identity: str | None) -> dict:
    try:
        chunk = json.loads(raw)
        stored_id = chunk.pop("chunk_id")
        ids = chunk["source_span_ids"]
        spans = chunk["spans"]
        if payload_identity == CHUNK_PAYLOAD_IDENTITY:
            computed = fingerprint([version, chunk])
        elif payload_identity is None:
            # Previously published snapshots retain their recorded identifier scheme.
            computed = fingerprint([version, ids, chunk["table_row"], chunk["chunk_config_hash"], chunk["normalized_text"]])
        else:
            raise ValueError("Unknown chunk identity format")
        if stored_id != chunk_id or computed != chunk_id:
            raise ValueError("Stored chunk does not match its identity")
        if (
            [span["span_id"] for span in spans] != ids
            or not set(required_qualifiers(chunk["table_row"])) <= set(ids)
            or "\n".join(span["normalized_text"] for span in spans) != chunk["rendered_context"]
        ):
            raise ValueError("Stored chunk is missing source context")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ApiError(409, "incomplete_snapshot", "Stored evidence fails its recorded content or source references.") from exc
    return {**chunk, "chunk_id": stored_id}
