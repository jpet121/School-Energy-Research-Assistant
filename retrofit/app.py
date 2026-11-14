from html import escape
import logging
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from .briefs import Briefs, brief_routes
from .contracts import Admission, ApiError, PDF_BYTES
from .controls import Controls, control_routes
from .corpus import Corpus, corpus_routes
from .embeddings import Embeddings
from .extraction import Extraction
from .generation import DEADLINE_SECONDS, Generation, generation_routes
from .hosted_policy import PaidAuthority
from .openai_provider import OpenAIProvider
from .research import Research, research_routes
from .reports import report_routes
from .store import Store


STATIC = Path(__file__).parent / "static"
LOGGER = logging.getLogger("uvicorn.error")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def safe_error(status: int, code: str, message: str) -> JSONResponse:
    correlation_id = uuid4().hex
    LOGGER.warning("Local request error code=%s correlation_id=%s", code, correlation_id)
    return JSONResponse(
        {"code": code, "message": message, "correlation_id": correlation_id},
        status_code=status,
        headers=SECURITY_HEADERS,
    )


class LocalBoundary:
    def __init__(self, app: ASGIApp, *, origin: str, token: str) -> None:
        self.app = app
        self.origin = origin
        self.host = urlsplit(origin).netloc
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                controlled = {name.lower().encode() for name in SECURITY_HEADERS}
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() not in controlled]
                headers.extend((key.lower().encode(), value.encode()) for key, value in SECURITY_HEADERS.items())
                message["headers"] = headers
            await send(message)

        try:
            error = self.authorize(Request(scope))
            if error is not None:
                await error(scope, receive, send)
                return
            await self.app(scope, receive, secure_send)
        except Exception:
            if not started:
                error = safe_error(500, "local_failure", "An unexpected local operation failed. Your saved work remains available.")
                await error(scope, receive, send)
            else:
                # Avoid sending application or provider exception details into generic server logs.
                return

    def authorize(self, request: Request) -> JSONResponse | None:
        headers = request.headers
        if (
            request.client is None or request.client.host != "127.0.0.1"
            or headers.get("host") != self.host
            or headers.get("origin", self.origin) != self.origin
            or headers.get("sec-fetch-site") in {"cross-site", "same-site"}
        ):
            return safe_error(403, "origin_denied", "Open the workbench from its configured loopback address.")
        public = request.method in {"GET", "HEAD"} and (
            request.url.path == "/" or request.url.path.startswith("/assets/")
        )
        if not public and not secrets.compare_digest(headers.get("x-session-token", "").encode(), self.token):
            return safe_error(401, "session_denied", "This local session expired. Reload the workbench to reconnect.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if headers.get("origin") != self.origin:
                return safe_error(403, "origin_denied", "Use the configured workbench origin for local changes.")
            length = headers.get("content-length")
            if headers.get("transfer-encoding") or length is None or not length.isascii() or not length.isdecimal():
                return safe_error(400, "unbounded_request", "The request must declare a bounded body length.")
            limit = PDF_BYTES + 128 * 1024 if request.url.path == "/ingestions" else 2 * 1024 * 1024
            significant_length = length.lstrip("0")
            if len(significant_length) > len(str(limit)) or int(significant_length or "0") > limit:
                return safe_error(413, "request_too_large", "The request exceeds the local size limit.")
        return None


def create_app(
    root: Path, *, origin: str, token: str | None = None, models: Path | None = None,
    provider: OpenAIProvider | None = None, paid_authority: PaidAuthority | None = None,
    generation_deadline: float = DEADLINE_SECONDS,
) -> FastAPI:
    address = urlsplit(origin)
    if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port or origin != f"http://127.0.0.1:{address.port}":
        raise ValueError("The workbench requires an explicit IPv4 loopback origin")
    session = token or secrets.token_urlsafe(32)
    store = Store(root)
    extraction = Extraction(store)
    embeddings = Embeddings(models)
    corpus = Corpus(store, embeddings)
    research = Research(store, extraction, embeddings)
    generation = Generation(store, research, session, provider=provider, authority=paid_authority, deadline_seconds=generation_deadline)
    briefs = Briefs(store, research, generation)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.store = store
    app.add_middleware(LocalBoundary, origin=origin, token=session)
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    app.include_router(corpus_routes(corpus))
    app.include_router(research_routes(research))
    app.include_router(brief_routes(briefs))
    app.include_router(control_routes(Controls(store)))
    app.include_router(generation_routes(generation))
    app.include_router(report_routes(store, generation))

    @app.exception_handler(ApiError)
    async def domain_failure(_: Request, error: ApiError) -> JSONResponse:
        return safe_error(error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def request_failure(_: Request, error: RequestValidationError) -> JSONResponse:
        return safe_error(400, "invalid_contract", "Check the required fields and contract version.")

    @app.exception_handler(HTTPException)
    async def http_failure(_: Request, error: HTTPException) -> JSONResponse:
        return safe_error(error.status_code, "request_failed", "That local request could not be completed.")

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8").replace("SESSION_TOKEN", escape(session, quote=True))

    @app.get("/catalog")
    def catalog() -> dict:
        return {"ingestions": store.catalog()}

    @app.post("/ingestions", status_code=201)
    async def stage(request: Request) -> dict[str, str]:
        async with request.form(max_files=1, max_fields=2, max_part_size=64 * 1024) as form:
            manifest, key, file = form.get("manifest"), form.get("idempotency_key"), form.get("pdf")
            if len(form) != 3 or not isinstance(file, UploadFile) or not isinstance(manifest, str) or not isinstance(key, str) or not 1 <= len(key.strip()) <= 128:
                raise ApiError(400, "invalid_intake", "Provide a local PDF, its admission manifest, and an intake key.")
            try:
                admission = Admission.model_validate_json(manifest)
            except ValidationError as exc:
                raise ApiError(
                    400, "invalid_manifest",
                    "Provide the source URL, publisher, date or unknown reason, research scope, public status, and covered pages with a use basis.",
                ) from exc
            pdf = await file.read(PDF_BYTES + 1)
        if len(pdf) > PDF_BYTES:
            raise ApiError(413, "pdf_too_large", "A local PDF may contain at most 30 MiB.")
        if not pdf.startswith(b"%PDF-"):
            raise ApiError(422, "unsupported_pdf", "Select a PDF file with a valid PDF header.")
        return {"job_id": store.stage(admission, pdf, key.strip())}

    @app.get("/ingestions/{job_id}")
    def ingestion(job_id: str) -> dict:
        return store.ingestion(job_id)

    @app.get("/parser")
    def parser_profile() -> dict:
        return extraction.profile

    @app.post("/ingestions/{job_id}/extract", status_code=202)
    def start_extraction(job_id: str) -> dict:
        return extraction.start(job_id)

    @app.post("/ingestions/{job_id}/cancel")
    def cancel_extraction(job_id: str) -> dict:
        return extraction.cancel(job_id)

    @app.get("/ingestions/{job_id}/pages/{page}")
    def staged_page(job_id: str, page: int) -> Response:
        record = store.ingestion(job_id)
        output = record["extraction"]
        if not output or not any(
            item["pdf_page_1based"] == page and item["within_use_scope"] for item in output["pages"]
        ):
            raise ApiError(404, "missing_page", "That staged page is unavailable.")
        return Response(extraction.render(record["document"], page), media_type="image/png")

    @app.get("/editions/{version_id}/original")
    def original(version_id: str) -> Response:
        return Response(
            store.original(store.document(version_id)),
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=source-edition.pdf"},
        )

    return app
