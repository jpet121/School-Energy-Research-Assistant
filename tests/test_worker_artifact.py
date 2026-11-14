from copy import deepcopy
import json

import pytest

from pdf_fixture import pdf_with_pages, text_stream
from retrofit.contracts import ApiError
from retrofit.extraction import read_worker_artifact
from test_parser_boundary import parser_command


def test_artifact_boundary_rejects_inconsistent_or_unknown_worker_output():
    pdf = pdf_with_pages(text_stream("Approved first page."), text_stream("Approved second page."))
    actual = parser_command("extract", "[1,2]", pdf=pdf)
    assert actual.returncode == 0 and actual.stderr == b""
    received = read_worker_artifact(actual.stdout, [1, 2])
    assert [page["pdf_page_1based"] for page in received["pages"]] == [1, 2]
    mutations = [
        {**received, "page_count": 1},
        {**received, "pages": list(reversed(received["pages"]))},
        {"error": "sensitive-contents-not-an-approved-error"},
        [],
    ]
    for field, value in (
        ("pdf_page_1based", 2), ("page_width", 10), ("page_height", 20), ("normalization_map", []),
        ("raw_character_boxes", []), ("bounding_boxes", [[0, 0, float("nan"), 1]]),
    ):
        malformed = deepcopy(received)
        malformed["pages"][0]["spans"][0][field] = value
        mutations.append(malformed)
    for changed in mutations:
        with pytest.raises(ApiError, match="parser_artifact_invalid") as denied:
            read_worker_artifact(json.dumps(changed).encode(), [1, 2])
        assert denied.value.status == 422 and "sensitive-contents" not in denied.value.message
    unavailable = parser_command("extract", "[3]", pdf=pdf)
    assert unavailable.returncode == 0
    with pytest.raises(ApiError, match="coverage_page_missing"):
        read_worker_artifact(unavailable.stdout, [3])
