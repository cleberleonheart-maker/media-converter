from __future__ import annotations

import asyncio
import socket
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import create_session_token
from app.config import Settings
from app.main import build_app
from app.media import OutputFormat, Quality, sanitize_filename
from app.url_validation import (
    _RESOLVER_MAX_CONCURRENT,
    _RESOLVER_SLOTS,
    ResolvedUrl,
    ResolverCapacityError,
    UrlValidationError,
    http_error_for_url,
    is_private_address,
    pinned_connection,
    resolve_public_url,
    resolve_public_url_async,
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


def test_basic_auth_requires_credentials() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(
            data_dir=Path(directory),
            trusted_hosts=("testserver",),
            auth_user="admin",
            auth_password="s3cret",
        )
        app = build_app(config)
        with TestClient(app) as test_client:
            unauthorized = test_client.get("/api/jobs")
            assert unauthorized.status_code == 401

            authorized = test_client.get("/api/jobs", auth=("admin", "s3cret"))
            assert authorized.status_code == 200

            wrong = test_client.get("/api/jobs", auth=("admin", "errada"))
            assert wrong.status_code == 401

            health = test_client.get("/api/health")
            assert health.status_code == 200


def test_auth_requires_user_and_password() -> None:
    with (
        tempfile.TemporaryDirectory() as directory,
        pytest.raises(ValueError, match="usuário e senha"),
    ):
        Settings(data_dir=Path(directory), auth_user="admin")


def test_auth_config_reports_google_disabled() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(data_dir=Path(directory), trusted_hosts=("testserver",))
        app = build_app(config)
        with TestClient(app) as test_client:
            response = test_client.get("/api/auth/config")

        assert response.status_code == 200
        assert response.json()["google_enabled"] is False


def test_session_cookie_grants_access() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Settings(
            data_dir=Path(directory),
            trusted_hosts=("testserver",),
            google_client_id="client-id",
            session_secret="segredo",
        )
        app = build_app(config)
        with TestClient(app) as test_client:
            unauthorized = test_client.get("/api/jobs")
            assert unauthorized.status_code == 401

            token = create_session_token("user@example.com", "segredo", 60)
            authorized = test_client.get("/api/jobs", cookies={"mc_session": token})
            assert authorized.status_code == 200

            logout = test_client.post("/api/auth/logout")
            assert logout.status_code == 200


def test_google_login_creates_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify(credential: str, client_id: str, allowed_domains: tuple[str, ...]) -> str:
        assert credential == "credencial"
        assert client_id == "client-id"
        return "user@example.com"

    monkeypatch.setattr("app.main.verify_google_credential", fake_verify)

    with tempfile.TemporaryDirectory() as directory:
        config = Settings(
            data_dir=Path(directory),
            trusted_hosts=("testserver",),
            google_client_id="client-id",
            session_secret="segredo",
        )
        app = build_app(config)
        with TestClient(app) as test_client:
            response = test_client.post(
                "/api/auth/google",
                json={"credential": "credencial"},
            )

            assert response.status_code == 200
            assert response.json()["email"] == "user@example.com"
            assert "mc_session" in response.cookies

            jobs = test_client.get("/api/jobs")
            assert jobs.status_code == 200


def test_google_login_rate_limit_blocks_repeated_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.auth import AuthError

    async def fake_verify(credential: str, client_id: str, allowed_domains: tuple[str, ...]) -> str:
        raise AuthError("Credencial inválida.")

    monkeypatch.setattr("app.main.verify_google_credential", fake_verify)

    with tempfile.TemporaryDirectory() as directory:
        config = Settings(
            data_dir=Path(directory),
            trusted_hosts=("testserver",),
            google_client_id="client-id",
            session_secret="segredo",
            login_max_failures=3,
        )
        app = build_app(config)
        with TestClient(app) as test_client:
            for _ in range(3):
                failed = test_client.post("/api/auth/google", json={"credential": "x"})
                assert failed.status_code == 401

            blocked = test_client.post("/api/auth/google", json={"credential": "x"})

            assert blocked.status_code == 429


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


@pytest.mark.asyncio
async def test_resolver_rejects_requests_when_capacity_is_exhausted() -> None:
    acquired = 0
    try:
        for _ in range(_RESOLVER_MAX_CONCURRENT):
            assert _RESOLVER_SLOTS.acquire(blocking=False)
            acquired += 1
        with pytest.raises(ResolverCapacityError, match="ocupada") as exc_info:
            await resolve_public_url_async("https://example.com/video.mp4", Settings())
        response = http_error_for_url(exc_info.value)
        assert response.status_code == 503
        assert response.headers == {"Retry-After": "1"}
    finally:
        for _ in range(acquired):
            _RESOLVER_SLOTS.release()


@pytest.mark.asyncio
async def test_async_resolver_returns_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.url_validation._RESOLVER_SCRIPT",
        'print(\'["93.184.216.34", "2606:4700::1"]\')',
    )

    resolved = await resolve_public_url_async("https://example.com/video.mp4", Settings())

    assert resolved.addresses == ("93.184.216.34", "2606:4700::1")


@pytest.mark.asyncio
async def test_resolver_timeout_releases_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.url_validation._RESOLVER_SCRIPT", "import time\ntime.sleep(1)")
    monkeypatch.setattr("app.url_validation._RESOLVER_TIMEOUT_SECONDS", 0.01)

    available_before = _RESOLVER_SLOTS._value
    with pytest.raises(UrlValidationError, match="tempo limite"):
        await resolve_public_url_async("https://example.com/video.mp4", Settings())

    for _ in range(100):
        if _RESOLVER_SLOTS._value == available_before:
            break
        await asyncio.sleep(0.01)
    assert _RESOLVER_SLOTS._value == available_before


@pytest.mark.asyncio
async def test_resolver_cancellation_during_creation_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    killed = asyncio.Event()

    class FakeProcess:
        returncode: int | None = None

        def kill(self) -> None:
            self.returncode = -9
            killed.set()

        async def wait(self) -> int:
            return -9

    process = FakeProcess()
    available_before = _RESOLVER_SLOTS._value

    async def fake_create(*args: object, **kwargs: object) -> FakeProcess:
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr("app.url_validation.asyncio.create_subprocess_exec", fake_create)
    task = asyncio.create_task(
        resolve_public_url_async("https://example.com/video.mp4", Settings())
    )
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)

        assert _RESOLVER_SLOTS._value == available_before - 1

        release.set()
        await asyncio.wait_for(killed.wait(), timeout=0.5)
        for _ in range(100):
            if _RESOLVER_SLOTS._value == available_before:
                break
            await asyncio.sleep(0.01)
        assert _RESOLVER_SLOTS._value == available_before
    finally:
        release.set()


def test_sanitize_filename() -> None:
    assert sanitize_filename("../../movie.mp4") == "movie.mp4"
    assert sanitize_filename("meu vídeo.mp4") == "meu video.mp4"
    assert sanitize_filename("file.exe") == "file"


def test_formats_are_distinct() -> None:
    assert OutputFormat.MP4 != OutputFormat.WEBM
    assert Quality.ORIGINAL != Quality.HIGH
