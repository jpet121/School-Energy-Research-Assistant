from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import resource
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from processes import python_launcher, python_module
from pdf_fixture import pdf_with_pages, text_stream
from retrofit.evaluate import client_for
from retrofit.contracts import ApiError
from retrofit.extraction import WorkerCanceled, supervise_worker
from test_admission import HEADERS, MANIFEST, local_client, make_pdf
from test_corpus import approve_and_prepare, publish


def wait_for(observe, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = observe()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail("The real loopback process did not reach the expected state.")


def free_origin():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def command(root, origin, *args):
    return python_module("retrofit", "--data", root, "--port", origin.rsplit(":", 1)[1], *args)


@contextmanager
def launch(root, origin, *, host_limit=None, models=None, hosted_endpoint=None):
    startup = command(root, origin, *("--models", models) if models else ())
    if host_limit:
        startup = [
            *python_launcher(), str(Path(__file__).with_name("run_under_host_limit.py")), host_limit,
            "--data", str(root), "--port", origin.rsplit(":", 1)[1],
        ]
    if hosted_endpoint:
        startup = [*python_launcher(), str(Path(__file__).with_name("hosted_service.py")), str(root), origin, str(hosted_endpoint)]
    process = subprocess.Popen(startup, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        def connect():
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            try:
                return client_for(origin)
            except httpx.TransportError:
                return None

        with closing(wait_for(connect)) as client:
            yield process, client
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=15)


def stage(client, key, *, pages=1, density=1):
    # Give the process observer time to read kernel controls on an active extraction.
    pdf = pdf_with_pages(*[
        "".join(text_stream(f"{key} Page {number}.", y=700 - 16 * line) for line in range(density))
        for number in range(1, pages + 1)
    ])
    manifest = {**MANIFEST, "coverage_regions": [{**MANIFEST["coverage_regions"][0], "last_page": pages}]}
    return client.post(
        "/ingestions", files={"pdf": ("fixture.pdf", pdf, "application/pdf")},
        data={"manifest": json.dumps(manifest), "idempotency_key": key},
    ).raise_for_status().json()["job_id"]


def test_real_service_blocks_extraction_when_host_denies_required_isolation(tmp_path):
    with launch(tmp_path / "app", free_origin(), host_limit="install-filter") as (_, client):
        profile = client.get("/parser").raise_for_status().json()
        assert profile["enabled"] is False
        assert "isolation self-check failed" in profile["reason"]
        job = stage(client, "Stay staged without isolation.")
        denied = client.post(f"/ingestions/{job}/extract", json={})
        assert (denied.status_code, denied.json()["code"]) == (503, "parser_isolation_unavailable")
        assert client.get(f"/ingestions/{job}").json()["state"] == "staged"
        assert client.get("/generation").json()["dispatch_count"] == 0


def test_real_semantic_query_cancel_and_restart_preserve_the_active_collection(tmp_path):
    assets = os.environ.get("RETROFIT_TEST_MODELS")
    if not assets:
        pytest.skip("Set RETROFIT_TEST_MODELS to exercise actual cold-start vector retrieval.")
    root = tmp_path / "app"
    with local_client(root, models=Path(assets)) as setup:
        _, job = approve_and_prepare(setup, make_pdf("Lighting controls preserve energy evidence."))
        prepared = setup.post("/snapshots", headers=HEADERS, json={
            "schema_version": "1.1", "job_ids": [job], "embedding_model": "bge-base",
            "chunking": "fixed", "include_vectors": True,
        }).raise_for_status().json()
        snapshot = publish(setup, prepared["snapshot_id"])["active_snapshot_id"]
    payload = {"schema_version": "1.1", "question": "lighting", "mode": "dense"}
    origin = free_origin()
    with launch(root, origin, models=assets) as (_, client):
        first = client.post("/queries", json=payload).raise_for_status().json()["run_id"]
        busy = client.post("/queries", json=payload)
        assert (busy.status_code, busy.json()["code"]) == (429, "query_busy")
        assert client.post(f"/queries/{first}/cancel", json={}).raise_for_status().json()["state"] == "canceled"
        lexical = client.post("/queries", json={**payload, "mode": "lexical"}).raise_for_status().json()["run_id"]

        def ready():
            row = client.get(f"/queries/{lexical}").raise_for_status().json()
            return row if row["status"] != "pending" else None

        assert wait_for(ready)["result_kind"] == "evidence"
        assert client.get(f"/queries/{first}").raise_for_status().json()["status"] == "canceled"

    with launch(root, origin, models=assets) as (process, client):
        interrupted = client.post("/queries", json=payload).raise_for_status().json()["run_id"]
        assert client.get(f"/queries/{interrupted}").raise_for_status().json()["status"] == "pending"
        process.kill()
        process.wait(timeout=10)
    with launch(root, origin, models=assets) as (_, client):
        record = client.get(f"/queries/{interrupted}").raise_for_status().json()
        assert (record["status"], record["failure_code"], record["attempts"][-1]["status"]) == ("error", "interrupted", "interrupted")
        assert client.get("/snapshots").raise_for_status().json()["active_snapshot_id"] == snapshot


def stop_extraction_worker(pid):
    for children in Path(f"/proc/{pid}/task").glob("*/children"):
        try:
            child_ids = children.read_text().split()
        except FileNotFoundError:
            continue
        for number in child_ids:
            try:
                child = Path(f"/proc/{number}/cmdline").read_bytes().split(b"\0")
                if b"retrofit.parser_worker" in child and b"extract" in child:
                    status = Path(f"/proc/{number}/status").read_text()
                    if "NoNewPrivs:\t1" not in status or "Seccomp:\t2" not in status:
                        continue
                    os.kill(int(number), signal.SIGSTOP)
                    return int(number)
            except FileNotFoundError:
                continue
    return None


def wait_for_state(client, job_id, expected, *, timeout=10):
    def observe():
        record = client.get(f"/ingestions/{job_id}").raise_for_status().json()
        return record if record["state"] == expected else None

    return wait_for(observe, timeout=timeout)


def publish_baseline(client):
    job = stage(client, "Established baseline fixture.")
    client.post(f"/ingestions/{job}/extract", json={}).raise_for_status()
    extraction = wait_for_state(client, job, "review_required")["extraction"]
    client.post(f"/ingestions/{job}/review", json={
        "schema_version": "1.1", "extraction_config_hash": extraction["config_hash"],
        "approved_span_ids": [extraction["pages"][0]["spans"][0]["span_id"]],
        "qa_note": "Verified the existing snapshot fixture before exercising parser recovery.",
    }).raise_for_status()
    snapshot = client.post("/snapshots", json={"schema_version": "1.1", "job_ids": [job]}).raise_for_status().json()["snapshot_id"]
    client.post(f"/snapshots/{snapshot}/publish", json={}).raise_for_status()
    return snapshot


def test_active_parser_cancel_releases_slot_and_restart_recovers_interruption(tmp_path):
    origin = free_origin()
    root = tmp_path / "private"
    with launch(root, origin) as (process, client):
        active = publish_baseline(client)
        first = stage(client, "Cancel the active process.", pages=150)
        second = stage(client, "Recover after process exit.", pages=150)
        assert client.post(f"/ingestions/{first}/extract", json={}).raise_for_status().json()["state"] == "extracting"
        child = wait_for(lambda: stop_extraction_worker(process.pid))
        try:
            limits = Path(f"/proc/{child}/limits").read_text()
            for name, amount in (
                ("Max cpu time", 8), ("Max file size", 64 * 1024 * 1024),
                ("Max address space", 512 * 1024 * 1024), ("Max core file size", 0),
            ):
                entry = next(line for line in limits.splitlines() if line.startswith(name))
                assert entry.removeprefix(name).split()[:2] == [str(amount), str(amount)]
            busy = client.post(f"/ingestions/{second}/extract", json={})
            assert (busy.status_code, busy.json()["code"]) == (429, "parser_busy")
            assert client.post(f"/ingestions/{first}/cancel", json={}).json()["state"] == "cancel_requested"
            assert wait_for_state(client, first, "canceled")["extraction"] is None
            assert not Path(f"/proc/{child}").exists()
            assert client.get("/snapshots").json()["active_snapshot_id"] == active
        finally:
            if Path(f"/proc/{child}").exists():
                os.kill(child, signal.SIGCONT)
        duplicate = subprocess.run(command(root, free_origin()), capture_output=True, text=True, timeout=15)
        assert duplicate.returncode == 2 and "already open" in duplicate.stderr

    with launch(root, origin) as (process, client):
        assert client.post(f"/ingestions/{second}/extract", json={}).raise_for_status().json()["state"] == "extracting"
        interrupted_child = wait_for(lambda: stop_extraction_worker(process.pid))
        # Exercise an abrupt exit before the worker can commit an extraction artifact.
        process.kill()
        process.wait(timeout=10)
        stale_headers = dict(client.headers)

    with launch(root, origin) as (_, restarted):
        stale = restarted.get("/catalog", headers=stale_headers)
        assert (stale.status_code, stale.json()["code"]) == (401, "session_denied")
        failed = restarted.get(f"/ingestions/{second}").raise_for_status().json()
        assert (failed["state"], failed["failure_code"], failed["extraction"]) == ("failed", "interrupted", None)
        stat = Path(f"/proc/{interrupted_child}/stat")
        def reaped():
            try:
                return stat.read_text().split(") ", 1)[1].startswith("Z ")
            except FileNotFoundError:
                return True

        assert wait_for(reaped)
        assert stage(restarted, "Recover after process exit.", pages=150) == second
        restarted.post(f"/ingestions/{second}/extract", json={}).raise_for_status()

        pages = wait_for_state(restarted, second, "review_required")["extraction"]["pages"]
        assert len(pages) == 150
        assert pages[0]["spans"][0]["normalized_text"] == "Recover after process exit. Page 1."
        assert restarted.get(f"/ingestions/{first}").json()["state"] == "canceled"
        assert stage(restarted, "Cancel the active process.", pages=150) == first
        restarted.post(f"/ingestions/{first}/extract", json={}).raise_for_status()
        assert wait_for_state(restarted, first, "review_required")["extraction"]["page_count"] == 150
        assert restarted.get("/snapshots").json()["active_snapshot_id"] == active


def test_parser_wall_deadline_discards_staging_without_changing_the_active_snapshot(tmp_path):
    with launch(tmp_path / "private", free_origin()) as (process, client):
        active = publish_baseline(client)
        job = stage(client, "Untrusted extraction stopped at the OS boundary.", pages=150)
        client.post(f"/ingestions/{job}/extract", json={}).raise_for_status()
        child = wait_for(lambda: stop_extraction_worker(process.pid))
        try:
            record = wait_for_state(client, job, "failed", timeout=20)
            assert (record["failure_code"], record["extraction"]) == ("parser_wall_limit", None)
            assert not Path(f"/proc/{child}").exists()
            assert client.get("/snapshots").json()["active_snapshot_id"] == active
        finally:
            if Path(f"/proc/{child}").exists():
                os.kill(child, signal.SIGCONT)


def test_process_group_cancellation_discards_real_partial_output_and_stops_descendant(tmp_path):
    # The normal parser's seccomp policy denies fork. This controlled fixture
    # exercises the same supervisor against an actual parent and child process.
    ready = tmp_path / "tree.json"
    fixture = """
import json, os, signal, sys
from pathlib import Path
child = os.fork()
if child == 0:
    while True:
        signal.pause()
os.write(1, b'{"partial":')
path = Path(sys.argv[1])
pending = path.with_suffix(".tmp")
pending.write_text(json.dumps([os.getpid(), child]))
pending.replace(path)
while True:
    signal.pause()
"""
    with pytest.raises(WorkerCanceled):
        supervise_worker(
            [sys.executable, "-c", fixture, os.fspath(ready)], b"",
            deadline=time.monotonic() + 5, canceled=ready.is_file,
        )
    parent, child = json.loads(ready.read_bytes())

    def stopped(pid):
        stat = Path(f"/proc/{pid}/stat")
        try:
            return stat.read_text().split(") ", 1)[1].startswith("Z ")
        except FileNotFoundError:
            return True

    assert wait_for(lambda: stopped(parent) and stopped(child))


def test_supervisor_stops_descendants_even_if_worker_leader_exits():
    fixture = """
import os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(5)
    os._exit(0)
sys.stdout.write(str(pid))
"""
    output = supervise_worker([sys.executable, "-c", fixture], b"", deadline=time.monotonic() + 3, canceled=lambda: False)
    child = int(output)
    stat = Path(f"/proc/{child}/stat")

    def stopped():
        try:
            return stat.read_text().split(") ", 1)[1].startswith("Z ")
        except FileNotFoundError:
            return True

    try:
        assert wait_for(stopped, timeout=0.5)
    finally:
        if not stopped():
            os.kill(child, signal.SIGKILL)


def test_supervisor_rejects_a_successful_worker_that_exceeds_its_deadline():
    with pytest.raises(ApiError, match="parser_wall_limit"):
        supervise_worker(["/usr/bin/sleep", "0.035"], b"", deadline=time.monotonic() + 0.012, canceled=lambda: False)


@pytest.mark.parametrize(("limit", "fixture_budget"), [(resource.RLIMIT_CPU, 1), (resource.RLIMIT_AS, 1024 * 1024)])
def test_kernel_overrun_discards_real_worker_output_and_preserves_published_evidence(tmp_path, limit, fixture_budget):
    with launch(tmp_path / "private", free_origin()) as (process, client):
        active = publish_baseline(client)
        job = stage(client, "Many actual glyphs exercise the reduced fixture resource limit.", pages=150, density=30)
        client.post(f"/ingestions/{job}/extract", json={}).raise_for_status()
        child = wait_for(lambda: stop_extraction_worker(process.pid))
        try:
            previous = resource.prlimit(child, limit, (fixture_budget, fixture_budget))
            assert previous[1] > fixture_budget
            assert resource.prlimit(child, limit) == (fixture_budget, fixture_budget)
        finally:
            os.kill(child, signal.SIGCONT)
        failed = wait_for_state(client, job, "failed")
        assert (failed["failure_code"], failed["extraction"]) == ("parser_rejected_pdf", None)
        assert not Path(f"/proc/{child}").exists()
        assert client.get("/snapshots").json()["active_snapshot_id"] == active


def test_launcher_rejects_nonprivate_store_and_invalid_hosted_policy(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    invalid_store = subprocess.run(command(shared, free_origin()), capture_output=True, text=True, timeout=15)
    assert invalid_store.returncode == 2 and "mode-0700" in invalid_store.stderr
    policy = tmp_path / "untrusted-policy.json"
    policy.write_text("test-credential-must-not-leak")
    invalid_policy = subprocess.run(
        command(tmp_path / "private", free_origin(), "--hosted-policy", policy),
        capture_output=True, text=True, timeout=15,
    )
    assert invalid_policy.returncode == 2 and "hosted policy" in invalid_policy.stderr
    assert "test-credential" not in invalid_policy.stderr
