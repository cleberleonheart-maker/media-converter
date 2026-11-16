from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .config import Settings
from .jobs import Job, JobStatus

logger = logging.getLogger(__name__)


class PushService:
    """Envia notificações Web Push para os dispositivos assinados."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self.path = config.data_dir / "push-subscriptions.json"
        self.enabled = bool(config.vapid_public_key and config.vapid_private_key)

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [
            item
            for item in data
            if isinstance(item, dict) and isinstance(item.get("endpoint"), str) and item.get("keys")
        ]

    def _write(self, subscriptions: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(subscriptions, indent=2), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def subscribe(self, subscription: dict[str, Any]) -> None:
        endpoint = subscription.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("Assinatura de notificação inválida.")
        subscriptions = [item for item in self._read() if item.get("endpoint") != endpoint]
        subscriptions.append(subscription)
        self._write(subscriptions)

    def unsubscribe(self, endpoint: str) -> None:
        subscriptions = [item for item in self._read() if item.get("endpoint") != endpoint]
        self._write(subscriptions)

    def _send_sync(self, subscriptions: list[dict[str, Any]], payload: dict[str, Any]) -> list[str]:
        from pywebpush import WebPushException, webpush

        expired: list[str] = []
        body = json.dumps(payload)
        for subscription in subscriptions:
            endpoint = str(subscription.get("endpoint", ""))
            try:
                webpush(
                    subscription_info=subscription,
                    data=body,
                    vapid_private_key=self.config.vapid_private_key,
                    vapid_claims={"sub": self.config.vapid_subject},
                    ttl=3600,
                    timeout=10,
                )
            except WebPushException as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in {404, 410}:
                    expired.append(endpoint)
                    continue
                logger.warning("Falha ao enviar notificação push: %s", exc)
            except Exception as exc:
                logger.warning("Falha inesperada ao enviar notificação push: %s", exc)
        return expired

    async def send(self, title: str, body: str, url: str = "/") -> None:
        if not self.enabled:
            return
        subscriptions = self._read()
        if not subscriptions:
            return
        payload = {"title": title, "body": body, "url": url}
        expired = await asyncio.to_thread(self._send_sync, subscriptions, payload)
        if expired:
            remaining = [
                item for item in self._read() if str(item.get("endpoint", "")) not in set(expired)
            ]
            self._write(remaining)

    async def notify_job(self, job: Job) -> None:
        if job.status == JobStatus.COMPLETED:
            await self.send(
                "Conversão concluída",
                f"{job.source_name} está pronto para download.",
                f"/?job={job.id}",
            )
        elif job.status == JobStatus.FAILED:
            await self.send(
                "Falha na conversão",
                f"{job.source_name}: {job.error or 'erro inesperado'}",
                f"/?job={job.id}",
            )
