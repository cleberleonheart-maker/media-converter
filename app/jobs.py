from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from .config import Settings
from .media import (
    ALLOWED_EXTENSIONS,
    ALLOWED_INPUT_FORMATS,
    AUDIO_FORMATS,
    FORMATS,
    OutputFormat,
    Quality,
    atomic_output_path,
    codec_options,
    ensure_binary,
    input_format_whitelist,
    safe_file_size,
    sanitize_filename,
    stream_copy_compatible,
    temporary_output_path,
)
from .url_validation import (
    ResolvedUrl,
    UrlValidationError,
    ValidatedUrl,
    is_private_address,
    pinned_connection,
    resolve_public_url_async,
)


class JobCapacityError(RuntimeError):
    pass


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.CONVERTING})
MAX_CAPTURED_LINES = 200
MAX_CAPTURED_LINE_LENGTH = 4096
MAX_CAPTURED_BYTES = 1024 * 1024
MAX_DOWNLOAD_STORAGE_FACTOR = 3
YTDLP_PROGRESS_PREFIX = "__MEDIA_CONVERTER_PROGRESS__|"


def _is_media_file_inside(path: Path, directory: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(directory.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return resolved_path.is_file()


def _directory_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _largest_file_size(directory: Path) -> int:
    largest = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                largest = max(largest, path.stat().st_size)
        except OSError:
            continue
    return largest


def _parse_ytdlp_progress(line: str) -> tuple[str, int, int | None] | None:
    if not line.startswith(YTDLP_PROGRESS_PREFIX):
        return None
    parts = line[len(YTDLP_PROGRESS_PREFIX) :].split("|")
    if len(parts) not in {2, 3} or not parts[0]:
        return None
    format_id = parts[0]
    try:
        downloaded_bytes = int(parts[1])
    except ValueError:
        return None
    if downloaded_bytes < 0:
        return None
    total_bytes: int | None = None
    if len(parts) == 3:
        try:
            total_bytes = int(parts[2])
        except ValueError:
            total_bytes = None
        if total_bytes is not None and total_bytes < 0:
            total_bytes = None
    return format_id, downloaded_bytes, total_bytes


def _public_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    if isinstance(exc, UrlValidationError) and message:
        return message
    if (
        isinstance(exc, RuntimeError)
        and message
        and not re.search(r"https?://|/[A-Za-z0-9]", message)
    ):
        return message
    return "Ocorreu um erro interno ao processar o arquivo."


@dataclass
class Job:
    id: str
    source: str
    source_name: str
    output_format: OutputFormat
    quality: Quality
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    message: str = "Na fila"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    output_path: str | None = None
    output_name: str | None = None
    output_size: int = 0
    error: str | None = None
    download_total: int | None = None
    download_completed: int = 0

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_format"] = self.output_format.value
        data["quality"] = self.quality.value
        data["status"] = self.status.value
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        return data


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    has_video: bool
    has_audio: bool
    format_names: tuple[str, ...]
    video_codec: str = ""
    audio_codec: str = ""
    video_stream_index: int | None = None
    audio_stream_index: int | None = None


@dataclass
class _DownloadBudget:
    limit_bytes: int
    downloaded_by_format: dict[str, int] = field(default_factory=dict)
    total_by_format: dict[str, int] = field(default_factory=dict)
    exceeded: bool = False

    def update(
        self,
        format_id: str,
        downloaded_bytes: int,
        total_bytes: int | None = None,
    ) -> bool:
        previous = self.downloaded_by_format.get(format_id, 0)
        self.downloaded_by_format[format_id] = max(previous, downloaded_bytes)
        if total_bytes is not None:
            known_total = self.total_by_format.get(format_id, 0)
            self.total_by_format[format_id] = max(known_total, total_bytes)
        was_exceeded = self.exceeded
        self.exceeded = sum(self.downloaded_by_format.values()) > self.limit_bytes
        return self.exceeded and not was_exceeded

    @property
    def downloaded_bytes(self) -> int:
        return sum(self.downloaded_by_format.values())

    @property
    def total_bytes(self) -> int | None:
        total = sum(self.total_by_format.values())
        return total or None


class JobStore:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.active_downloads: dict[str, int] = {}
        self._download_lock = threading.Lock()
        self.semaphore = asyncio.Semaphore(config.max_concurrent_jobs)

    def can_accept_job(self) -> bool:
        pending_jobs = sum(job.status in ACTIVE_JOB_STATUSES for job in self.jobs.values())
        return (
            pending_jobs < self.config.max_pending_jobs
            and len(self.jobs) < self.config.max_retained_jobs
        )

    def create(
        self,
        source: str,
        source_name: str,
        output_format: OutputFormat,
        quality: Quality,
    ) -> Job:
        if not self.can_accept_job():
            raise JobCapacityError("O limite de conversões foi atingido.")
        job = Job(
            id=uuid4().hex,
            source=source,
            source_name=source_name,
            output_format=output_format,
            quality=quality,
        )
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def begin_download(self, job_id: str) -> None:
        with self._download_lock:
            self.active_downloads[job_id] = self.active_downloads.get(job_id, 0) + 1

    def end_download(self, job_id: str) -> None:
        with self._download_lock:
            active = self.active_downloads.get(job_id, 0)
            if active <= 1:
                self.active_downloads.pop(job_id, None)
            else:
                self.active_downloads[job_id] = active - 1

    def has_active_download(self, job_id: str) -> bool:
        with self._download_lock:
            return self.active_downloads.get(job_id, 0) > 0

    def update(self, job_id: str, **changes: Any) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = datetime.now(UTC)

    def list_jobs(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    async def run(self, job: Job, source_path: Path) -> None:
        try:
            async with self.semaphore:
                if job.status == JobStatus.CANCELLED:
                    self.discard_input(job.id)
                    return
                await self._convert(job, source_path)
        except asyncio.CancelledError:
            self.discard_input(job.id)
            self.update(job.id, status=JobStatus.CANCELLED, message="Cancelado")
            raise
        except Exception as exc:
            self.discard_input(job.id)
            self.update(
                job.id,
                status=JobStatus.FAILED,
                progress=0,
                message="Falha na conversão",
                error=_public_error_message(exc),
            )

    async def _convert(self, job: Job, source_path: Path) -> None:
        media_info = await self._probe_media(job.id, source_path)
        if job.output_format not in AUDIO_FORMATS and not media_info.has_video:
            raise RuntimeError("O arquivo de origem não contém uma faixa de vídeo.")
        if job.output_format in AUDIO_FORMATS and not media_info.has_audio:
            raise RuntimeError("O arquivo de origem não contém uma faixa de áudio.")
        if not media_info.has_video and not media_info.has_audio:
            raise RuntimeError("O arquivo de origem não contém uma faixa de mídia.")

        output_dir = self.config.data_dir / job.id / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(job.source_name).stem or "video"
        output_path = atomic_output_path(
            output_dir,
            sanitize_filename(stem),
            FORMATS[job.output_format].extension,
        )
        temporary_path = temporary_output_path(output_path)
        progress_base = 45 if job.source == "url" else 5
        self.update(
            job.id,
            status=JobStatus.CONVERTING,
            progress=progress_base,
            message="Convertendo vídeo" if media_info.has_video else "Convertendo áudio",
        )
        try:
            try:
                returncode, stderr_tail = await self._run_ffmpeg(
                    job,
                    source_path,
                    temporary_path,
                    media_info.duration_seconds,
                    progress_base,
                    media_info,
                )
            except TimeoutError as exc:
                raise RuntimeError("A conversão excedeu o tempo limite.") from exc

            if returncode != 0:
                message = stderr_tail.replace(str(source_path), "origem").replace(
                    str(temporary_path), "saída"
                )
                raise RuntimeError(message[-1200:] or "O FFmpeg não conseguiu converter o arquivo.")

            output_size = safe_file_size(temporary_path)
            if output_size == 0:
                raise RuntimeError("A conversão terminou sem gerar um arquivo válido.")
            if output_size > self.config.max_upload_bytes:
                raise RuntimeError("O arquivo convertido excede o limite permitido.")

            temporary_path.replace(output_path)
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        input_dir = (self.config.data_dir / job.id / "input").resolve()
        if source_path.parent.resolve() == input_dir:
            shutil.rmtree(input_dir, ignore_errors=True)
        self.update(
            job.id,
            status=JobStatus.COMPLETED,
            progress=100,
            message="Conversão concluída",
            output_path=str(output_path),
            output_name=output_path.name,
            output_size=safe_file_size(output_path),
        )

    async def _run_ffmpeg(
        self,
        job: Job,
        source_path: Path,
        output_path: Path,
        duration_seconds: float,
        progress_base: int,
        media_info: MediaInfo | None = None,
    ) -> tuple[int, str]:
        ffmpeg = ensure_binary(self.config.ffmpeg_binary)
        args = self._ffmpeg_args(job, source_path, output_path, media_info)
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.processes[job.id] = process
        stderr_tail = ""
        try:
            async with asyncio.timeout(self.config.conversion_timeout_seconds):
                stderr_tail = await self._read_ffmpeg_progress(
                    job.id,
                    process,
                    duration_seconds,
                    progress_base,
                )
                await process.wait()
        except TimeoutError:
            await self._terminate(job.id, process)
            raise
        except asyncio.CancelledError:
            await self._terminate(job.id, process)
            raise
        except Exception:
            await self._terminate(job.id, process)
            raise
        finally:
            self.processes.pop(job.id, None)

        returncode = process.returncode if process.returncode is not None else -1
        return returncode, stderr_tail

    async def _read_ffmpeg_progress(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
        duration_seconds: float,
        progress_base: int,
    ) -> str:
        if process.stderr is None:
            await process.wait()
            return ""

        stderr_tail = ""
        while True:
            raw_line = await process.stderr.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            stderr_tail = (stderr_tail + f"\n{line}")[-12000:]
            key, separator, value = line.partition("=")
            if not separator:
                continue
            if key in {"out_time_us", "out_time_ms"} and value.isdigit() and duration_seconds > 0:
                ratio = min(1, int(value) / (duration_seconds * 1_000_000))
                percent = progress_base + int(ratio * (95 - progress_base))
                self.update(job_id, progress=percent)
            elif key == "progress" and value == "end":
                self.update(job_id, progress=95)
        return stderr_tail

    async def _probe_media(self, job_id: str, source_path: Path) -> MediaInfo:
        ffprobe = ensure_binary(self.config.ffprobe_binary)
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            input_format_whitelist(),
            "-show_entries",
            "format=duration,format_name:stream=index,codec_type,codec_name,width,height:stream_disposition=attached_pic",
            "-of",
            "json",
            str(source_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.processes[job_id] = process
        try:
            async with asyncio.timeout(self.config.probe_timeout_seconds):
                stdout, _ = await process.communicate()
        except TimeoutError as exc:
            await self._terminate(job_id, process)
            raise RuntimeError("A análise do arquivo excedeu o tempo limite.") from exc
        except asyncio.CancelledError:
            await self._terminate(job_id, process)
            raise
        except Exception:
            await self._terminate(job_id, process)
            raise
        finally:
            self.processes.pop(job_id, None)

        if process.returncode != 0:
            raise RuntimeError("O arquivo de origem não é uma mídia válida ou suportada.")
        try:
            payload = json.loads(stdout)
            format_data = payload.get("format", {})
            duration = float(format_data.get("duration", 0))
            format_names = tuple(
                name.strip()
                for name in str(format_data.get("format_name", "")).split(",")
                if name.strip()
            )
            raw_streams = payload.get("streams", [])
            streams = [stream for stream in raw_streams if isinstance(stream, dict)]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("Não foi possível analisar o arquivo de origem.") from exc
        if not format_names or not ALLOWED_INPUT_FORMATS.intersection(format_names):
            raise RuntimeError("O formato do arquivo de origem não é suportado.")
        if duration > self.config.max_duration_seconds:
            raise RuntimeError("A mídia excede a duração máxima permitida.")

        video_streams = [
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic", False)
        ]
        for stream in video_streams:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width * height > self.config.max_video_pixels:
                raise RuntimeError("A resolução da mídia excede o limite permitido.")

        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        video_stream_index = (
            video_streams[0].get("index")
            if video_streams and isinstance(video_streams[0].get("index"), int)
            else None
        )
        audio_stream_index = (
            audio_streams[0].get("index")
            if audio_streams and isinstance(audio_streams[0].get("index"), int)
            else None
        )
        return MediaInfo(
            duration_seconds=duration,
            has_video=bool(video_streams),
            has_audio=bool(audio_streams),
            format_names=format_names,
            video_codec=str(video_streams[0].get("codec_name") or "") if video_streams else "",
            audio_codec=str(audio_streams[0].get("codec_name") or "") if audio_streams else "",
            video_stream_index=video_stream_index,
            audio_stream_index=audio_stream_index,
        )

    def _ffmpeg_args(
        self,
        job: Job,
        source_path: Path,
        output_path: Path,
        media_info: MediaInfo | None = None,
    ) -> list[str]:
        stream_copy = (
            media_info is not None
            and job.quality == Quality.ORIGINAL
            and stream_copy_compatible(
                job.output_format,
                media_info.video_codec,
                media_info.audio_codec,
            )
        )
        args = [
            "-y",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "error",
            "-progress",
            "pipe:2",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            input_format_whitelist(),
            "-threads",
            str(self.config.ffmpeg_threads),
            "-i",
            str(source_path),
        ]
        if job.output_format in AUDIO_FORMATS:
            audio_selector = (
                f"0:{media_info.audio_stream_index}"
                if media_info is not None and media_info.audio_stream_index is not None
                else "0:a:0"
            )
            args.extend(["-map", audio_selector])
        else:
            video_selector = (
                f"0:{media_info.video_stream_index}"
                if media_info is not None and media_info.video_stream_index is not None
                else "0:v:0"
            )
            audio_selector = (
                f"0:{media_info.audio_stream_index}"
                if media_info is not None and media_info.audio_stream_index is not None
                else "0:a:0?"
            )
            args.extend(["-map", video_selector, "-map", audio_selector])
        if job.output_format not in AUDIO_FORMATS and job.quality != Quality.ORIGINAL:
            height = {"high": 1080, "medium": 720, "low": 480}[job.quality]
            args.extend(["-vf", f"scale=-2:'min({height},ih)'"])
        if stream_copy:
            if job.output_format in AUDIO_FORMATS:
                args.extend(["-vn", "-c:a", "copy"])
            else:
                args.extend(["-c", "copy"])
        else:
            args.extend(codec_options(job.output_format))
        args.extend(["-threads", str(self.config.ffmpeg_threads), "-max_muxing_queue_size", "1024"])
        args.extend(["-t", str(self.config.max_duration_seconds)])
        args.extend(["-fs", str(self.config.max_upload_bytes + 1)])
        if job.output_format == OutputFormat.MP4:
            args.extend(["-movflags", "+faststart"])
        args.extend([str(output_path)])
        return args

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return False
        self.update(job_id, status=JobStatus.CANCELLED, message="Cancelado")
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        process = self.processes.get(job_id)
        if process:
            await self._terminate(job_id, process)
        if task and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def _terminate(self, job_id: str, process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=5)
        except TimeoutError:
            await self._kill_process(process)
        except asyncio.CancelledError:
            await self._kill_process(process)
            raise

    async def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=5)

    def discard_input(self, job_id: str) -> None:
        shutil.rmtree(self.config.data_dir / job_id / "input", ignore_errors=True)

    def discard_job(self, job_id: str) -> None:
        shutil.rmtree(self.config.data_dir / job_id, ignore_errors=True)
        self.jobs.pop(job_id, None)
        self.tasks.pop(job_id, None)

    def _delete_job_dir(self, job_id: str) -> bool:
        path = self.config.data_dir / job_id
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def remove(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        task = self.tasks.get(job_id)
        if (
            job.status in ACTIVE_JOB_STATUSES
            or self.has_active_download(job_id)
            or (task is not None and not task.done())
        ):
            return False
        if not self._delete_job_dir(job_id):
            return False
        self.jobs.pop(job_id, None)
        self.tasks.pop(job_id, None)
        return True

    def cleanup_expired(self) -> None:
        cutoff = time.time() - self.config.cleanup_after_seconds
        for job_id, job in list(self.jobs.items()):
            task = self.tasks.get(job_id)
            if (
                job.status in ACTIVE_JOB_STATUSES
                or self.has_active_download(job_id)
                or (task is not None and not task.done())
                or job.updated_at.timestamp() >= cutoff
            ):
                continue
            if not self._delete_job_dir(job_id):
                continue
            self.jobs.pop(job_id, None)
            self.tasks.pop(job_id, None)

    def cleanup_orphans(self) -> None:
        if not self.config.data_dir.is_dir():
            return
        cutoff = time.time() - self.config.cleanup_after_seconds
        for path in self.config.data_dir.iterdir():
            try:
                UUID(path.name)
            except ValueError:
                continue
            if path.name in self.jobs:
                continue
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                continue
            if modified_at >= cutoff:
                continue
            if path.is_symlink():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path, ignore_errors=True)

    async def shutdown(self) -> None:
        for task in list(self.tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for process in list(self.processes.values()):
            await self._terminate("shutdown", process)


def _ytdlp_format_selector(output_format: OutputFormat | None) -> str:
    if output_format in AUDIO_FORMATS:
        return "ba/b"
    if output_format == OutputFormat.MP4:
        return "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    if output_format == OutputFormat.WEBM:
        return "bv*[ext=webm]+ba/bv*+ba/b"
    if output_format == OutputFormat.MKV:
        return "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*+ba/b"
    return "bv*+ba/b"


def _ytdlp_error_message(stderr: str) -> str:
    normalized = stderr.casefold()
    if "sign in to confirm your age" in normalized or "age-restricted" in normalized:
        return (
            "Este vídeo exige confirmação de idade. Use cookies de uma conta do YouTube "
            "com a confirmação de idade concluída."
        )
    if "sign in to confirm you're not a bot" in normalized:
        return "O YouTube exigiu autenticação para continuar o download."
    if "unsupported url" in normalized:
        return "O yt-dlp não reconhece este link."
    return "Não foi possível baixar o link."


class UrlDownloader:
    def __init__(self, config: Settings) -> None:
        self.config = config

    async def download(
        self,
        job: Job,
        validated_url: ValidatedUrl,
        store: JobStore,
    ) -> Path:
        if validated_url.direct_media:
            return await self._download_direct(job, validated_url, store)
        return await self._download_with_ytdlp(job, validated_url, store)

    def _ytdlp_args(
        self,
        url: str,
        output_template: Path,
        output_format: OutputFormat | None = None,
    ) -> list[str]:
        args = [
            "--no-playlist",
            "--newline",
            "--no-warnings",
            "--no-cache-dir",
            "--no-colors",
            "--progress",
            "--progress-template",
            f"download:{YTDLP_PROGRESS_PREFIX}%(info.format_id)s|%(progress.downloaded_bytes)s"
            "|%(progress.total_bytes)s",
            "--concurrent-fragments",
            "4",
            "--format",
            _ytdlp_format_selector(output_format),
            "--max-filesize",
            str(self.config.max_upload_bytes),
            "--retries",
            "3",
            "--socket-timeout",
            str(self.config.download_timeout_seconds),
            "--output",
            str(output_template),
            "--print",
            "after_move:filepath",
        ]
        if self.config.ytdlp_cookies_file is not None:
            args.extend(["--cookies", str(self.config.ytdlp_cookies_file)])
        if self.config.ytdlp_cookies_from_browser:
            args.extend(["--cookies-from-browser", self.config.ytdlp_cookies_from_browser])
        for runtime in self.config.ytdlp_js_runtimes:
            args.extend(["--js-runtimes", runtime])
        if self.config.ytdlp_proxy:
            args.extend(["--proxy", self.config.ytdlp_proxy])
        args.append(url)
        return args

    async def _download_with_ytdlp(
        self,
        job: Job,
        validated_url: ValidatedUrl,
        store: JobStore,
    ) -> Path:
        ytdlp = ensure_binary(self.config.ytdlp_binary)
        output_dir = self.config.data_dir / job.id / "input"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = output_dir / "source.%(ext)s"
        args = [
            ytdlp,
            *self._ytdlp_args(validated_url.url, output_template, job.output_format),
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        store.processes[job.id] = process
        store.update(
            job.id,
            status=JobStatus.DOWNLOADING,
            progress=1,
            message="Baixando conteúdo",
        )
        oversized = asyncio.Event()
        budget = _DownloadBudget(
            limit_bytes=self.config.max_upload_bytes + (self.config.max_upload_bytes // 100)
        )
        monitor_task = asyncio.create_task(
            self._watch_output_size(job, output_dir, process, store, oversized)
        )
        try:
            async with asyncio.timeout(self.config.download_total_timeout_seconds):
                stdout_lines, stderr = await asyncio.gather(
                    self._read_ytdlp_stdout(job, process, store, budget),
                    self._read_ytdlp_stderr(job, process, store),
                )
                await process.wait()
        except TimeoutError as exc:
            await store._terminate(job.id, process)
            raise RuntimeError("O download excedeu o tempo limite.") from exc
        except asyncio.CancelledError:
            await store._terminate(job.id, process)
            raise
        except Exception:
            await store._terminate(job.id, process)
            raise
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            store.processes.pop(job.id, None)

        if (
            budget.exceeded
            or oversized.is_set()
            or _largest_file_size(output_dir) > self.config.max_upload_bytes
        ):
            raise RuntimeError("O arquivo baixado excede o limite permitido.")

        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(_ytdlp_error_message(message))

        lines = [line.strip() for line in stdout_lines if line.strip()]
        candidates = [
            Path(line)
            for line in lines
            if line
            and _is_media_file_inside(Path(line), output_dir)
            and not Path(line).name.endswith((".part", ".ytdl", ".temp"))
        ]
        if not candidates:
            candidates = [
                path
                for path in output_dir.glob("source.*")
                if _is_media_file_inside(path, output_dir)
                and not path.name.endswith((".part", ".ytdl", ".temp"))
            ]
        if not candidates:
            raise RuntimeError("O link não produziu um arquivo de mídia.")

        chosen = max(candidates, key=safe_file_size)
        chosen_size = safe_file_size(chosen)
        if chosen_size <= 0:
            raise RuntimeError("O link não produziu um arquivo de mídia válido.")
        if chosen_size > self.config.max_upload_bytes:
            raise RuntimeError("O arquivo baixado excede o limite permitido.")
        if job.source_name == "Vídeo do link":
            job.source_name = sanitize_filename(chosen.stem or "video")
        return chosen

    async def _watch_output_size(
        self,
        job: Job,
        output_dir: Path,
        process: asyncio.subprocess.Process,
        store: JobStore,
        oversized: asyncio.Event,
    ) -> None:
        while process.returncode is None:
            if (
                _largest_file_size(output_dir) > self.config.max_upload_bytes
                or _directory_size(output_dir)
                > self.config.max_upload_bytes * MAX_DOWNLOAD_STORAGE_FACTOR
            ):
                oversized.set()
                await store._terminate(job.id, process)
                return
            await asyncio.sleep(0.25)

    async def _read_ytdlp_stdout(
        self,
        job: Job,
        process: asyncio.subprocess.Process,
        store: JobStore,
        budget: _DownloadBudget,
    ) -> list[str]:
        if process.stdout is None:
            return []

        lines: list[str] = []
        while True:
            raw_line = await process.stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            lines.append(line[:MAX_CAPTURED_LINE_LENGTH])
            if len(lines) > MAX_CAPTURED_LINES:
                lines.pop(0)
            progress = _parse_ytdlp_progress(line)
            if progress is not None:
                if budget.update(*progress):
                    await store._terminate(job.id, process)
                self._update_download_totals(job, budget, store)
            self._update_ytdlp_progress(job, line, store)
        return lines

    async def _read_ytdlp_stderr(
        self,
        job: Job,
        process: asyncio.subprocess.Process,
        store: JobStore,
    ) -> bytes:
        if process.stderr is None:
            return b""

        chunks: list[bytes] = []
        captured_bytes = 0
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            if captured_bytes < MAX_CAPTURED_BYTES:
                remaining = MAX_CAPTURED_BYTES - captured_bytes
                captured = chunk[:remaining]
                chunks.append(captured)
                captured_bytes += len(captured)
            text = chunk.decode("utf-8", errors="replace")
            self._update_ytdlp_progress(job, text, store)
        return b"".join(chunks)

    def _update_download_totals(
        self,
        job: Job,
        budget: _DownloadBudget,
        store: JobStore,
    ) -> None:
        completed = budget.downloaded_bytes
        total = budget.total_bytes
        changes: dict[str, Any] = {
            "download_completed": completed,
            "download_total": total,
        }
        if total:
            changes["progress"] = min(45, max(1, int(completed / total * 45)))
        store.update(job.id, **changes)

    def _update_ytdlp_progress(self, job: Job, text: str, store: JobStore) -> None:
        for match in re.finditer(r"(\d{1,3}(?:\.\d+)?)%", text):
            value = float(match.group(1))
            if value > 100:
                continue
            progress = min(45, max(1, int(value * 0.45)))
            store.update(job.id, progress=progress)

    async def _download_direct(
        self,
        job: Job,
        validated_url: ValidatedUrl,
        store: JobStore,
    ) -> Path:
        try:
            async with asyncio.timeout(self.config.download_total_timeout_seconds):
                return await self._download_direct_content(job, validated_url, store)
        except TimeoutError as exc:
            raise RuntimeError("O download do link excedeu o tempo limite.") from exc

    def _validate_response_peer(self, response: httpx.Response) -> None:
        if self.config.allow_private_hosts:
            return
        network_stream = response.extensions.get("network_stream")
        get_extra_info = getattr(network_stream, "get_extra_info", None)
        if not callable(get_extra_info):
            return
        server_address = get_extra_info("server_addr")
        if not server_address:
            return
        peer_address = str(server_address[0])
        if is_private_address(peer_address):
            raise RuntimeError("O link conectado a um endereço privado ou local.")

    @contextlib.asynccontextmanager
    async def _open_pinned_stream(
        self,
        client: httpx.AsyncClient,
        resolved: ResolvedUrl,
    ) -> AsyncIterator[httpx.Response]:
        last_error: Exception | None = None
        for address in resolved.addresses:
            connect_url, host_header = pinned_connection(resolved, address)
            try:
                async with client.stream(
                    "GET",
                    connect_url,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": resolved.hostname},
                ) as response:
                    yield response
                    return
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        raise RuntimeError("Não foi possível conectar ao link.") from last_error

    async def _download_direct_content(
        self,
        job: Job,
        validated_url: ValidatedUrl,
        store: JobStore,
    ) -> Path:
        output_dir = self.config.data_dir / job.id / "input"
        output_dir.mkdir(parents=True, exist_ok=True)
        current_url = validated_url.url
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=self.config.download_timeout_seconds,
            trust_env=False,
        ) as client:
            for _ in range(6):
                resolved = await resolve_public_url_async(current_url, self.config)
                current_url = resolved.url
                async with self._open_pinned_stream(client, resolved) as response:
                    self._validate_response_peer(response)
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError(
                                "O link direto retornou um redirecionamento inválido."
                            )
                        next_url = str(httpx.URL(current_url).join(location))
                        if current_url.lower().startswith(
                            "https://"
                        ) and next_url.lower().startswith("http://"):
                            raise RuntimeError(
                                "O redirecionamento não pode remover a proteção HTTPS."
                            )
                        current_url = next_url
                        continue

                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise RuntimeError("O servidor do link recusou o download.") from exc
                    content_type = response.headers.get("content-type", "").lower()
                    extension = Path(current_url.split("?", 1)[0]).suffix.lower()
                    if content_type.startswith("text/html") or (
                        not content_type.startswith(
                            ("video/", "audio/", "application/octet-stream")
                        )
                        and extension not in ALLOWED_EXTENSIONS
                    ):
                        raise RuntimeError(
                            "O link não aponta diretamente para um arquivo de mídia."
                        )

                    content_length = response.headers.get("content-length", "0")
                    try:
                        total = max(0, int(content_length)) or None
                    except ValueError:
                        total = None
                    if total is not None and total > self.config.max_upload_bytes:
                        raise RuntimeError("O arquivo direto excede o limite de download.")
                    store.update(
                        job.id,
                        status=JobStatus.DOWNLOADING,
                        progress=1,
                        message="Baixando arquivo direto",
                        download_total=total,
                        download_completed=0,
                    )
                    extension = Path(current_url.split("?", 1)[0]).suffix.lower() or ".media"
                    destination = output_dir / f"source{extension}"
                    completed = 0
                    last_reported = 0
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            completed += len(chunk)
                            if completed > self.config.max_upload_bytes:
                                raise RuntimeError("O arquivo direto excede o limite de download.")
                            output.write(chunk)
                            if total:
                                percent = min(45, max(1, int(completed / total * 45)))
                                store.update(job.id, progress=percent, download_completed=completed)
                            elif completed - last_reported >= 1024 * 1024:
                                last_reported = completed
                                store.update(job.id, download_completed=completed)
                    if total:
                        store.update(job.id, download_completed=completed)
                    if safe_file_size(destination) == 0:
                        raise RuntimeError("O link direto produziu um arquivo vazio.")
                    return destination
        raise RuntimeError("O link direto excedeu o limite de redirecionamentos.")


class JobService:
    def __init__(self, config: Settings, store: JobStore) -> None:
        self.config = config
        self.store = store
        self.downloader = UrlDownloader(config)

    async def submit_upload(
        self,
        job: Job,
        uploaded_path: Path,
    ) -> None:
        task = asyncio.create_task(self.store.run(job, uploaded_path))
        self.store.tasks[job.id] = task

    async def submit_url(self, job: Job, validated_url: ValidatedUrl) -> None:
        async def process() -> None:
            try:
                async with self.store.semaphore:
                    if job.status == JobStatus.CANCELLED:
                        self.store.discard_input(job.id)
                        return
                    source_path = await self.downloader.download(job, validated_url, self.store)
                    await self.store._convert(job, source_path)
            except asyncio.CancelledError:
                self.store.discard_input(job.id)
                self.store.update(job.id, status=JobStatus.CANCELLED, message="Cancelado")
                raise
            except Exception as exc:
                self.store.discard_input(job.id)
                self.store.update(
                    job.id,
                    status=JobStatus.FAILED,
                    progress=0,
                    message="Falha no processamento",
                    error=_public_error_message(exc),
                )

        task = asyncio.create_task(process())
        self.store.tasks[job.id] = task

    def remove(self, job_id: str) -> bool:
        return self.store.remove(job_id)
