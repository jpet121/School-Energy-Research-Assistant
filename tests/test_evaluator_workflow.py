import asyncio
from contextlib import closing
from hashlib import sha256
from itertools import combinations
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import httpx
import pytest

from retrofit.contracts import fingerprint
from retrofit.evaluate import COVERAGE_MINIMUMS, STRATA, client_for, query_case, run_comparison
from processes import python_module
from test_admission import MANIFEST, make_pdf


def test_external_evaluator_rejects_replay_on_a_real_loopback_run(tmp_path):
    def save(name, payload):
        path = tmp_path / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(*parts, failure=None):
        command = python_module("retrofit.evaluate", *parts)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        if failure:
            assert result.returncode != 0
            assert failure in result.stderr
        else:
            assert result.returncode == 0, result.stderr

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        python_module("retrofit", "--data", tmp_path / "app", "--port", port),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(120):
            try:
                client = client_for(origin)
                break
            except httpx.TransportError:
                time.sleep(0.05)
        else:
            raise AssertionError("The local evaluator test server did not start")
        with closing(client):
            document = make_pdf(*(f"Retrofit feature {index} has independently reviewed context." for index in range(1, 33)))
            response = client.post("/ingestions", files={"pdf": ("eval-fixture.pdf", document, "application/pdf")},
                                   data={"manifest": json.dumps(MANIFEST), "idempotency_key": "eval-fixture"}).raise_for_status()
            job = response.json()["job_id"]
            client.post(f"/ingestions/{job}/extract", json={}).raise_for_status()
            for _ in range(160):
                record = client.get(f"/ingestions/{job}").raise_for_status().json()
                if record["state"] != "extracting":
                    break
                time.sleep(0.05)
            extracted = record["extraction"]
            ids = [span["span_id"] for span in extracted["pages"][0]["spans"]]
            rows = [{
                "cell_span_ids": [ids[i]], "row_header_span_ids": [ids[6]],
                "column_header_span_ids": [ids[7]], "unit_span_ids": [ids[8]], "mandatory_note_span_ids": [ids[9]],
            } for i in range(5)]
            client.post(f"/ingestions/{job}/review", json={
                "schema_version": "1.1", "extraction_config_hash": extracted["config_hash"],
                "approved_span_ids": ids, "table_rows": rows, "qa_note": "Synthetic evaluator workflow fixture.",
            }).raise_for_status()
            snapshot = client.post("/snapshots", json={"schema_version": "1.1", "job_ids": [job]}).raise_for_status().json()["snapshot_id"]
            client.post(f"/snapshots/{snapshot}/publish", json={}).raise_for_status()
            inventory_path = tmp_path / "inventory.json"
            cli("inventory", "--origin", origin, "--snapshot", snapshot, "--out", inventory_path)
            inventory = json.loads(inventory_path.read_bytes())

        units = []
        for category, count in COVERAGE_MINIMUMS.items():
            if category == "table":
                candidates = [(row["ordered_span_ids"], row["row_id"]) for row in inventory["table_rows"]]
            elif category in {"multi_passage", "paired_assumptions"}:
                candidates = [(list(pair), None) for pair in combinations(ids, 2)]
            else:
                candidates = [([sid], None) for sid in ids]
            units += [{
                "unit_id": f"{category}-{index}", "category": category, "span_ids": span_ids,
                "row_id": row_id, "evaluator_note": "Synthetic protocol validation only.",
            } for index, (span_ids, row_id) in enumerate(candidates[:count])]
        cli("audit", inventory_path, save("coverage.json", {"units": units}), "--out", tmp_path / "audit.json")
        invalid_units = [
            {**units[0], "evaluator_note": None},
            {"unit_id": "outside", "category": "lighting", "span_ids": ["unverified"], "evaluator_note": "Fixture."},
            {"unit_id": "incomplete-table", "category": "table", "span_ids": [ids[0]], "row_id": inventory["table_rows"][0]["row_id"],
             "evaluator_note": "Fixture."},
            {"unit_id": "single-span-pair", "category": "multi_passage", "span_ids": [ids[0]], "evaluator_note": "Fixture."},
        ]
        invalid_audit = tmp_path / "audit-rejected.json"
        cli("audit", inventory_path, save("coverage-rejected.json", {"units": invalid_units}), "--out", invalid_audit)
        rejected = json.loads(invalid_audit.read_bytes())
        assert rejected["status"] == "HOLD"
        assert {entry["unit_id"] for entry in rejected["rejected"]} == {unit["unit_id"] for unit in invalid_units}
        cases = []
        for split in ("development", "held_out"):
            for subtype, count in STRATA.items():
                for _ in range(count):
                    sequence = len(cases) + 1
                    cases.append({
                        "case_id": f"fixture-{sequence}", "split": split, "subtype": subtype,
                        "leakage_cluster": f"fixture-cluster-{sequence}",
                        "question": f"Retrofit feature independently reviewed case {sequence}",
                    })
        gold = save("gold.json", {"cases": {case["case_id"]: {
            "relevance_sets": [[ids[15]]], "required_units": [[[ids[15]]]],
        } for case in cases}})
        source = save("protocol-input.json", {
            "schema_version": "1.1", "mode": "SELF_EVALUATED", "builder_uid": os.getuid(),
            "custody_reference": "fixture", "builder_exposure": "Synthetic evaluator integration test",
            "candidates": {"lexical-fixture": {"snapshot_id": snapshot, "mode": "lexical"}},
            "gold_sha256": sha256(gold.read_bytes()).hexdigest(), "labels_version": "fixture-1",
            "previous_gold_sha256": None, "retired_clusters": [], "cases": cases,
        })
        protocol, selection = tmp_path / "protocol.json", tmp_path / "selection.json"
        cli("freeze", source, invalid_audit, "--out", tmp_path / "must-not-freeze.json",
            failure="Coverage must pass before benchmark freeze")
        cli("freeze", source, tmp_path / "audit.json", "--out", protocol)
        frozen = json.loads(protocol.read_bytes())
        with pytest.raises(ValueError, match="only to the local loopback"):
            client_for(origin.replace("127.0.0.1", "localhost"))
        with pytest.raises(ValueError, match="before held-out execution"):
            run_comparison(frozen, origin, "lexical-fixture", "held_out", None, 1)
        with closing(client_for(origin)) as operator:
            async def failed_case():
                async with httpx.AsyncClient(base_url=origin, headers=operator.headers, trust_env=False) as client:
                    return await query_case(client, {"snapshot_id": "missing", "mode": "lexical"}, frozen["cases"][0])

            failed_query = asyncio.run(failed_case())
            assert (failed_query["status"], failed_query["failure_code"], failed_query["run_id"]) == (
                "error", "local_dependency_error", None,
            )
            assert operator.get("/generation").json()["dispatch_count"] == 0
        altered_budget = {key: value for key, value in frozen.items() if key != "protocol_sha256"}
        altered_budget["budget"] = {"candidates": 21, "bundles": 8}
        altered_budget["protocol_sha256"] = fingerprint(altered_budget)
        with pytest.raises(ValueError, match="did not match the frozen protocol"):
            run_comparison(altered_budget, origin, "lexical-fixture", "development", None, 1)
        consent = {
            "protocol_sha256": frozen["protocol_sha256"], "split": "development",
            "operator_confirmation_reference": "Synthetic preflight fixture",
            "pairs": [{key: case[key] for key in ("case_id", "question", "context")}
                      for case in frozen["cases"] if case["split"] == "development"],
        }
        with pytest.raises(ValueError, match="separately authorized"):
            run_comparison(frozen, origin, "lexical-fixture", "development", None, 1, consent)
        development = tmp_path / "development.json"
        cli("run", protocol, "--origin", origin, "--candidate", "lexical-fixture", "--split", "development", "--out", development)
        different_custody = {**json.loads(source.read_bytes()), "mode": "INDEPENDENT", "builder_exposure": None}
        independent = tmp_path / "independent-fixture.json"
        cli("freeze", save("independent-source.json", different_custody), tmp_path / "audit.json", "--out", independent)
        cli("select", independent, gold, development, "--out", tmp_path / "must-not-select.json",
            failure="separate custodian and private gold directory")
        cli("select", protocol, gold, development, "--out", selection)
        repetitions: list[Path] = []
        for number in (1, 2, 3):
            raw = tmp_path / f"repetition-{number}.json"
            cli("run", protocol, "--origin", origin, "--candidate", "lexical-fixture", "--split", "held_out",
                "--selection", selection, "--repetition", number, "--out", raw)
            repetitions.append(raw)
        report = tmp_path / "report.json"
        cli("report", protocol, gold, selection, *repetitions, "--out", report)
        with closing(client_for(origin)) as operator:
            imported = operator.post("/evaluation/reports", content=report.read_bytes(),
                                     headers={"Content-Type": "application/json"}).raise_for_status().json()
            assert imported["decisions"]["eligible_scope"] == "WITHHELD"
            assert len(imported["retrieval"]) == 3 and imported["retrieval"][0]["case_count"] == 90
            assert "case_scores" not in imported["retrieval"][0]
        replay = json.loads(repetitions[0].read_bytes())
        replay["repetition"] = 2
        cli("report", protocol, gold, selection, repetitions[0], save("replay.json", replay), repetitions[2],
            "--out", tmp_path / "must-not-exist.json", failure="separate run identities")
        with closing(client_for(origin)) as operator:
            operator.post(f"/editions/{inventory['documents'][0]['version_id']}/withdraw", json={
                "schema_version": "1.1", "reason": "Deliberate eligibility change after freeze.",
            }).raise_for_status()
        with pytest.raises(ValueError, match="same audited source"):
            run_comparison(frozen, origin, "lexical-fixture", "development", None, 1)
    finally:
        process.terminate()
        process.wait(timeout=10)
