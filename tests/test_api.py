from __future__ import annotations

import socket
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_app
from app.media import OutputFormat, Quality, sanitize_filename
from app.url_validation import (
    ResolvedUrl,
    UrlValidationError,
    is_private_address,
    pinned_connection,
    resolve_public_url,
    validate_media_url,
)


@pytest.fixture
def config() -> Settings:
    with tempfile.TemporaryDirectory() as directory:
        yield Settings(
            data_dir=Path(directory),
            max_upload_bytes=1024 * 1024,
            trusted_hosts=("testserver",),
        )


@pytest.fixture
def client(config: Settings):
    app = build_app(config)
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_rejects_untrusted_host(client: TestClient) -> None:
    response = client.get("/api/health", headers={"host": "attacker.example"})
    assert response.status_code == 400


def test_rejects_oversized_content_length(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/url",
        content=b"x" * ((64 * 1024) + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_rejects_oversized_url_json_under_root_path(config: Settings) -> None:
    with TestClient(build_app(config), root_path="/prefix") as test_client:
        response = test_client.post(
            "/prefix/api/jobs/url",
            content=b"x" * ((64 * 1024) + 1),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


def test_rejects_oversized_upload_body(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/upload",
        content=b"x" * ((2 * 1024 * 1024) + 1),
        headers={"content-type": "multipart/form-data; boundary=test"},
    )
    assert response.status_code == 413


def test_rejects_oversized_chunked_body(client: TestClient) -> None:
    def chunks():
        for _ in range(3):
            yield b"x" * (1024 * 1024)

    response = client.post(
        "/api/jobs/url",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_index_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Media Converter" in response.text


def test_rejects_jobs_when_pending_limit_is_reached(client: TestClient) -> None:
    store = client.app.state.job_store
    for index in range(store.config.max_pending_jobs):
        store.create("upload", f"movie-{index}.mp4", OutputFormat.MP4, Quality.ORIGINAL)

    response = client.post(
        "/api/jobs/url",
        json={"url": "https://www.youtube.com/watch?v=test", "output_format": "mp4"},
    )
    assert response.status_code == 429


def test_upload_creates_job(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/upload",
        files={"file": ("movie.mp4", b"video-data", "video/mp4")},
        data={"output_format": "mp4", "quality": "original"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] in {"queued", "converting", "failed"}
    assert payload["source"] == "upload"
    assert payload["source_name"] == "movie.mp4"


def test_upload_rejects_empty_file_name(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/upload",
        files={"file": ("", b"video-data", "video/mp4")},
        data={"output_format": "mp4", "quality": "original"},
    )
    assert response.status_code == 422


def test_upload_rejects_invalid_format(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/upload",
        files={"file": ("movie.mp4", b"video-data", "video/mp4")},
        data={"output_format": "avi", "quality": "original"},
    )
    assert response.status_code == 422


def test_url_job_rejects_local_address(config: Settings) -> None:
    with pytest.raises(UrlValidationError):
        validate_media_url("http://127.0.0.1/video.mp4", config)


def test_rejects_multicast_and_reserved_addresses() -> None:
    assert is_private_address("224.0.0.1")
    assert is_private_address("255.255.255.255")
    assert is_private_address("::1")
    assert not is_private_address("8.8.8.8")


def test_resolve_public_url_orders_ipv4_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.url_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )

    resolved = resolve_public_url("https://example.com/video.mp4", Settings())

    assert resolved.hostname == "example.com"
    assert resolved.port == 443
    assert resolved.addresses == ("93.184.216.34", "2606:4700::1")


def test_resolve_public_url_rejects_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.url_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ],
    )

    with pytest.raises(UrlValidationError):
        resolve_public_url("https://example.com/video.mp4", Settings())


def test_resolve_public_url_allows_private_dns_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.url_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ],
    )

    resolved = resolve_public_url(
        "https://example.com/video.mp4",
        Settings(allow_private_hosts=True),
    )

    assert resolved.addresses == ("10.0.0.5",)


def test_pinned_connection_preserves_original_host() -> None:
    resolved = ResolvedUrl(
        "https://example.com:8443/video.mp4?token=1",
        "example.com",
        8443,
        ("93.184.216.34",),
    )

    connect_url, host_header = pinned_connection(resolved, "93.184.216.34")

    assert connect_url == "https://93.184.216.34:8443/video.mp4?token=1"
    assert host_header == "example.com:8443"


def test_pinned_connection_supports_ipv6() -> None:
    resolved = ResolvedUrl(
        "https://[2001:db8::1]/video.mp4",
        "2001:db8::1",
        443,
        ("2606:4700::1",),
    )

    connect_url, host_header = pinned_connection(resolved, "2606:4700::1")

    assert connect_url == "https://[2606:4700::1]/video.mp4"
    assert host_header == "[2001:db8::1]"


def test_url_job_rejects_unknown_host(config: Settings) -> None:
    with pytest.raises(UrlValidationError):
        validate_media_url("https://example.com/watch", config)


def test_url_job_accepts_direct_media_on_unknown_host(config: Settings) -> None:
    validated_url = validate_media_url("https://example.com/video.mp4", config)
    assert validated_url.direct_media is True


def test_sanitize_filename() -> None:
    assert sanitize_filename("../../movie.mp4") == "movie.mp4"
    assert sanitize_filename("meu vídeo.mp4") == "meu video.mp4"
    assert sanitize_filename("file.exe") == "file"


def test_formats_are_distinct() -> None:
    assert OutputFormat.MP4 != OutputFormat.WEBM
    assert Quality.ORIGINAL != Quality.HIGH
