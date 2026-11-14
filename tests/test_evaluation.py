from test_admission import HEADERS, local_client, make_pdf
from test_corpus import approve_and_prepare, publish


def test_frozen_evaluation_inventory_contains_eligible_spans_without_gold(tmp_path):
    with local_client(tmp_path) as client:
        snapshot, _ = approve_and_prepare(client, make_pdf("Qualified lighting baseline."))
        publish(client, snapshot["snapshot_id"])
        response = client.get(f"/snapshots/{snapshot['snapshot_id']}/evaluation-inventory", headers=HEADERS)
        assert response.status_code == 200, response.text
        inventory = response.json()
        assert inventory["snapshot_id"] == snapshot["snapshot_id"]
        assert len(inventory["spans"]) == 1
        assert set(inventory) == {"schema_version", "snapshot_id", "eligibility_revision", "documents", "spans", "table_rows"}
        edition = snapshot["documents"][0]["version_id"]
        client.post(f"/editions/{edition}/withdraw", json={"schema_version": "1.1", "reason": "Test withdrawal"}, headers=HEADERS)
        assert client.get(f"/snapshots/{snapshot['snapshot_id']}/evaluation-inventory", headers=HEADERS).json()["spans"] == []
from retrofit.evaluate import eligibility, retrieval_metrics


def test_worked_precision_and_joint_required_evidence_oracles():
    gold = {"relevance_sets": [["A"], ["B", "C"]], "required_units": [[["A"], ["B", "C"]]]}
    one = retrieval_metrics([{"span_ids": ["A"]}], gold)
    assert (one["returned_precision"], one["ranked_precision"], one["recall"], one["p_at_8"]) == (1, 1, 1, 0.125)
    second = retrieval_metrics([{"span_ids": ["distractor"]}, {"span_ids": ["B", "C"]}], gold)
    assert (second["returned_precision"], second["ranked_precision"], second["recall"]) == (0.5, 0.5, 1)
    incomplete = retrieval_metrics([{"span_ids": ["B"]}], gold)
    assert incomplete["recall"] == 0


def test_d6_eligibility_keeps_repetition_gates_and_unmeasured_generation():
    assert eligibility("PASS", [True, True, True], [], False)["eligible_scope"] == "WORKBENCH_ONLY"
    assert eligibility("PASS", [True, True, False], [], False)["eligible_scope"] == "WITHHELD"
    assert eligibility("PASS", [True, True, True], [True, True, False], True)["eligible_scope"] == "WORKBENCH_ONLY"
    assert eligibility("NOT_EXECUTED", [True, True, True], [], False)["eligible_scope"] == "WITHHELD"
