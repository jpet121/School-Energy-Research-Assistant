"""Exact outstanding M1 fixtures, using HTTP and real persisted components."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from pdf_fixture import pdf_with_pages, text_stream
from retrofit.contracts import fingerprint
from retrofit.generation import PaidAuthority
from test_admission import HEADERS, MANIFEST, local_client, make_pdf, stage
from test_corpus import approve_and_prepare, publish
from test_embedding import models
from test_extraction import extract_pdf, wait_for_job
from test_generation import (
    confirmation, enable_fixture, evidence_run, generate, hosted, wait_generation,
)
from test_research import search
from retrofit.evaluate import checked_gold, choose_candidate, freeze_protocol, retrieval_metrics
from test_evaluator_boundaries import passing_audit, protocol_input
from test_evaluator_scoring import execution


def test_ac008_2_changed_parser_profile_reextracts_without_reusing_old_review(tmp_path):
    with local_client(tmp_path) as client:
        baseline, job = approve_and_prepare(client, make_pdf("Retain published school evidence."))
        publish(client, baseline["snapshot_id"])
        # Persist a preceding parser profile, as an existing store after a parser upgrade.
        record = client.get(f"/ingestions/{job}", headers=HEADERS).json()
        old = deepcopy(record["extraction"])
        old["profile"]["parser_version"] = "previous-profile-fixture"
        old["config_hash"] = fingerprint(old["profile"])
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("UPDATE ingestions SET extraction=? WHERE id=?", (json.dumps(old), job))
        started = client.post(f"/ingestions/{job}/extract", headers=HEADERS, json={})
        assert started.status_code == 202
        assert started.json()["state"] == "extracting"
        fresh = wait_for_job(client, job)
        assert fresh["state"] == "review_required"
        assert fresh["extraction"]["config_hash"] != old["config_hash"]
        assert "review" not in fresh["extraction"]
        assert search(client, "school")["snapshot_id"] == baseline["snapshot_id"]
        stale = client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
            "schema_version": "1.1", "extraction_config_hash": old["config_hash"],
            "approved_span_ids": old["review"]["approved_span_ids"], "qa_note": "Old review must not apply.",
        })
        assert stale.json()["code"] == "extraction_changed"


def test_ac007_1_ac008_1_interrupted_vector_replacement_and_concurrent_publication(tmp_path, models):
    with local_client(tmp_path, models=models) as client:
        _, job = approve_and_prepare(client, make_pdf("School lighting schedule."))
        request = {"schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small",
                   "include_vectors": True, "chunking": "fixed"}
        active = client.post("/snapshots", headers=HEADERS, json=request).raise_for_status().json()
        publish(client, active["snapshot_id"])
        lexical, other = approve_and_prepare(client, make_pdf("Replacement lighting evidence."), key="replacement")
        incomplete = client.post("/snapshots", headers=HEADERS, json={**request, "job_ids": [other]}).json()
        # Incomplete durable staging after interruption, not a mock of core logic.
        with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
            db.execute("DELETE FROM snapshot_vectors WHERE snapshot_id=?", (incomplete["snapshot_id"],))
        assert search(client, "lighting", mode="dense")["snapshot_id"] == active["snapshot_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            bad = pool.submit(client.post, f"/snapshots/{incomplete['snapshot_id']}/publish", headers=HEADERS, json={})
            good = pool.submit(client.post, f"/snapshots/{lexical['snapshot_id']}/publish", headers=HEADERS, json={})
            observations = [client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] for _ in range(10)]
            assert bad.result().status_code == 409
            assert good.result().status_code == 200
        assert set(observations) <= {active["snapshot_id"], lexical["snapshot_id"]}
        assert search(client, "lighting")["snapshot_id"] == lexical["snapshot_id"]
        assert search(client, "lighting", mode="dense", snapshot_id=active["snapshot_id"])["result_kind"] == "evidence"
    with local_client(tmp_path, models=models) as reopened:
        assert reopened.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] == lexical["snapshot_id"]
        assert reopened.post(f"/snapshots/{incomplete['snapshot_id']}/publish", headers=HEADERS, json={}).status_code == 409


def test_ac019_1_audit_reopens_two_failed_attempts_without_inference(tmp_path, hosted):
    hosted.fault = "transient_always"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("loopback fixture", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        operation = generate(client, run)
        saved = wait_generation(client, operation)
        assert len(saved["attempts"]) == 2 and hosted.calls == 2
        assert saved["failure_code"] == "provider_failed"
        assert saved["prompt_hash"] and saved["model_hash"] and saved["provider"]
        assert all(a["actual_model"] is None and a["state"] == "transient_error" for a in saved["attempts"])
        assert run["prompt_hash"] is None and run["model_hash"] is None and run["null_reason"]
        run = client.get(f"/queries/{run['run_id']}", headers=HEADERS).json()
    with local_client(tmp_path) as reopened:
        assert reopened.get(f"/queries/{run['run_id']}", headers=HEADERS).json() == run
        assert reopened.get(f"/generation/{operation}", headers=HEADERS).json() == saved
        assert hosted.calls == 2


def test_ac029_1_linked_contracts_and_successful_retry_survive_restart(tmp_path, hosted):
    hosted.fault = "transient_once"
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("loopback fixture", 1_000_000)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        saved = wait_generation(client, generate(client, run))
        assert saved["state"] == "result" and len(saved["attempts"]) == 2
        assert [a["state"] for a in saved["attempts"]] == ["transient_error", "received"]
        assert saved["run_id"] == run["run_id"]
        assert saved["answer"]["claims"][0]["citations"][0]["bundle_id"] in run["final_bundle_ids"]
        run = client.get(f"/queries/{run['run_id']}", headers=HEADERS).json()
        bundle = run["bundles"][0]
        evidence = client.get(f"/runs/{run['run_id']}/evidence/{bundle['bundle_id']}", headers=HEADERS).json()
        assert evidence["document"]["version_id"] == bundle["version_id"]
        assert evidence["document"]["byte_hash"] in evidence["document"]["original_path"]
        text = "Night setback reduced modeled heating demand."
        assert evidence["document"]["byte_hash"] == sha256(make_pdf(text)).hexdigest()
        span = evidence["spans"][0]
        assert span["normalized_text"] == text
        # The accepted contract permits inserted whitespace without a source glyph.
        assert span["raw_text"] == text.replace(" ", "")
        expected_map = []
        glyph = 0
        for character in text:
            expected_map.append(None if character == " " else glyph)
            if character != " ":
                glyph += 1
        assert span["normalization_map"] == expected_map
        # Helvetica 12-point N at the fixture's PDF (50,730), on a 792-point page.
        assert span["raw_character_boxes"][0] == pytest.approx([50, 52.484, 58.664, 64.484])
        assert span["pdf_page_1based"] == 1
        assert run["ranked_candidates"][0]["chunk_id"] == bundle["anchor_chunk_id"]
        assert saved["schema_version"] == run["schema_version"] == "1.1"
        for value in (evidence["document"]["retrieved_at"], run["requested_at"], saved["confirmed_at"]):
            assert datetime.fromisoformat(value).utcoffset() == timezone.utc.utcoffset(None)
    with local_client(tmp_path) as reopened:
        assert reopened.get(f"/queries/{run['run_id']}", headers=HEADERS).json() == run
        assert reopened.get(f"/generation/{saved['operation_id']}", headers=HEADERS).json() == saved
        assert reopened.get(f"/runs/{run['run_id']}/evidence/{bundle['bundle_id']}", headers=HEADERS).json() == evidence
        assert hosted.calls == 2


def test_ac024_2_gold_revision_does_not_relabel_frozen_score(tmp_path):
    source = protocol_input()
    source["candidates"]["small"] = {"snapshot_id": "vector-small", "mode": "dense"}
    gold = {"cases": {case["case_id"]: {"relevance_sets": [["A"]], "required_units": [[["A"]]]}
                      for case in source["cases"]}}
    path = tmp_path / "gold-v1.json"
    path.write_text(json.dumps(gold))
    source["gold_sha256"] = sha256(path.read_bytes()).hexdigest()
    protocol = freeze_protocol(source, passing_audit())
    original_protocol = deepcopy(protocol)
    original_score = retrieval_metrics([{"span_ids": ["B"]}], checked_gold(protocol, path)["cases"][source["cases"][0]["case_id"]])
    assert original_score["recall"] == 0
    revised = deepcopy(gold)
    for item in revised["cases"].values():
        item["required_units"][0].append(["B"])
        item["relevance_sets"].append(["B"])
    new_path = tmp_path / "gold-v2.json"
    new_path.write_text(json.dumps(revised))
    with pytest.raises(ValueError, match="label identity"):
        checked_gold(protocol, new_path)
    next_protocol = freeze_protocol({
        **source, "labels_version": "fixture-2", "previous_gold_sha256": source["gold_sha256"],
        "gold_sha256": sha256(new_path.read_bytes()).hexdigest(),
    }, passing_audit())
    assert next_protocol["protocol_sha256"] != protocol["protocol_sha256"]
    assert checked_gold(next_protocol, new_path) == revised
    assert protocol == original_protocol and original_score["recall"] == 0
    assert path.read_text() == json.dumps(gold)
    # Public evaluator scoring seam: two matched synthetic development executions.
    # These are decision fixtures, never claimed as independent corpus evaluation.
    comparison_gold = deepcopy(gold)
    for item in comparison_gold["cases"].values():
        item.update(relevance_sets=[["qualified"]], required_units=[[["qualified"]]])
    comparison_path = tmp_path / "comparison-gold-v1.json"
    comparison_path.write_text(json.dumps(comparison_gold))
    comparison_source = {**source, "gold_sha256": sha256(comparison_path.read_bytes()).hexdigest()}
    comparison_protocol = freeze_protocol(comparison_source, passing_audit())
    runs = [execution(comparison_protocol, name) for name in ("lexical", "small")]
    prior = choose_candidate(comparison_protocol, checked_gold(comparison_protocol, comparison_path), runs)
    score_path = tmp_path / "selection-v1.json"
    score_path.write_text(json.dumps(prior))
    prior_bytes = score_path.read_bytes()
    for item in comparison_gold["cases"].values():
        item["required_units"][0].append(["alternate"])
        item["relevance_sets"].append(["alternate"])
    updated_path = tmp_path / "comparison-gold-v2.json"
    updated_path.write_text(json.dumps(comparison_gold))
    with pytest.raises(ValueError, match="label identity"):
        checked_gold(comparison_protocol, updated_path)
    revised_protocol = freeze_protocol({
        **comparison_source, "labels_version": "fixture-2",
        "previous_gold_sha256": comparison_source["gold_sha256"],
        "gold_sha256": sha256(updated_path.read_bytes()).hexdigest(),
    }, passing_audit())
    with pytest.raises(ValueError):
        choose_candidate(revised_protocol, checked_gold(revised_protocol, updated_path), runs)
    revised_runs = [execution(revised_protocol, name) for name in ("lexical", "small")]
    revised_score = choose_candidate(revised_protocol, checked_gold(revised_protocol, updated_path), revised_runs)
    assert revised_score["protocol_sha256"] != prior["protocol_sha256"]
    assert revised_protocol["mode"] == comparison_protocol["mode"] == "SELF_EVALUATED"
    assert score_path.read_bytes() == prior_bytes


def test_ac024_4_gold_alternatives_survive_actual_rechunking(tmp_path, models):
    with local_client(tmp_path, models=models) as client:
        _, job = approve_and_prepare(client, make_pdf(
            "Lighting schedule reduces demand.", "Lighting controls reduce demand.",
            *(["School lighting schedules depend on occupancy and weather."] * 32)))
        span_sets = []
        chunk_sets = []
        for strategy in ("fixed", "structure"):
            prepared = client.post("/snapshots", headers=HEADERS, json={
                "schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-small",
                "chunking": strategy, "include_vectors": False,
            }).raise_for_status().json()
            publish(client, prepared["snapshot_id"])
            inventory = client.get(f"/snapshots/{prepared['snapshot_id']}/evaluation-inventory", headers=HEADERS).json()
            span_sets.append({s["span_id"] for s in inventory["spans"]})
            chunk_sets.append(prepared["chunk_set_hash"])
        assert span_sets[0] == span_sets[1] and chunk_sets[0] != chunk_sets[1]
        a, b = sorted(span_sets[0])[:2]
        gold = {"relevance_sets": [[a], [b]], "required_units": [[[a], [b]]]}
        identity = fingerprint(gold)
        for bundles in ([{"span_ids": [a]}], [{"span_ids": [b]}], [{"span_ids": [a]}, {"span_ids": [b]}]):
            assert retrieval_metrics(bundles, gold)["recall"] == 1
        assert retrieval_metrics([{"span_ids": [chunk_sets[0]]}], gold)["recall"] == 0
        assert fingerprint(gold) == identity


def test_ac021_1_exact_file_question_and_corpus_page_limits(tmp_path):
    with local_client(tmp_path) as client:
        pdf = make_pdf("Thirty MiB exact upload.").ljust(30 * 1024 * 1024, b" ")
        admitted = stage(client, pdf, key="bytes-at-limit")
        assert admitted.status_code == 201
        assert stage(client, pdf + b" ", key="bytes-over-limit").status_code == 413
        jobs = []
        for name, pages in (("A", 500), ("B", 500), ("C", 1)):
            prepared, job = approve_and_prepare(
                client, pdf_with_pages(text_stream(name + " school evidence."), *([""] * (pages - 1))), key=name)
            assert prepared["page_count"] == pages
            jobs.append(job)
        full = client.post("/snapshots", headers=HEADERS, json={"schema_version": "1.1", "job_ids": jobs[:2]}).json()
        assert full["page_count"] == 1000
        publish(client, full["snapshot_id"])
        over = client.post("/snapshots", headers=HEADERS, json={"schema_version": "1.1", "job_ids": jobs})
        assert over.json()["code"] == "corpus_page_limit"
        _, failed = extract_pdf(client, pdf_with_pages(*([""] * 501)), key="file-over-pages")
        assert failed["failure_code"] == "pdf_page_limit"
        assert search(client, "x" * 2000)["status"] == "result"
        assert client.post("/queries", headers=HEADERS, json={
            "schema_version": "1.1", "question": "x" * 2001, "mode": "lexical",
        }).json()["code"] == "invalid_contract"
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] == full["snapshot_id"]


def test_ac021_1_exact_ten_thousand_chunk_boundary(tmp_path):
    with local_client(tmp_path) as client:
        jobs = []
        for identity in ("A", "B"):
            pdf = pdf_with_pages(*[
                "".join(text_stream(f"{identity}{page:03d}{line:02d}", y=745 - 18 * line) for line in range(25))
                for page in range(200)
            ])
            manifest = {**MANIFEST, "coverage_regions": [
                {"first_page": 1, "last_page": 200, "use_basis": "Synthetic capacity fixture."}]}
            job, record = extract_pdf(client, pdf, manifest=manifest, key=identity)
            extraction = record["extraction"]
            spans = [s["span_id"] for page in extraction["pages"] for s in page["spans"]]
            assert len(spans) == 5000
            client.post(f"/ingestions/{job}/review", headers=HEADERS, json={
                "schema_version": "1.1", "extraction_config_hash": extraction["config_hash"],
                "approved_span_ids": spans, "qa_note": "Synthetic capacity fixture; not human corpus approval.",
            }).raise_for_status()
            jobs.append(job)
        exact = client.post("/snapshots", headers=HEADERS, json={"schema_version": "1.1", "job_ids": jobs}).raise_for_status().json()
        assert exact["chunk_count"] == 10000
        publish(client, exact["snapshot_id"])
        _, extra = approve_and_prepare(client, make_pdf("One extra chunk."), key="extra")
        over = client.post("/snapshots", headers=HEADERS, json={"schema_version": "1.1", "job_ids": [*jobs, extra]})
        assert over.json()["code"] == "corpus_chunk_limit"
        assert client.get("/snapshots", headers=HEADERS).json()["active_snapshot_id"] == exact["snapshot_id"]


@pytest.mark.parametrize("ceiling", ["requests", "attempts", "dollars"])
def test_ac028_1_count_and_dollar_limits_independently_block_dispatch(tmp_path, hosted, ceiling):
    allowance = 1 if ceiling == "dollars" else 1_000_000_000
    with local_client(tmp_path, provider=hosted.adapter, paid_authority=PaidAuthority("loopback fixture", allowance)) as client:
        enable_fixture(client)
        run = evidence_run(client)
        # Replay count-boundary records in the real durable ledger with zero billed cost.
        if ceiling != "dollars":
            with sqlite3.connect(tmp_path / "evidence.sqlite3") as db:
                db.executemany("INSERT INTO generation_ops VALUES (?,?,'error','development',?)", [
                    (f"boundary-{i}", run["run_id"], "{}") for i in range(800 if ceiling == "requests" else 1)])
                if ceiling == "attempts":
                    db.executemany("INSERT INTO provider_attempts VALUES ('boundary-0',?,0,0,?)", [
                        (i, json.dumps({"state": "provider_error", "dispatch_at": "2026-11-11T00:00:00Z"}))
                        for i in range(1, 1601)])
            run = search(client, "night setback")
        path, decision = confirmation(client, run)
        response = client.post(f"{path}/generate", headers=HEADERS, json=decision)
        if ceiling == "requests":
            assert response.json()["code"] == "request_count_limit"
        else:
            saved = wait_generation(client, response.raise_for_status().json()["operation_id"])
            assert saved["failure_code"] == "budget_limit"
        assert hosted.calls == 0
