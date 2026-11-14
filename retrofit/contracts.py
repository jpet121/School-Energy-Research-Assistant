from datetime import date, datetime, timezone
from hashlib import sha256
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator


SCHEMA_VERSION = "1.1"
PDF_BYTES = 30 * 1024 * 1024
PDF_PAGES = 500
CORPUS_PAGES = 1_000
CORPUS_CHUNKS = 10_000
QUESTION_CHARS = 2_000
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def fingerprint(value: object) -> str:
    return digest(canonical(value).encode("utf-8"))


def box_within_page(box: list[float], width: float, height: float) -> bool:
    left, top, right, bottom = box
    return 0 <= left < right <= width and 0 <= top < bottom <= height


def quote_geometry(span: dict, start: int, end: int, text: str) -> list[list[float]] | None:
    source = span["normalized_text"]
    if not 0 <= start < end <= len(source) or source[start:end] != text:
        return None
    raw = span["raw_text"]
    positions = span.get("normalization_map")
    boxes = span.get("raw_character_boxes")
    if not isinstance(positions, list) or not isinstance(boxes, list) or len(positions) != len(source) or len(boxes) != len(raw):
        return None
    selected: list[int] = []
    for character, index in zip(text, positions[start:end]):
        if index is None and character.isspace():
            continue
        if type(index) is not int or not 0 <= index < len(raw) or index >= len(boxes) or raw[index] != character:
            return None
        if selected:
            previous = selected[-1]
            if index <= previous:
                return None
            skipped = [
                position for position in range(previous + 1, index) if not raw[position].isspace()
            ]
            if any(raw[position] != raw[previous] or boxes[position] != boxes[previous] for position in skipped):
                return None
        selected.append(index)
    if not selected:
        return None
    located = [boxes[index] for index in selected]
    return [[min(box[0] for box in located), min(box[1] for box in located),
             max(box[2] for box in located), max(box[3] for box in located)]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Versioned(Contract):
    schema_version: Literal["1.1"]


class CoverageRegion(Contract):
    first_page: Annotated[int, Field(strict=True, ge=1, le=PDF_PAGES)]
    last_page: Annotated[int, Field(strict=True, ge=1, le=PDF_PAGES)]
    use_basis: LongText

    @model_validator(mode="after")
    def valid_order(self) -> Self:
        if self.last_page < self.first_page:
            raise ValueError("Coverage page range is reversed")
        return self


class Admission(Versioned):
    title: ShortText
    publisher: ShortText
    source_url: HttpUrl
    publication_date: date | None = None
    unknown_reason: ShortText | None = None
    scope: ShortText
    use_basis: LongText
    public_document: Literal[True]
    coverage_regions: Annotated[list[CoverageRegion], Field(min_length=1, max_length=PDF_PAGES)]

    @model_validator(mode="after")
    def metadata(self) -> Self:
        if (self.publication_date is None) == (self.unknown_reason is None):
            raise ValueError("Provide a publication date or the reason it is unknown")
        if self.source_url.username or self.source_url.password:
            raise ValueError("Source URL cannot contain credentials")
        regions = sorted(self.coverage_regions, key=lambda region: region.first_page)
        if any(a.last_page >= b.first_page for a, b in zip(regions, regions[1:])):
            raise ValueError("Coverage page ranges overlap")
        return self
