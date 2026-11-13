from __future__ import annotations

import mimetypes
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class OutputFormat(StrEnum):
    MP4 = "mp4"
    WEBM = "webm"
    MKV = "mkv"
    MP3 = "mp3"
    M4A = "m4a"
    WAV = "wav"


class Quality(StrEnum):
    ORIGINAL = "original"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


AUDIO_FORMATS = {OutputFormat.MP3, OutputFormat.M4A, OutputFormat.WAV}
VIDEO_FORMATS = set(OutputFormat) - AUDIO_FORMATS
ALLOWED_EXTENSIONS = {
    ".3gp",
    ".aac",
    ".avi",
    ".flac",
    ".flv",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogg",
    ".opus",
    ".ts",
    ".wav",
    ".webm",
    ".wmv",
}
ALLOWED_INPUT_FORMATS = frozenset(
    {
        "3g2",
        "3gp",
        "aac",
        "ac3",
        "amr",
        "ape",
        "asf",
        "avi",
        "caf",
        "dv",
        "flac",
        "flv",
        "m4a",
        "matroska",
        "mj2",
        "mov",
        "mp4",
        "mpeg",
        "mpegts",
        "mpegvideo",
        "mka",
        "mp3",
        "mxf",
        "nut",
        "oga",
        "ogg",
        "ogv",
        "opus",
        "rm",
        "vob",
        "wav",
        "webm",
        "wma",
    }
)
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class MediaFormat:
    extension: str
    mime_type: str


FORMATS: dict[OutputFormat, MediaFormat] = {
    OutputFormat.MP4: MediaFormat("mp4", "video/mp4"),
    OutputFormat.WEBM: MediaFormat("webm", "video/webm"),
    OutputFormat.MKV: MediaFormat("mkv", "video/x-matroska"),
    OutputFormat.MP3: MediaFormat("mp3", "audio/mpeg"),
    OutputFormat.M4A: MediaFormat("m4a", "audio/mp4"),
    OutputFormat.WAV: MediaFormat("wav", "audio/wav"),
}


def sanitize_filename(filename: str, fallback: str = "video") -> str:
    name = Path(filename or fallback).name
    stem = Path(name).stem.strip() or fallback
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        suffix = ""
    normalized_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    clean_stem = SAFE_FILENAME.sub("_", normalized_stem).strip(" ._")[:120] or fallback
    return f"{clean_stem}{suffix}"


def extension_for_format(output_format: OutputFormat) -> str:
    return FORMATS[output_format].extension


def input_format_whitelist() -> str:
    return ",".join(sorted(ALLOWED_INPUT_FORMATS))


def detect_mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def ensure_binary(binary: str) -> str:
    resolved = shutil.which(binary)
    if not resolved:
        raise RuntimeError(f"Executável não encontrado: {binary}")
    return resolved


def atomic_output_path(directory: Path, stem: str, extension: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stem}.{extension}"


def temporary_output_path(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")


def safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def video_output_requires_video(output_format: OutputFormat) -> bool:
    return output_format in VIDEO_FORMATS


def stream_copy_compatible(
    output_format: OutputFormat,
    video_codec: str,
    audio_codec: str,
) -> bool:
    video = video_codec.lower()
    audio = audio_codec.lower()
    if output_format == OutputFormat.MP4:
        return video == "h264" and audio in {"", "aac", "mp3"}
    if output_format == OutputFormat.MKV:
        return video in {"h264", "vp8", "vp9", "av1"} and audio in {
            "",
            "aac",
            "mp3",
            "opus",
            "vorbis",
            "flac",
            "ac3",
        }
    if output_format == OutputFormat.WEBM:
        return video in {"vp8", "vp9", "av1"} and audio in {"", "opus", "vorbis"}
    if output_format == OutputFormat.MP3:
        return audio == "mp3"
    if output_format == OutputFormat.M4A:
        return audio == "aac"
    if output_format == OutputFormat.WAV:
        return audio.startswith("pcm_")
    return False


def codec_options(output_format: OutputFormat) -> list[str]:
    if output_format == OutputFormat.MP4:
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
    if output_format == OutputFormat.WEBM:
        return [
            "-c:v",
            "libvpx-vp9",
            "-crf",
            "32",
            "-b:v",
            "0",
            "-row-mt",
            "1",
            "-cpu-used",
            "4",
            "-c:a",
            "libopus",
            "-b:a",
            "160k",
        ]
    if output_format == OutputFormat.MKV:
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
    if output_format == OutputFormat.MP3:
        return ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
    if output_format == OutputFormat.M4A:
        return ["-vn", "-c:a", "aac", "-b:a", "192k"]
    return ["-vn", "-c:a", "pcm_s16le"]
