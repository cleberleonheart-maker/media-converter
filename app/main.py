from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings, settings
from .jobs import Job, JobCapacityError, JobService, JobStatus, JobStore
from .media import FORMATS, OutputFormat, Quality, sanitize_filename
from .url_validation import UrlValidationError, http_error_for_url, validate_media_url_async

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class RequestBodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="O corpo da requisição excede o limite permitido.",
        )


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int,
        url_max_body_bytes: int = 64 * 1024,
        can_accept_job: Callable[[], bool] | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.url_max_body_bytes = url_max_body_bytes
        self.can_accept_job = can_accept_job

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :] or "/"
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        if (
            path in {"/api/jobs/upload", "/api/jobs/url"}
            and self.can_accept_job is not None
            and not self.can_accept_job()
        ):
            await JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "O limite de conversões foi atingido."},
            )(scope, receive, send)
            return

        body_limit = self.url_max_body_bytes if path == "/api/jobs/url" else self.max_body_bytes
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        content_length = headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                await JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Cabeçalho Content-Length inválido."},
                )(scope, receive, send)
                return
            if length < 0:
                await JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Cabeçalho Content-Length inválido."},
                )(scope, receive, send)
                return
            if length > body_limit:
                await JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"detail": "O corpo da requisição excede o limite permitido."},
                )(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    raise RequestBodyTooLarge
            return message

        await self.app(scope, limited_receive, send)


class UrlJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    output_format: OutputFormat = OutputFormat.MP4
    quality: Quality = Quality.ORIGINAL


class JobResponse(BaseModel):
    id: str
    source: str
    source_name: str
    output_format: OutputFormat
    quality: Quality
    status: JobStatus
    progress: int
    message: str
    created_at: str
    updated_at: str
    output_name: str | None = None
    output_size: int = 0
    error: str | None = None
    download_url: str | None = None
    download_total: int | None = None
    download_completed: int = 0

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        download_url = f"/api/jobs/{job.id}/download" if job.status == JobStatus.COMPLETED else None
        return cls(**job.public_dict(), download_url=download_url)


def build_app(config: Settings = settings) -> FastAPI:
    store = JobStore(config)
    service = JobService(config, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        store.cleanup_orphans()
        cleanup_task = asyncio.create_task(_cleanup_loop(store))
        yield
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await store.shutdown()

    app = FastAPI(
        title="Media Converter",
        description="Conversor de vídeos e áudio para arquivos locais e links.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.job_store = store
    app.state.job_service = service

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=config.max_upload_bytes + 1024 * 1024,
        can_accept_job=store.can_accept_job,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.trusted_hosts))

    @app.exception_handler(RequestBodyTooLarge)
    async def request_body_too_large(_: Request, __: RequestBodyTooLarge) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "O corpo da requisição excede o limite permitido."},
        )

    @app.get("/api/jobs", response_model=list[JobResponse])
    async def list_jobs() -> list[JobResponse]:
        return [JobResponse.from_job(job) for job in store.list_jobs()]

    @app.get("/api/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: str) -> JobResponse:
        job = store.get(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversão não encontrada.",
            )
        return JobResponse.from_job(job)

    @app.post("/api/jobs/upload", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
    async def create_upload_job(
        file: Annotated[UploadFile, File(description="Arquivo de vídeo ou áudio")],
        output_format: Annotated[OutputFormat, Form()] = OutputFormat.MP4,
        quality: Annotated[Quality, Form()] = Quality.ORIGINAL,
    ) -> JobResponse:
        filename = sanitize_filename(file.filename or "video")
        extension = Path(filename).suffix.lower()
        if not extension:
            extension = ".bin"
        try:
            job = store.create("upload", filename, output_format, quality)
        except JobCapacityError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
            ) from exc
        try:
            job_dir = config.data_dir / job.id
            job_dir.mkdir(parents=True, exist_ok=True)
            upload_path = job_dir / "input" / f"source{extension}"
            upload_path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with upload_path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > config.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail="O arquivo excede o limite de upload.",
                        )
                    output.write(chunk)
            await service.submit_upload(job, upload_path)
        except asyncio.CancelledError:
            store.discard_job(job.id)
            raise
        except Exception:
            store.discard_job(job.id)
            raise
        finally:
            await file.close()

        return JobResponse.from_job(job)

    @app.post("/api/jobs/url", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
    async def create_url_job(payload: UrlJobRequest) -> JobResponse:
        try:
            validated_url = await validate_media_url_async(payload.url, config)
        except UrlValidationError as exc:
            raise http_error_for_url(exc) from exc

        try:
            job = store.create("url", "Vídeo do link", payload.output_format, payload.quality)
        except JobCapacityError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
            ) from exc
        try:
            await service.submit_url(job, validated_url)
        except asyncio.CancelledError:
            store.discard_job(job.id)
            raise
        except Exception:
            store.discard_job(job.id)
            raise
        return JobResponse.from_job(job)

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(job_id: str) -> JobResponse:
        if not store.get(job_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversão não encontrada."
            )
        cancelled = await store.cancel(job_id)
        job = store.get(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversão não encontrada.",
            )
        if not cancelled and job.status != JobStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Esta conversão já foi concluída.",
            )
        return JobResponse.from_job(job)

    @app.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_job(job_id: str) -> None:
        if not store.get(job_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversão não encontrada."
            )
        if not service.remove(job_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Aguarde a conversão ou o download terminar antes de removê-la.",
            )

    @app.api_route("/api/jobs/{job_id}/download", methods=["GET", "HEAD"])
    async def download_job(job_id: str) -> FileResponse:
        job = store.get(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversão não encontrada."
            )
        if job.status != JobStatus.COMPLETED or not job.output_path:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="A conversão não está concluída."
            )
        output_path = Path(job.output_path)
        if not output_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Arquivo não encontrado."
            )
        media_type = FORMATS[job.output_format].mime_type
        store.begin_download(job_id)
        return FileResponse(
            output_path,
            media_type=media_type,
            filename=job.output_name,
            background=BackgroundTask(store.end_download, job_id),
        )

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


async def _cleanup_loop(store: JobStore) -> None:
    while True:
        await asyncio.sleep(3600)
        store.cleanup_expired()
        store.cleanup_orphans()


app = build_app()
