from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import JobStatus, JobStore
from app.main import build_app
from app.media import OutputFormat, Quality
from app.push import PushService


@pytest.fixture
def config() -> Settings:
    with tempfile.TemporaryDirectory() as directory:
        yield Settings(
            data_dir=Path(directory),
            trusted_hosts=("testserver",),
            google_client_id="client-id",
            session_secret="segredo",
            vapid_public_key="public-key",
            vapid_private_key="private-key",
        )


def test_push_key_is_public(config: Settings) -> None:
    with TestClient(build_app(config)) as client:
        response = client.get("/api/push/key")

    assert response.status_code == 200
    assert response.json() == {"enabled": True, "public_key": "public-key"}


def test_push_subscribe_requires_session(config: Settings) -> None:
    with TestClient(build_app(config)) as client:
        response = client.post(
            "/api/push/subscribe",
            json={"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}},
        )

    assert response.status_code == 401


def test_push_subscribe_stores_subscription(config: Settings) -> None:
    with TestClient(build_app(config)) as client:
        response = client.post(
            "/api/push/subscribe",
            json={"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}},
            cookies={"mc_session": _session_token(config)},
        )

        assert response.status_code == 201

        stored = PushService(config)._read()
        assert len(stored) == 1
        assert stored[0]["endpoint"] == "https://push.example/1"


def test_push_unsubscribe_removes_subscription(config: Settings) -> None:
    push = PushService(config)
    push.subscribe({"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}})

    with TestClient(build_app(config)) as client:
        response = client.post(
            "/api/push/unsubscribe",
            json={"endpoint": "https://push.example/1"},
            cookies={"mc_session": _session_token(config)},
        )

        assert response.status_code == 200
        assert push._read() == []


def test_push_service_sends_and_removes_expired(
    config: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    push = PushService(config)
    push.subscribe({"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}})
    sent: list[dict[str, object]] = []

    def fake_send_sync(
        subscriptions: list[dict[str, object]], payload: dict[str, object]
    ) -> list[str]:
        sent.append(payload)
        return [str(subscriptions[0]["endpoint"])]

    monkeypatch.setattr(push, "_send_sync", fake_send_sync)

    import asyncio

    asyncio.run(push.send("Título", "Corpo", "/?job=1"))

    assert sent and sent[0]["title"] == "Título"
    assert push._read() == []


def test_push_service_disabled_without_keys(config: Settings) -> None:
    config_without_push = Settings(
        data_dir=config.data_dir,
        trusted_hosts=("testserver",),
    )
    push = PushService(config_without_push)

    assert push.enabled is False


@pytest.mark.asyncio
async def test_job_completion_triggers_notification(config: Settings) -> None:
    notified: list[str] = []

    async def fake_notify(job) -> None:
        notified.append(job.status)

    store = JobStore(config)
    store.on_finished = lambda job: asyncio.get_running_loop().create_task(fake_notify(job))
    job = store.create("url", "video.mp4", OutputFormat.MP4, Quality.ORIGINAL)

    store.update(job.id, status=JobStatus.COMPLETED)
    await asyncio.sleep(0)

    assert notified == [JobStatus.COMPLETED]


def _session_token(config: Settings) -> str:
    from app.auth import create_session_token

    return create_session_token("user@example.com", config.session_secret, 60)
