from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import httpx

SESSION_COOKIE_NAME = "mc_session"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


class AuthError(Exception):
    """Falha na validação de credenciais."""


@dataclass(frozen=True)
class Session:
    email: str
    expires_at: int


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64encode(digest)


def create_session_token(email: str, secret: str, ttl_seconds: int) -> str:
    payload = {"email": email, "exp": int(time.time()) + ttl_seconds}
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{encoded}.{_sign(encoded, secret)}"


def verify_session_token(token: str, secret: str) -> Session | None:
    encoded, separator, signature = token.partition(".")
    if not separator or not encoded or not signature:
        return None
    if not hmac.compare_digest(signature, _sign(encoded, secret)):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    email = payload.get("email")
    expires_at = payload.get("exp")
    if not isinstance(email, str) or not isinstance(expires_at, int):
        return None
    if expires_at <= int(time.time()):
        return None
    return Session(email=email, expires_at=expires_at)


async def verify_google_credential(
    credential: str,
    client_id: str,
    allowed_domains: tuple[str, ...] = (),
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        response = await client.get(GOOGLE_TOKENINFO_URL, params={"id_token": credential})
    if response.status_code != status_code_ok():
        raise AuthError("Não foi possível validar a credencial do Google.")
    try:
        data = response.json()
    except ValueError as exc:
        raise AuthError("Resposta inválida do Google.") from exc
    if not isinstance(data, dict):
        raise AuthError("Resposta inválida do Google.")
    if data.get("aud") != client_id:
        raise AuthError("Credencial emitida para outro aplicativo.")
    if data.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError("Emissor de credencial não reconhecido.")
    if str(data.get("email_verified")).lower() not in {"true", "1"}:
        raise AuthError("O e-mail da conta não está verificado.")
    try:
        expires_at = int(data.get("exp", 0))
    except (TypeError, ValueError) as exc:
        raise AuthError("Credencial inválida.") from exc
    if expires_at <= int(time.time()):
        raise AuthError("Credencial expirada.")
    email = str(data.get("email", "")).strip().lower()
    if not email:
        raise AuthError("Conta do Google sem e-mail.")
    if allowed_domains:
        domain = email.rsplit("@", 1)[-1]
        if domain not in allowed_domains:
            raise AuthError("Esta conta não tem acesso ao conversor.")
    return email


def status_code_ok() -> int:
    return 200
