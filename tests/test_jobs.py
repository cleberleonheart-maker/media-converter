from __future__ import annotations

import asyncio
import contextlib
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.jobs import (
    Job,
    JobStatus,
    JobStore,
    MediaInfo,
    UrlDownloader,
    _DownloadBudget,
    _parse_ytdlp_progress,
    _ytdlp_error_message,
    _ytdlp_format_selector,
)
from app.media import OutputFormat, Quality, stream_copy_compatible
from app.url_validation import ResolvedUrl, ValidatedUrl


def test_ffmpeg_args_for_video() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = Job("abc", "upload", "movie.mp4", OutputFormat.MP4, Quality.HIGH)
        args = store._ffmpeg_args(job, Path("movie.mkv"), Path("movie.mp4"))
        assert "-vf" in args
        assert "scale=-2:'min(1080,ih)'" in args
        assert "-protocol_whitelist" in args
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert "-format_whitelist" in args
        assert args[args.index("-fs") + 1] == str(config.max_upload_bytes + 1)
        assert args[args.index("-threads") + 1] == str(config.ffmpeg_threads)
        maps = [args[index + 1] for index, value in enumerate(args) if value == "-map"]
        assert maps == ["0:v:0", "0:a:0?"]
        assert args[args.index("-t") + 1] == str(config.max_duration_seconds)
        assert args[-1] == "movie.mp4"


def test_ffmpeg_args_for_webm() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = Job("abc", "upload", "movie.mp4", OutputFormat.WEBM, Quality.ORIGINAL)
        args = store._ffmpeg_args(job, Path("movie.mp4"), Path("movie.webm"))
        assert args[args.index("-c:v") + 1] == "libvpx"
        assert "libopus" in args
        assert "libx264" not in args


def test_ffmpeg_args_for_audio() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = Job("abc", "upload", "movie.mp4", OutputFormat.MP3, Quality.ORIGINAL)
        args = store._ffmpeg_args(job, Path("movie.mp4"), Path("movie.mp3"))
        assert "-vn" in args
        assert "libmp3lame" in args


def test_ffmpeg_args_copies_compatible_streams() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = Job("abc", "upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        media_info = MediaInfo(60, True, True, ("mp4",), "h264", "aac")
        args = store._ffmpeg_args(job, Path("movie.mp4"), Path("movie.mp4"), media_info)
        assert args[args.index("-c") + 1] == "copy"
        assert "libx264" not in args


def test_ffmpeg_args_use_selected_stream_indices() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = Job("abc", "upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        media_info = MediaInfo(
            60,
            True,
            True,
            ("mp4",),
            "h264",
            "aac",
            video_stream_index=2,
            audio_stream_index=3,
        )
        args = store._ffmpeg_args(job, Path("movie.mp4"), Path("movie.mp4"), media_info)
        maps = [args[index + 1] for index, value in enumerate(args) if value == "-map"]
        assert maps == ["0:2", "0:3"]


def test_stream_copy_respects_output_codec_contract() -> None:
    assert stream_copy_compatible(OutputFormat.MP4, "h264", "aac")
    assert not stream_copy_compatible(OutputFormat.MP4, "h264", "mp3")
    assert not stream_copy_compatible(OutputFormat.WEBM, "vp9", "opus")
    assert not stream_copy_compatible(OutputFormat.WAV, "", "pcm_s24le")


def test_ffmpeg_args_reencodes_when_scaling() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = Job("abc", "upload", "movie.mp4", OutputFormat.MP4, Quality.HIGH)
        media_info = MediaInfo(60, True, True, ("mp4",), "h264", "aac")
        args = store._ffmpeg_args(job, Path("movie.mp4"), Path("movie.mp4"), media_info)
        assert "-c" not in args
        assert "libx264" in args


def test_ytdlp_format_selector() -> None:
    assert _ytdlp_format_selector(OutputFormat.MP3) == "ba/b"
    assert "avc1" in _ytdlp_format_selector(OutputFormat.MP4)
    webm_selector = _ytdlp_format_selector(OutputFormat.WEBM)
    assert webm_selector.split("/")[:2] == ["bv*[ext=webm]+ba", "bv*+ba"]


def test_ytdlp_progress_tracks_aggregate_download_size() -> None:
    assert _parse_ytdlp_progress("__MEDIA_CONVERTER_PROGRESS__|137|600") == ("137", 600, None)
    assert _parse_ytdlp_progress("__MEDIA_CONVERTER_PROGRESS__|137|600|1200") == (
        "137",
        600,
        1200,
    )
    assert _parse_ytdlp_progress("__MEDIA_CONVERTER_PROGRESS__|137|600|NA") == ("137", 600, None)
    assert _parse_ytdlp_progress("invalid") is None

    budget = _DownloadBudget(limit_bytes=1000)
    assert budget.update("137", 600) is False
    assert budget.update("140", 500) is True
    assert budget.exceeded is True
    assert budget.downloaded_bytes == 1100


def test_ytdlp_progress_tracks_total_size() -> None:
    budget = _DownloadBudget(limit_bytes=10_000)
    assert budget.update("137", 600, 1200) is False

    assert budget.downloaded_bytes == 600
    assert budget.total_bytes == 1200


def test_ytdlp_error_message_for_age_restricted_video() -> None:
    stderr = (
        "ERROR: [youtube] 02WlSQrVuJ8: Sign in to confirm your age. "
        "This video may be inappropriate for some users."
    )

    assert _ytdlp_error_message(stderr) == (
        "Este vídeo exige confirmação de idade. Use cookies de uma conta do YouTube "
        "com a confirmação de idade concluída."
    )


def test_ytdlp_error_message_for_unknown_error() -> None:
    assert _ytdlp_error_message("ERROR: unexpected failure") == "Não foi possível baixar o link."


def test_ytdlp_args_without_proxy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        downloader = UrlDownloader(config)

        args = downloader._ytdlp_args("https://youtube.com/watch?v=x", Path("source.%(ext)s"))

        assert "--proxy" not in args
        assert "__MEDIA_CONVERTER_PROGRESS__|" in args[args.index("--progress-template") + 1]
        assert args[-1] == "https://youtube.com/watch?v=x"


def test_ytdlp_args_with_proxy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory), ytdlp_proxy="http://127.0.0.1:3128")
        downloader = UrlDownloader(config)

        args = downloader._ytdlp_args("https://youtube.com/watch?v=x", Path("source.%(ext)s"))

        assert args[args.index("--proxy") + 1] == "http://127.0.0.1:3128"
        assert args[-1] == "https://youtube.com/watch?v=x"


def test_ytdlp_args_with_cookies_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cookies_path = Path(directory) / "cookies.txt"
        cookies_path.write_text("# Netscape HTTP Cookie File\n")
        cookies_path.chmod(0o600)
        config = Settings(data_dir=Path(directory), ytdlp_cookies_file=cookies_path)
        downloader = UrlDownloader(config)

        args = downloader._ytdlp_args("https://youtube.com/watch?v=x", Path("source.%(ext)s"))

        assert args[args.index("--cookies") + 1] == str(cookies_path)


def test_ytdlp_args_with_cookies_from_browser() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(
            data_dir=Path(directory),
            ytdlp_cookies_from_browser="chromium",
        )
        downloader = UrlDownloader(config)

        args = downloader._ytdlp_args("https://youtube.com/watch?v=x", Path("source.%(ext)s"))

        assert args[args.index("--cookies-from-browser") + 1] == "chromium"


def test_ytdlp_args_with_js_runtimes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(
            data_dir=Path(directory),
            ytdlp_js_runtimes=("node:/usr/bin/node",),
        )
        downloader = UrlDownloader(config)

        args = downloader._ytdlp_args("https://youtube.com/watch?v=x", Path("source.%(ext)s"))

        assert args[args.index("--js-runtimes") + 1] == "node:/usr/bin/node"


def test_ytdlp_cookies_file_requires_private_permissions() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cookies_path = Path(directory) / "cookies.txt"
        cookies_path.write_text("# Netscape HTTP Cookie File\n")
        cookies_path.chmod(0o644)

        with pytest.raises(ValueError, match="proprietário"):
            Settings(data_dir=Path(directory), ytdlp_cookies_file=cookies_path)


def test_remove_keeps_job_when_files_cannot_be_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = store.create("upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        store.update(job.id, status=JobStatus.FAILED)
        monkeypatch.setattr("app.jobs.shutil.rmtree", _raise_os_error)

        assert store.remove(job.id) is False
        assert store.get(job.id) is job


def test_remove_is_blocked_during_download() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = store.create("upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        store.update(job.id, status=JobStatus.COMPLETED)
        store.begin_download(job.id)
        store.begin_download(job.id)

        store.end_download(job.id)
        assert store.remove(job.id) is False
        store.end_download(job.id)
        assert store.remove(job.id) is True


def test_concurrent_download_completion_preserves_counter() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = store.create("upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        store.begin_download(job.id)
        store.begin_download(job.id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(store.end_download, job.id) for _ in range(2)]
            for future in futures:
                future.result()

        assert store.has_active_download(job.id) is False


@pytest.mark.asyncio
async def test_cancel_waits_for_running_task_to_stop() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = store.create("upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        blocker = asyncio.Event()

        async def wait_forever() -> None:
            await blocker.wait()

        task = asyncio.create_task(wait_forever())
        store.tasks[job.id] = task
        await asyncio.sleep(0)

        assert await store.cancel(job.id) is True
        assert task.done()


@pytest.mark.asyncio
async def test_remove_is_blocked_while_task_is_running() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = store.create("upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        blocker = asyncio.Event()

        async def wait_forever() -> None:
            await blocker.wait()

        task = asyncio.create_task(wait_forever())
        store.tasks[job.id] = task
        await asyncio.sleep(0)
        store.update(job.id, status=JobStatus.CANCELLED)

        assert store.remove(job.id) is False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert store.remove(job.id) is True


@pytest.mark.asyncio
async def test_convert_removes_partial_output_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = store.create("upload", "movie.mp4", OutputFormat.MP4, Quality.ORIGINAL)
        source_path = config.data_dir / job.id / "input" / "source.mp4"
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"source")

        async def fake_probe(_: str, __: Path) -> MediaInfo:
            return MediaInfo(60, True, True, ("mp4",), "h264", "aac")

        async def fake_ffmpeg(
            _: Job,
            __: Path,
            output_path: Path,
            ___: float,
            ____: int,
            _____: MediaInfo,
        ) -> tuple[int, str]:
            output_path.write_bytes(b"")
            return 0, ""

        monkeypatch.setattr(store, "_probe_media", fake_probe)
        monkeypatch.setattr(store, "_run_ffmpeg", fake_ffmpeg)

        with pytest.raises(RuntimeError, match="sem gerar um arquivo válido"):
            await store._convert(job, source_path)

        temporary_path = config.data_dir / job.id / "output" / ".movie.part.mp4"
        assert not temporary_path.exists()


@pytest.mark.asyncio
async def test_direct_download_rejects_known_oversized_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory), max_upload_bytes=10)
        store = JobStore(config)
        job = store.create("url", "Vídeo do link", OutputFormat.MP4, Quality.ORIGINAL)
        downloader = UrlDownloader(config)
        response = httpx.Response(
            200,
            headers={"content-length": "11"},
            request=httpx.Request("GET", "https://example.com/video.mp4"),
        )

        @contextlib.asynccontextmanager
        async def fake_open_stream(
            _: httpx.AsyncClient,
            __: ResolvedUrl,
        ):
            yield response

        async def fake_resolve(_: str, __: Settings) -> ResolvedUrl:
            return ResolvedUrl(
                "https://example.com/video.mp4",
                "example.com",
                443,
                ("93.184.216.34",),
            )

        monkeypatch.setattr(downloader, "_open_pinned_stream", fake_open_stream)
        monkeypatch.setattr("app.jobs.resolve_public_url_async", fake_resolve)

        with pytest.raises(RuntimeError, match="excede o limite"):
            await downloader._download_direct(
                job,
                ValidatedUrl("https://example.com/video.mp4", "example.com", True),
                store,
            )


def _raise_os_error(*args: object, **kwargs: object) -> None:
    raise OSError("disco ocupado")
