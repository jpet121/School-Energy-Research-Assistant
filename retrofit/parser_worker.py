"""Bounded Linux worker. Reads PDF bytes on stdin and emits an artifact on stdout."""

import codecs
import ctypes
from importlib.metadata import version
from io import BytesIO
import json
import os
import re
import resource
import signal
import socket
from statistics import median
import sys

from PIL import Image
import pdfplumber
import pypdfium2

from .contracts import PDF_BYTES, PDF_PAGES, canonical


CPU_SECONDS = 8
MEMORY_BYTES = 512 * 1024 * 1024
WALL_SECONDS = 15
OUTPUT_BYTES = 64 * 1024 * 1024
BODY_COLUMN_GAP_PT = 18
PARSER_VERSION = version("pdfplumber")
ALLOWED = (
    "read", "write", "readv", "writev", "pread64", "lseek", "fstat", "newfstatat", "statx", "close",
    "brk", "mmap", "mremap", "munmap", "mprotect", "madvise",
    "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack",
    "clock_gettime", "clock_getres", "gettimeofday", "clock_nanosleep", "getitimer",
    "getpid", "getppid", "gettid", "getuid", "geteuid", "getgid", "getegid",
    "getrandom", "getrusage", "sched_getaffinity", "sched_yield", "sysinfo",
    "futex", "restart_syscall", "exit", "exit_group",
)


def isolate(parent: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
        raise RuntimeError("Worker parent died")
    if libc.prctl(38, 1, 0, 0, 0):
        raise RuntimeError("Cannot set no_new_privs")
    for limit, value in (
        (resource.RLIMIT_CPU, CPU_SECONDS), (resource.RLIMIT_AS, MEMORY_BYTES),
        (resource.RLIMIT_FSIZE, OUTPUT_BYTES), (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(limit, (value, value))
    signal.setitimer(signal.ITIMER_REAL, WALL_SECONDS)
    install_filter(0x00050001, ALLOWED)  # EPERM for unlisted system calls


def install_filter(default_action: int, allowed: tuple[str, ...]) -> None:
    """Install a kernel allowlist, failing closed if libseccomp rejects its policy."""
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = seccomp.seccomp_init(default_action)
    if not ctx:
        raise RuntimeError("Cannot initialize seccomp")
    try:
        for name in allowed:
            syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
            if syscall < 0 or seccomp.seccomp_rule_add(ctx, 0x7FFF0000, syscall, 0):
                raise RuntimeError(f"Cannot install seccomp rule {name}")
        if seccomp.seccomp_load(ctx):
            raise RuntimeError("Cannot load seccomp filter")
    finally:
        seccomp.seccomp_release(ctx)


def probe(limits: dict[str, int]) -> dict:
    try:
        socket.socket().close()
        network_denied = False
    except PermissionError:
        network_denied = True
    try:
        pid = os.fork()
        if not pid:
            os._exit(0)
        os.waitpid(pid, 0)
        child_denied = False
    except PermissionError:
        child_denied = True
    return {
        "enabled": network_denied and child_denied,
        "network_denied": network_denied,
        "child_creation_denied": child_denied,
        **limits,
        "sandbox": "Linux seccomp allowlist, no_new_privs, dedicated process group and resource limits",
        "parser": "pdfplumber",
        "parser_version": PARSER_VERSION,
        "coordinate_convention": "PDF points, top-left of the rendered page, rotation recorded separately",
        "normalization_version": "5",
        "body_column_gap_pt": BODY_COLUMN_GAP_PT,
        "furniture_geometry_pt": 28,
    }


def line_span(line: dict, number: int, rotation: int, printed: str | None) -> dict | None:
    chars = sorted((char for char in line["chars"] if char["text"]), key=lambda char: (char["x0"], char["top"]))
    if not chars or any(not char.get("upright", True) for char in chars):
        return None
    raw = "".join(char["text"] for char in chars)
    if not raw.strip() or len(raw) > 8_192:
        return None
    indices: list[int | None] = []
    normalized_chars: list[str] = []
    boxes = []
    offset = 0
    previous = None
    for char in chars:
        value = char["text"]
        if (
            previous and not previous["text"][-1].isspace() and not value[0].isspace()
            and char["x0"] - previous["x1"] > max(1.4, 0.20 * char.get("size", 8))
        ):
            normalized_chars.append(" ")
            indices.append(None)
        box = [round(char[key], 3) for key in ("x0", "top", "x1", "bottom")]
        boxes.extend([box] * len(value))
        normalized_chars.append(value)
        indices.extend(range(offset, offset + len(value)))
        offset += len(value)
        previous = char
    normalized = "".join(normalized_chars)
    if len(normalized) > 8_192:
        return None
    ambiguous = any(
        right["x0"] - left["x1"] > BODY_COLUMN_GAP_PT for left, right in zip(chars, chars[1:])
    )
    return {
        "pdf_page_1based": number, "printed_page_label": printed,
        "rotation": rotation, "origin": "top-left", "raw_text": raw, "normalized_text": normalized,
        "bounding_boxes": [[
            round(min(char["x0"] for char in chars), 3), round(min(char["top"] for char in chars), 3),
            round(max(char["x1"] for char in chars), 3), round(max(char["bottom"] for char in chars), 3),
        ]],
        "raw_character_boxes": boxes, "normalization_map": indices, "requires_review": ambiguous,
        "max_font_size": max((char.get("size", 0) for char in chars), default=0),
    }


def separate_cell_groups(line: dict) -> list[dict]:
    groups: list[list[dict]] = []
    for char in sorted(line["chars"], key=lambda item: (item["x0"], item["top"])):
        if not groups or char["x0"] - max(item["x1"] for item in groups[-1]) > 12:
            groups.append([])
        groups[-1].append(char)
    return [{"chars": chars} for chars in groups]


def extract(data: bytes, included_pages: list[int]) -> dict:
    pages = []
    with pdfplumber.open(BytesIO(data), pages=included_pages) as document, pypdfium2.PdfDocument(data) as rendered:
        page_count = len(rendered)
        if page_count > PDF_PAGES:
            return {"error": "pdf_page_limit"}
        if any(number > page_count for number in included_pages):
            return {"error": "coverage_page_missing"}
        if not document.pages or len(document.pages) != len(included_pages):
            return {"error": "unsupported_pdf"}
        section_path: list[str] = []
        for original in document.pages:
            page = original.dedupe_chars(tolerance=0.5)
            printed = rendered.get_page_label(page.page_number - 1)
            tables = page.find_tables()
            body = page.filter(lambda obj: obj["object_type"] != "char" or not any(
                box[0] <= (obj["x0"] + obj["x1"]) / 2 <= box[2]
                and box[1] <= (obj["top"] + obj["bottom"]) / 2 <= box[3]
                for box in (table.bbox for table in tables)
            ))
            lines = body.extract_text_lines(return_chars=True)
            spans = [line_span(line, page.page_number, page.rotation, printed or None) for line in lines]
            font_sizes = [span["max_font_size"] for span in spans if span is not None]
            typical_font = median(font_sizes) if font_sizes else 0
            for span in spans:
                if span is None:
                    continue
                text = span["normalized_text"].strip()
                if not span["requires_review"] and len(text) <= 120 and (
                    re.match(r"^\d+(?:\.\d+){1,4}\s+[A-Za-z]", text) or span["max_font_size"] >= typical_font + 3
                ):
                    section_path = [text]
                span["section_path"] = section_path
            candidates = []
            for table_number, table in enumerate(tables, 1):
                start = len(spans)
                for row_number, row in enumerate(table.rows, 1):
                    for col_number, cell in enumerate(row.cells, 1):
                        if not cell:
                            continue
                        try:
                            left, top, right, bottom = page.bbox
                            cropped = page.crop((
                                max(left, cell[0]), max(top, cell[1]), min(right, cell[2]), min(bottom, cell[3]),
                            ))
                            cell_lines = cropped.extract_text_lines(return_chars=True)
                        except ValueError:
                            continue
                        for line in cell_lines:
                            for part in separate_cell_groups(line):
                                span = line_span(part, page.page_number, page.rotation, printed or None)
                                if span:
                                    span.update(
                                        table_candidate=table_number, candidate_row=row_number, candidate_column=col_number,
                                        requires_review=True, section_path=section_path,
                                    )
                                spans.append(span)
                candidates.append({"table": table_number, "bounding_box": list(table.bbox), "candidate_spans": len(spans) - start})
            for span in spans:
                if span is not None:
                    span.update({"page_width": page.width, "page_height": page.height})
            pages.append({
                "pdf_page_1based": page.page_number,
                "printed_page_label": printed or None,
                "width": page.width,
                "height": page.height,
                "rotation": page.rotation,
                "spans": [span for span in spans if span is not None],
                "quarantined_lines": sum(span is None for span in spans),
                "table_candidates": candidates,
            })
            original.close()
    furniture: dict[str, set[int]] = {}
    for page_data in pages:
        for recorded_span in page_data["spans"]:
            _, top, _, bottom = recorded_span["bounding_boxes"][0]
            if not recorded_span.get("table_candidate") and (top < 28 or bottom > page_data["height"] - 28):
                furniture.setdefault(recorded_span["normalized_text"], set()).add(page_data["pdf_page_1based"])
    for page_data in pages:
        for recorded_span in page_data["spans"]:
            _, top, _, bottom = recorded_span["bounding_boxes"][0]
            if len(furniture.get(recorded_span["normalized_text"], ())) > 1 and (top < 28 or bottom > page_data["height"] - 28):
                recorded_span["excluded_reason"] = "repeated page furniture in recorded margin geometry"
    return {"page_count": page_count, "pages": pages}


def render(data: bytes, page_number: int) -> None:
    with pypdfium2.PdfDocument(data) as document:
        page = document[page_number - 1]
        scale = min(1.5, 1800 / max(page.get_size()))
        bitmap = page.render(scale=scale)
        bitmap.to_pil().save(sys.stdout.buffer, format="PNG")


def main() -> None:
    parent, operation, *arguments = sys.argv[1:]
    for encoding in ("latin-1", "utf-16-le", "utf-16-be", "utf-16", "cp1252"):
        codecs.lookup(encoding)
    if operation == "render":
        Image.preinit()
    limits = {
        "cpu_seconds": CPU_SECONDS, "memory_bytes": MEMORY_BYTES, "output_bytes": OUTPUT_BYTES,
        "wall_seconds": WALL_SECONDS,
    }
    isolate(int(parent))
    if operation == "probe":
        print(canonical(probe(limits)))
        return
    data = sys.stdin.buffer.read(PDF_BYTES + 1)
    if len(data) > PDF_BYTES:
        raise RuntimeError("Unbounded PDF input")
    if operation == "extract":
        print(canonical(extract(data, json.loads(arguments[0]))))
    elif operation == "render":
        render(data, int(arguments[0]))
    else:
        raise RuntimeError("Unknown worker operation")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # PDF contents and exception payloads never become application logs.
        sys.exit(2)
