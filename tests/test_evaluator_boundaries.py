from copy import deepcopy
from hashlib import sha256
import json
import os

import pytest

from retrofit.evaluate import (
    COVERAGE_MINIMUMS, STRATA, checked_gold, freeze_protocol, validate_hosted_manifest, verify_frozen,
)


def protocol_input():
    cases = []
    for split in ("development", "held_out"):
        for subtype in STRATA:
            for index in range(15):
                identity = f"{split}-{subtype}-{index}"
                cases.append({
                    "case_id": identity, "split": split, "subtype": subtype, "leakage_cluster": identity,
                    "question": f"Fixture input for {identity}", "context": "",
                })
    return {
        "schema_version": "1.1", "mode": "SELF_EVALUATED", "builder_uid": os.getuid(),
        "custody_reference": "Synthetic contract fixture", "builder_exposure": "Fixture labels known",
        "candidates": {"lexical": {"snapshot_id": "snapshot-a", "mode": "lexical"}},
        "gold_sha256": "1" * 64, "labels_version": "fixture-1",
        "previous_gold_sha256": None, "retired_clusters": [], "cases": cases,
    }


def passing_audit():
    return {"status": "PASS", "counts": dict(COVERAGE_MINIMUMS), "eligibility_revision": 2}


def test_hosted_manifest_requires_an_explicit_nonblank_operator_reference():
    protocol = freeze_protocol(protocol_input(), passing_audit())
    cases = [case for case in protocol["cases"] if case["split"] == "development"]
    manifest = {
        "protocol_sha256": protocol["protocol_sha256"], "split": "development",
        "operator_confirmation_reference": None,
        "pairs": [{key: case[key] for key in ("case_id", "question", "context")} for case in cases],
    }
    for reference in (None, False, 123, "   "):
        with pytest.raises(ValueError, match="operator confirmation"):
            validate_hosted_manifest({**manifest, "operator_confirmation_reference": reference}, protocol, "development", cases)
    manifest["operator_confirmation_reference"] = "Synthetic manifest validation reference"
    assert validate_hosted_manifest(manifest, protocol, "development", cases) is None
    with pytest.raises(ValueError, match="exact ordered"):
        validate_hosted_manifest({**manifest, "pairs": list(reversed(manifest["pairs"]))}, protocol, "development", cases)
    with pytest.raises(ValueError, match="frozen split"):
        validate_hosted_manifest(manifest, protocol, "held_out", cases)


def test_freeze_rejects_short_duplicate_leaked_and_unbalanced_inventories():
    source = protocol_input()
    audit = passing_audit()
    partial = {**audit, "counts": {**audit["counts"], "table": 4}}
    with pytest.raises(ValueError, match="Coverage must pass"):
        freeze_protocol(source, partial)
    for bad_cases in (source["cases"][:-1], [*source["cases"][:-1], source["cases"][0]]):
        with pytest.raises(ValueError, match="180 distinct"):
            freeze_protocol({**source, "cases": bad_cases}, audit)
    for field, value, message in (
        ("question", source["cases"][0]["question"], "Duplicate exact"),
        ("leakage_cluster", source["cases"][0]["leakage_cluster"], "crossed its boundary"),
        ("case_id", "DEV-01", "crossed its boundary"),
        ("subtype", "direct", "Incorrect stratification"),
    ):
        changed = deepcopy(source)
        changed["cases"][-1][field] = value
        with pytest.raises(ValueError, match=message):
            freeze_protocol(changed, audit)
    with pytest.raises(ValueError, match="retired cluster"):
        freeze_protocol({**source, "retired_clusters": [source["cases"][0]["leakage_cluster"]]}, audit)
    with pytest.raises(ValueError, match="candidate configurations"):
        freeze_protocol({**source, "candidates": {}}, audit)
    with pytest.raises(ValueError, match="protected labels separate"):
        freeze_protocol({**source, "mode": "INDEPENDENT"}, audit)
    frozen = freeze_protocol(source, audit)
    assert len(frozen["cases"]) == 180 and frozen["repetitions"] == 3
    assert verify_frozen(frozen) is None
    with pytest.raises(ValueError, match="frozen protocol changed"):
        verify_frozen({**frozen, "repetitions": 2})


def test_gold_uses_private_files_and_frozen_inventory(tmp_path):
    directory = tmp_path / "custody-fixture"
    directory.mkdir(mode=0o700)
    path = directory / "gold.json"
    source = protocol_input()
    gold = {"cases": {case["case_id"]: {"relevance_sets": [], "required_units": []} for case in source["cases"]}}
    path.write_text(json.dumps(gold))
    path.chmod(0o600)
    scope = {
        **source, "mode": "INDEPENDENT", "builder_uid": os.getuid() + 1, "builder_exposure": None,
        "gold_sha256": sha256(path.read_bytes()).hexdigest(),
    }
    frozen = freeze_protocol(scope, passing_audit())
    assert checked_gold(frozen, path) == gold
    path.chmod(0o640)
    with pytest.raises(ValueError, match="separate custodian"):
        checked_gold(frozen, path)
    path.chmod(0o600)
    directory.chmod(0o750)
    with pytest.raises(ValueError, match="private gold directory"):
        checked_gold(frozen, path)
    directory.chmod(0o700)
    missing = {"cases": {}}
    path.write_text(json.dumps(missing))
    with pytest.raises(ValueError, match="label identity"):
        checked_gold(frozen, path)
    with pytest.raises(ValueError, match="exact frozen case inventory"):
        checked_gold(freeze_protocol({**scope, "gold_sha256": sha256(path.read_bytes()).hexdigest()}, passing_audit()), path)
    case_id = next(case["case_id"] for case in source["cases"] if case["subtype"] == "table")
    gold["cases"][case_id] = {"relevance_sets": [[]], "required_units": [[[]]]}
    path.write_text(json.dumps(gold))
    with pytest.raises(ValueError, match="nonempty source-span sets"):
        checked_gold(freeze_protocol({**scope, "gold_sha256": sha256(path.read_bytes()).hexdigest()}, passing_audit()), path)
