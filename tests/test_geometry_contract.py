import pytest

from retrofit.contracts import quote_geometry
from retrofit.generation import PaidAuthority


def test_only_contiguous_normalization_can_supply_exact_original_geometry():
    left = [10, 20, 16, 30]
    right = [20, 20, 26, 30]
    raw = {
        "raw_text": "AAB", "normalized_text": "AB", "raw_character_boxes": [left, left, right],
        "normalization_map": [0, 2],
    }
    assert quote_geometry(raw, 0, 2, "AB") == [[10, 20, 26, 30]]
    assert quote_geometry(raw, 0, 2, "AC") is None
    assert quote_geometry(raw, -1, 2, "AB") is None
    for update in (
        {"normalization_map": [0, 9]},
        {"normalization_map": [0, None]},
        {"normalization_map": [0, True]},
        {"raw_character_boxes": [left, right, right]},
        {"raw_text": "AAA"},
        {"normalized_text": "AA", "normalization_map": [1, 0]},
    ):
        span = {**raw, **update}
        assert quote_geometry(span, 0, 2, span["normalized_text"]) is None
    inserted_space = {**raw, "normalized_text": "A B", "normalization_map": [0, None, 2]}
    assert quote_geometry(inserted_space, 0, 3, "A B") == [[10, 20, 26, 30]]
    assert quote_geometry(inserted_space, 1, 2, " ") is None


@pytest.mark.parametrize(("reference", "amount"), [("", 1), ("fixture", 0), ("fixture", 25_000_000_001)])
def test_paid_authority_rejects_missing_reference_and_unapproved_budget(reference, amount):
    with pytest.raises(ValueError, match="M1 budget"):
        PaidAuthority(reference, amount)
