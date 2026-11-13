from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import Job, JobStatus, JobStore, MediaInfo, UrlDownloader, _ytdlp_format_selector
from app.media import OutputFormat, Quality


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
        assert args[args.index("-t") + 1] == str(config.max_duration_seconds)
        assert args[-1] == "movie.mp4"


def test_ffmpeg_args_for_webm() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        store = JobStore(config)
        job = Job("abc", "upload", "movie.mp4", OutputFormat.WEBM, Quality.ORIGINAL)
        args = store._ffmpeg_args(job, Path("movie.mp4"), Path("movie.webm"))
        assert "libvpx-vp9" in args
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
    assert "vp9" in _ytdlp_format_selector(OutputFormat.WEBM)


def test_ytdlp_args_without_proxy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory))
        downloader = UrlDownloader(config)

        args = downloader._ytdlp_args("https://youtube.com/watch?v=x", Path("source.%(ext)s"))

        assert "--proxy" not in args
        assert args[-1] == "https://youtube.com/watch?v=x"


def test_ytdlp_args_with_proxy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory), ytdlp_proxy="http://127.0.0.1:3128")
        downloader = UrlDownloader(config)

        args = downloader._ytdlp_args("https://youtube.com/watch?v=x", Path("source.%(ext)s"))

        assert args[args.index("--proxy") + 1] == "http://127.0.0.1:3128"
        assert args[-1] == "https://youtube.com/watch?v=x"


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

        assert store.remove(job.id) is False
        store.end_download(job.id)
        assert store.remove(job.id) is True


def _raise_os_error(*args: object, **kwargs: object) -> None:
    raise OSError("disco ocupado")
