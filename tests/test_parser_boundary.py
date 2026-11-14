import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from tempfile import TemporaryFile
import time

from coverage import Coverage, CoverageData
import pytest

from pdf_fixture import pdf_with_pages, text_stream
from processes import python_launcher
from retrofit.contracts import PDF_BYTES, PDF_PAGES, quote_geometry


def parser_command(operation, *arguments, pdf=b"", parent=None, host_limit="none"):
    parent_pid = str(os.getpid() if parent is None else parent)
    with TemporaryFile() as trace:
        measurement = Coverage.current()
        command = [sys.executable, "-B", "-m", "retrofit.parser_worker"]
        descriptors = ()
        if measurement is not None or host_limit != "none":
            descriptor = trace.fileno() if measurement is not None else -1
            descriptors = (descriptor,) if descriptor >= 0 else ()
            command = [
                sys.executable, "-B", str(Path(__file__).with_name("measure_parser.py")), str(descriptor), host_limit,
            ]
        result = subprocess.run(
            [*command, parent_pid, operation, *map(str, arguments)],
            input=pdf, capture_output=True, timeout=18, check=False,
            start_new_session=True, pass_fds=descriptors,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        if measurement is not None:
            trace.seek(0)
            data = CoverageData(no_disk=True)
            serialized = trace.read()
            assert serialized, result.stderr.decode(errors="replace")
            data.loads(serialized)
            measurement.get_data().update(data)
        return result


def test_parser_sandbox_and_admitted_page_geometry():
    probe = parser_command("probe")
    assert probe.returncode == 0 and probe.stderr == b""
    profile = json.loads(probe.stdout)
    assert profile["enabled"] and profile["network_denied"] and profile["child_creation_denied"]
    assert (profile["cpu_seconds"], profile["memory_bytes"], profile["wall_seconds"], profile["output_bytes"]) == (
        8, 512 * 1024 * 1024, 15, 64 * 1024 * 1024,
    )
    heading = text_stream("Recurring header", y=780)
    pdf = pdf_with_pages(
        heading + text_stream("5.2 Lighting upgrades", y=735) + text_stream("Keep this evidence.", y=685),
        heading + text_stream("Retain this separate detail.") + text_stream("Unique footer", y=10),
    )
    result = parser_command("extract", "[1,2]", pdf=pdf)
    assert result.returncode == 0 and result.stderr == b""
    artifact = json.loads(result.stdout)
    assert artifact["page_count"] == 2
    for page in artifact["pages"]:
        assert [span["normalized_text"] for span in page["spans"] if span.get("excluded_reason")] == ["Recurring header"]
    body = next(span for span in artifact["pages"][0]["spans"] if span["normalized_text"] == "Keep this evidence.")
    assert body["section_path"] == ["5.2 Lighting upgrades"]
    assert body["origin"] == "top-left" and body["bounding_boxes"][0][1] > 90
    assert "".join(" " if i is None else body["raw_text"][i] for i in body["normalization_map"]) == "Keep this evidence."
    assert quote_geometry(body, 0, len(body["normalized_text"]), body["normalized_text"]) == body["bounding_boxes"]
    render = parser_command("render", 2, pdf=pdf)
    assert render.returncode == 0 and render.stderr == b"" and render.stdout.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize(("operation", "argument", "pdf", "parent"), [
    ("extract", "[1]", b"%PDF-Broken with confidential input", None),
    ("extract", "[1]", b"a" * (PDF_BYTES + 1), None),
    ("unknown", "1", b"", None),
    ("probe", "", b"", 1),
], ids=["malformed-pdf", "byte-limit", "unknown-operation", "wrong-parent"])
def test_parser_rejects_boundary_faults_without_emitting_input(operation, argument, pdf, parent):
    result = parser_command(operation, argument, pdf=pdf, parent=parent)
    assert (result.returncode, result.stdout, result.stderr) == (2, b"", b"")


def test_parser_rejects_outside_document_and_page_count_overflow():
    one_page = pdf_with_pages(text_stream("Evidence."))
    missing = parser_command("extract", "[2]", pdf=one_page)
    assert missing.returncode == 0 and json.loads(missing.stdout) == {"error": "coverage_page_missing"}
    over_limit = parser_command("extract", "[1]", pdf=pdf_with_pages(*([""] * (PDF_PAGES + 1))))
    assert over_limit.returncode == 0 and json.loads(over_limit.stdout) == {"error": "pdf_page_limit"}
    duplicate = parser_command("extract", "[1,1]", pdf=one_page)
    assert duplicate.returncode == 0 and json.loads(duplicate.stdout) == {"error": "unsupported_pdf"}


def test_sidebar_gap_is_quarantined_and_cannot_become_a_section_heading():
    page = (
        text_stream("5.7 Sidebar", x=50, y=720) + text_stream("Controls", x=132, y=720)
        + text_stream("Clean body evidence.", y=660)
    )
    result = parser_command("extract", "[1]", pdf=pdf_with_pages(page))
    assert result.returncode == 0 and result.stderr == b""
    spans = json.loads(result.stdout)["pages"][0]["spans"]
    joined, body = spans
    assert joined["normalized_text"] == "5.7 Sidebar Controls" and joined["requires_review"]
    assert body["normalized_text"] == "Clean body evidence." and not body["requires_review"]
    assert body["section_path"] == []


def test_grid_candidates_keep_columns_and_blank_pages_do_not_create_evidence():
    grid = (
        "50 580 260 120 re S\n50 640 m 310 640 l S\n180 580 m 180 700 l S\n"
        + text_stream("School", x=60, y=668) + text_stream("Savings", x=190, y=668)
        + text_stream("Miami", x=60, y=610) + text_stream("22.2%", x=190, y=610)
        + text_stream("Units", x=254, y=610) + text_stream("Footnote stays outside the grid.", y=530)
        + "BT /F1 10 Tf 0 1 -1 0 35 480 Tm (Rotated margin) Tj ET\n"
    )
    result = parser_command("extract", "[1,2]", pdf=pdf_with_pages(grid, ""))
    assert result.returncode == 0 and result.stderr == b""
    first, blank = json.loads(result.stdout)["pages"]
    assert blank["spans"] == [] and blank["table_candidates"] == []
    cells = {
        span["normalized_text"]: (span["candidate_row"], span["candidate_column"])
        for span in first["spans"] if span.get("table_candidate") and span["requires_review"]
    }
    assert cells == {"School": (1, 1), "Savings": (1, 2), "Miami": (2, 1), "22.2%": (2, 2), "Units": (2, 2)}
    assert first["quarantined_lines"] >= 1
    assert any(span["normalized_text"] == "Footnote stays outside the grid." and not span["requires_review"] for span in first["spans"])


def test_irregular_table_ignores_missing_and_off_page_cells():
    grid = (
        "-80 580 390 120 re S\n-80 640 m 310 640 l S\n-30 580 m -30 700 l S\n100 580 m 100 700 l S\n180 580 m 180 640 l S\n"
        + text_stream("Merged header", x=122, y=668)
        + text_stream("22.2%", x=110, y=611)
        + text_stream("Visible edge", x=10, y=611)
        + "BT /F1 10 Tf 0 1 -1 0 220 588 Tm (Rotated cell) Tj ET\n"
    )
    result = parser_command("extract", "[1]", pdf=pdf_with_pages(grid))
    assert result.returncode == 0 and result.stderr == b""
    page = json.loads(result.stdout)["pages"][0]
    cells = [span["normalized_text"] for span in page["spans"] if span.get("table_candidate")]
    assert cells == ["Merged header", "Visible edge", "22.2%"]
    assert page["quarantined_lines"] >= 1


@pytest.mark.parametrize(("fixture", "error"), [
    ("invalid-action", "Cannot initialize seccomp"),
    ("unknown-syscall", "Cannot install seccomp rule not_a_real_syscall"),
])
def test_libseccomp_rejects_invalid_policy_before_worker_can_continue(fixture, error):
    harness = Path(__file__).with_name("parser_kernel_probe.py")
    result = subprocess.run([*python_launcher(), str(harness), fixture, str(os.getpid())],
                            text=True, capture_output=True, timeout=10)
    assert (result.returncode, result.stdout.strip(), result.stderr) == (0, error, "")


def test_kernel_enforces_parser_address_space_and_cpu_budgets():
    harness = Path(__file__).with_name("parser_kernel_probe.py")
    memory = subprocess.run([sys.executable, str(harness), "address-space", str(os.getpid())],
                            capture_output=True, timeout=10, check=False)
    assert memory.returncode == 0, memory.stderr
    assert memory.stdout == b"below-limit mapping succeeded; over-limit mapping denied\n"
    with subprocess.Popen([sys.executable, str(harness), "cpu", str(os.getpid())], stdout=subprocess.PIPE) as worker:
        try:
            assert select.select([worker.stdout], [], [], 5)[0], "The CPU fixture did not start."
            assert worker.stdout.readline() == b"running under parser CPU limit\n"
            deadline = time.monotonic() + 18
            while time.monotonic() < deadline:
                pid, status, usage = os.wait4(worker.pid, os.WNOHANG)
                if pid:
                    worker.returncode = os.waitstatus_to_exitcode(status)
                    break
                time.sleep(0.05)
            assert worker.returncode is not None, "The parser's kernel CPU limit did not terminate the fixture."
        finally:
            if worker.returncode is None:
                worker.kill()
                worker.wait(timeout=5)
    assert worker.returncode == -signal.SIGKILL
    cpu_seconds = usage.ru_utime + usage.ru_stime
    assert 7.5 <= cpu_seconds <= 9, f"Kernel CPU enforcement observed at {cpu_seconds:.3f} seconds"


@pytest.mark.parametrize("host_limit", ["parent-death", "no-new-privileges", "install-filter"])
def test_worker_refuses_to_process_any_pdf_when_kernel_rejects_required_controls(host_limit):
    source = pdf_with_pages(text_stream("Should not be processed without isolation."))
    rejected = parser_command("extract", "[1]", pdf=source, host_limit=host_limit)
    assert (rejected.returncode, rejected.stdout, rejected.stderr) == (2, b"", b"")


def test_parser_self_check_rejects_a_process_with_socket_and_fork_access(tmp_path):
    script = Path(__file__).with_name("probe_unconfined.py")
    measurement = Coverage.current()
    report = tmp_path / "probe"
    args = [str(report)] if measurement is not None else []
    process = subprocess.run([sys.executable, str(script), *args], capture_output=True, timeout=10, check=False)
    assert process.returncode == 0, process.stderr
    profile = json.loads(process.stdout)
    assert (profile["enabled"], profile["network_denied"], profile["child_creation_denied"]) == (False, False, False)
    if measurement is not None:
        recorded = list(tmp_path.glob("probe.*"))
        assert len(recorded) == 2
        for artifact in recorded:
            data = CoverageData(basename=str(artifact))
            data.read()
            measurement.get_data().update(data)
