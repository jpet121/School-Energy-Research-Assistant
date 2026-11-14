from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryFile
from time import monotonic, sleep
from typing import Annotated, Any, Literal, NotRequired

from pydantic import Field, FiniteFloat, TypeAdapter
from typing_extensions import TypedDict

from .contracts import ApiError, PDF_PAGES, box_within_page, canonical, fingerprint
from .store import Store


class WorkerCanceled(Exception):
    pass


def require_uncanceled(canceled: Callable[[], bool]) -> None:
    if canceled():
        raise WorkerCanceled()


def time_remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ApiError(422, "parser_wall_limit", "Extraction exceeded the recorded wall-time limit.")
    return remaining


Box = Annotated[list[FiniteFloat], Field(min_length=4, max_length=4)]
Dimension = Annotated[FiniteFloat, Field(gt=0)]


class WorkerSpan(TypedDict):
    pdf_page_1based: int
    printed_page_label: str | None
    rotation: int
    origin: Literal["top-left"]
    raw_text: str
    normalized_text: str
    bounding_boxes: Annotated[list[Box], Field(min_length=1)]
    raw_character_boxes: list[Box]
    normalization_map: list[int | None]
    requires_review: bool
    page_width: Dimension
    page_height: Dimension
    section_path: list[str]
    table_candidate: NotRequired[int]
    candidate_row: NotRequired[int]
    candidate_column: NotRequired[int]
    excluded_reason: NotRequired[str]


class WorkerPage(TypedDict):
    pdf_page_1based: int
    printed_page_label: str | None
    width: Dimension
    height: Dimension
    rotation: int
    spans: list[WorkerSpan]
    quarantined_lines: int
    table_candidates: list[dict]


class WorkerArtifact(TypedDict):
    page_count: Annotated[int, Field(ge=1, le=PDF_PAGES)]
    pages: list[WorkerPage]


WORKER_ARTIFACT: TypeAdapter[WorkerArtifact] = TypeAdapter(WorkerArtifact)
WORKER_ERRORS = {"coverage_page_missing", "pdf_page_limit", "unsupported_pdf"}


def read_worker_artifact(raw: bytes, included: list[int]) -> dict:
    """Validate the bounded worker protocol before any artifact enters staging."""
    try:
        output = json.loads(raw)
        if "error" in output:
            failure = output["error"]
            if failure in WORKER_ERRORS:
                raise ApiError(422, failure, "The parser could not extract the selected PDF pages.")
            raise ValueError("Unexpected parser error code")
        WORKER_ARTIFACT.validate_python(output, strict=True)
        if output["page_count"] < included[-1] or [page["pdf_page_1based"] for page in output["pages"]] != included:
            raise ValueError("Unexpected extracted page range")
        for page in output["pages"]:
            for span in page["spans"]:
                if (
                    span["pdf_page_1based"] != page["pdf_page_1based"]
                    or span["page_width"] != page["width"] or span["page_height"] != page["height"]
                    or len(span["normalization_map"]) != len(span["normalized_text"])
                    or len(span["raw_character_boxes"]) != len(span["raw_text"])
                ):
                    raise ValueError("Inconsistent span geometry")
                if not all(box_within_page(box, page["width"], page["height"]) for box in span["bounding_boxes"]):
                    span["excluded_reason"] = "source span is outside the rendered page"
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(422, "parser_artifact_invalid", "The parser returned an invalid extraction artifact.") from exc
    return output


def worker(
    operation: str,
    pdf: bytes = b"",
    *,
    arguments: tuple[str, ...] = (),
    deadline: float,
    canceled: Callable[[], bool] = lambda: False,
) -> bytes:
    command = [sys.executable, "-B", "-m", "retrofit.parser_worker", str(os.getpid()), operation, *arguments]
    return supervise_worker(command, pdf, deadline=deadline, canceled=canceled)


def supervise_worker(command: list[str], pdf: bytes, *, deadline: float, canceled: Callable[[], bool]) -> bytes:
    with TemporaryFile() as source, TemporaryFile() as output:
        source.write(pdf)
        source.seek(0)
        process = subprocess.Popen(
            command,
            stdin=source, stdout=output, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
            cwd=Path(__file__).resolve().parent.parent,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        try:
            while True:
                require_uncanceled(canceled)
                remaining = time_remaining(deadline)
                # Keep the leader PID reserved until the entire group has been
                # stopped. Reaping it first could free the process-group ID.
                ended = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if ended is not None:
                    time_remaining(deadline)
                    if ended.si_code != os.CLD_EXITED or ended.si_status != 0:
                        raise ApiError(422, "parser_rejected_pdf", "The bounded parser could not safely process this PDF.")
                    break
                sleep(min(0.05, remaining))
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output.seek(0)
        return output.read()


class Extraction:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retrofit-parser")
        try:
            self.profile = json.loads(worker("probe", deadline=monotonic() + 15))
        except (OSError, ApiError, ValueError, subprocess.SubprocessError):
            self.profile = {"enabled": False, "reason": "The parser isolation self-check failed on this host."}
        with store.connection(write=True) as db:
            db.execute("UPDATE ingestions SET state='failed',failure_code='interrupted' WHERE state='extracting'")
            db.execute("UPDATE ingestions SET state='canceled' WHERE state='cancel_requested'")

    def require_isolation(self) -> None:
        if not self.profile["enabled"]:
            raise ApiError(503, "parser_isolation_unavailable", "Extraction requires a passing local parser isolation self-check.")

    def start(self, job_id: str) -> dict[str, str]:
        self.require_isolation()
        current = self.store.ingestion(job_id)
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT state,extraction FROM ingestions WHERE id=?", (job_id,)).fetchone()
            state = row["state"]
            changed_profile = state in {"ready", "review_required"} and (
                json.loads(row["extraction"])["config_hash"] != fingerprint(self.profile)
            )
            if state not in {"staged", "failed", "canceled"} and not changed_profile:
                return {"job_id": job_id, "state": state}
            if db.execute("SELECT 1 FROM ingestions WHERE state IN ('extracting','cancel_requested')").fetchone():
                raise ApiError(429, "parser_busy", "An extraction is already active. Wait or cancel it.")
            db.execute("UPDATE ingestions SET state='extracting',extraction=NULL,failure_code=NULL WHERE id=?", (job_id,))
        self.pool.submit(self._extract, job_id, current["document"])
        return {"job_id": job_id, "state": "extracting"}

    def cancel(self, job_id: str) -> dict[str, str]:
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT state FROM ingestions WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ApiError(404, "missing_ingestion", "That staged source could not be found.")
            if row["state"] == "staged":
                db.execute("UPDATE ingestions SET state='canceled' WHERE id=?", (job_id,))
                state = "canceled"
            elif row["state"] == "extracting":
                db.execute("UPDATE ingestions SET state='cancel_requested' WHERE id=?", (job_id,))
                state = "cancel_requested"
            elif row["state"] in {"cancel_requested", "canceled"}:
                state = row["state"]
            else:
                state = "already_completed"
        return {"operation_id": job_id, "state": state}

    def _canceled(self, job_id: str) -> bool:
        with self.store.connection() as db:
            row = db.execute("SELECT state FROM ingestions WHERE id=?", (job_id,)).fetchone()
        return row["state"] == "cancel_requested"

    def _extract(self, job_id: str, document: dict[str, Any]) -> None:
        serialized = None
        failure = None
        try:
            def canceled() -> bool:
                return self._canceled(job_id)

            require_uncanceled(canceled)
            data = self.store.original(document)
            included = sorted(
                page for region in document["coverage_regions"]
                for page in range(region["first_page"], region["last_page"] + 1)
            )
            output = read_worker_artifact(worker(
                "extract", data, deadline=monotonic() + self.profile["wall_seconds"],
                arguments=(canonical(included),),
                canceled=canceled,
            ), included)
            config_hash = fingerprint(self.profile)
            output.update({"profile": self.profile, "config_hash": config_hash})
            for page in output["pages"]:
                page["within_use_scope"] = True
                for index, span in enumerate(page["spans"]):
                    span.update({
                        "span_id": fingerprint([document["version_id"], config_hash, page["pdf_page_1based"], index]),
                        "document_version_id": document["version_id"],
                        "extraction_config_hash": config_hash,
                    })
            serialized = canonical(output)
        except WorkerCanceled:
            pass
        except (ApiError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            failure = exc.code if isinstance(exc, ApiError) else "parser_artifact_invalid"
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT state FROM ingestions WHERE id=?", (job_id,)).fetchone()
            if row["state"] == "cancel_requested":
                db.execute("UPDATE ingestions SET state='canceled',extraction=NULL WHERE id=?", (job_id,))
            else:
                state = "review_required" if serialized is not None else "failed"
                db.execute(
                    "UPDATE ingestions SET state=?,extraction=?,failure_code=? WHERE id=? AND state='extracting'",
                    (state, serialized, failure, job_id),
                )

    def render(self, document: dict[str, Any], page: int) -> bytes:
        self.require_isolation()
        return worker(
            "render", self.store.original(document), arguments=(str(page),),
            deadline=monotonic() + self.profile["wall_seconds"],
        )
