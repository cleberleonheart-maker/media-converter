from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path = BASE_DIR / "data"
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    allowed_hosts: tuple[str, ...] = (
        "youtube.com",
        "youtu.be",
        "vimeo.com",
        "dailymotion.com",
        "twitch.tv",
        "facebook.com",
        "fb.watch",
        "instagram.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "reddit.com",
        "soundcloud.com",
        "bilibili.com",
        "odysee.com",
        "rumble.com",
        "streamable.com",
        "coub.com",
        "nicovideo.jp",
        "linkedin.com",
    )
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    ytdlp_binary: str = "yt-dlp"
    ytdlp_proxy: str = ""
    allow_private_hosts: bool = False
    download_timeout_seconds: int = 120
    download_total_timeout_seconds: int = 60 * 60 * 12
    conversion_timeout_seconds: int = 60 * 60 * 12
    probe_timeout_seconds: int = 120
    cleanup_after_seconds: int = 60 * 60 * 24
    max_concurrent_jobs: int = 2
    max_pending_jobs: int = 10
    max_retained_jobs: int = 100
    max_duration_seconds: int = 60 * 60 * 4
    max_video_pixels: int = 16_777_216

    def __post_init__(self) -> None:
        positive_values = {
            "max_upload_bytes": self.max_upload_bytes,
            "download_timeout_seconds": self.download_timeout_seconds,
            "download_total_timeout_seconds": self.download_total_timeout_seconds,
            "conversion_timeout_seconds": self.conversion_timeout_seconds,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "cleanup_after_seconds": self.cleanup_after_seconds,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "max_pending_jobs": self.max_pending_jobs,
            "max_retained_jobs": self.max_retained_jobs,
            "max_duration_seconds": self.max_duration_seconds,
            "max_video_pixels": self.max_video_pixels,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} deve ser maior que zero.")
        if not self.trusted_hosts:
            raise ValueError("É necessário definir pelo menos um host confiável.")

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        data_dir = Path(os.getenv("MEDIA_CONVERTER_DATA_DIR", str(defaults.data_dir))).resolve()
        allowed_hosts = tuple(
            host.strip().lower().rstrip(".")
            for host in os.getenv("MEDIA_CONVERTER_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        trusted_hosts = tuple(
            host.strip().lower().rstrip(".")
            for host in os.getenv("MEDIA_CONVERTER_TRUSTED_HOSTS", "").split(",")
            if host.strip()
        )
        return cls(
            data_dir=data_dir,
            max_upload_bytes=int(
                os.getenv("MEDIA_CONVERTER_MAX_UPLOAD_BYTES", str(defaults.max_upload_bytes))
            ),
            allowed_hosts=allowed_hosts or defaults.allowed_hosts,
            trusted_hosts=trusted_hosts or defaults.trusted_hosts,
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", defaults.ffmpeg_binary),
            ffprobe_binary=os.getenv("FFPROBE_BINARY", defaults.ffprobe_binary),
            ytdlp_binary=os.getenv("YTDLP_BINARY", defaults.ytdlp_binary),
            ytdlp_proxy=os.getenv("MEDIA_CONVERTER_YTDLP_PROXY", defaults.ytdlp_proxy).strip(),
            allow_private_hosts=os.getenv("MEDIA_CONVERTER_ALLOW_PRIVATE_HOSTS", "").strip().lower()
            in {"1", "true", "yes", "on"},
            download_timeout_seconds=int(
                os.getenv(
                    "MEDIA_CONVERTER_DOWNLOAD_TIMEOUT_SECONDS",
                    str(defaults.download_timeout_seconds),
                )
            ),
            download_total_timeout_seconds=int(
                os.getenv(
                    "MEDIA_CONVERTER_DOWNLOAD_TOTAL_TIMEOUT_SECONDS",
                    str(defaults.download_total_timeout_seconds),
                )
            ),
            conversion_timeout_seconds=int(
                os.getenv(
                    "MEDIA_CONVERTER_CONVERSION_TIMEOUT_SECONDS",
                    str(defaults.conversion_timeout_seconds),
                )
            ),
            probe_timeout_seconds=int(
                os.getenv(
                    "MEDIA_CONVERTER_PROBE_TIMEOUT_SECONDS",
                    str(defaults.probe_timeout_seconds),
                )
            ),
            cleanup_after_seconds=int(
                os.getenv(
                    "MEDIA_CONVERTER_CLEANUP_AFTER_SECONDS",
                    str(defaults.cleanup_after_seconds),
                )
            ),
            max_concurrent_jobs=int(
                os.getenv("MEDIA_CONVERTER_MAX_CONCURRENT_JOBS", str(defaults.max_concurrent_jobs))
            ),
            max_pending_jobs=int(
                os.getenv("MEDIA_CONVERTER_MAX_PENDING_JOBS", str(defaults.max_pending_jobs))
            ),
            max_retained_jobs=int(
                os.getenv("MEDIA_CONVERTER_MAX_RETAINED_JOBS", str(defaults.max_retained_jobs))
            ),
            max_duration_seconds=int(
                os.getenv(
                    "MEDIA_CONVERTER_MAX_DURATION_SECONDS",
                    str(defaults.max_duration_seconds),
                )
            ),
            max_video_pixels=int(
                os.getenv(
                    "MEDIA_CONVERTER_MAX_VIDEO_PIXELS",
                    str(defaults.max_video_pixels),
                )
            ),
        )


settings = Settings.from_env()
