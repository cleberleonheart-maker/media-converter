from __future__ import annotations

import time

import httpx
import pytest

from app.auth import (
    AuthError,
    create_session_token,
    verify_google_credential,
    verify_session_token,
)


def test_session_token_roundtrip() -> None:
    token = create_session_token("user@example.com", "segredo", 60)

    session = verify_session_token(token, "segredo")

    assert session is not None
    assert session.email == "user@example.com"
    assert session.expires_at > int(time.time())


def test_session_token_rejects_wrong_secret() -> None:
    token = create_session_token("user@example.com", "segredo", 60)

    assert verify_session_token(token, "outro") is None


def test_session_token_rejects_tampering() -> None:
    token = create_session_token("user@example.com", "segredo", 60)
    payload, _, _ = token.partition(".")

    assert verify_session_token(f"{payload}x.{token.split('.', 1)[1]}", "segredo") is None
    assert verify_session_token("invalido", "segredo") is None


def test_session_token_rejects_expired() -> None:
    token = create_session_token("user@example.com", "segredo", -10)

    assert verify_session_token(token, "segredo") is None


def _tokeninfo_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "aud": "client-id",
        "iss": "https://accounts.google.com",
        "email": "User@Example.com",
        "email_verified": "true",
        "exp": int(time.time()) + 300,
    }
    payload.update(overrides)
    return payload


def _transport(payload: dict[str, object], status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "id_token" in request.url.params
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_verify_google_credential_accepts_valid_token() -> None:
    email = await verify_google_credential(
        "token",
        "client-id",
        transport=_transport(_tokeninfo_payload()),
    )

    assert email == "user@example.com"


@pytest.mark.asyncio
async def test_verify_google_credential_rejects_other_client() -> None:
    with pytest.raises(AuthError, match="outro aplicativo"):
        await verify_google_credential(
            "token",
            "client-id",
            transport=_transport(_tokeninfo_payload(aud="outro")),
        )


@pytest.mark.asyncio
async def test_verify_google_credential_rejects_unverified_email() -> None:
    with pytest.raises(AuthError, match="não está verificado"):
        await verify_google_credential(
            "token",
            "client-id",
            transport=_transport(_tokeninfo_payload(email_verified="false")),
        )


@pytest.mark.asyncio
async def test_verify_google_credential_rejects_expired_token() -> None:
    with pytest.raises(AuthError, match="expirada"):
        await verify_google_credential(
            "token",
            "client-id",
            transport=_transport(_tokeninfo_payload(exp=int(time.time()) - 10)),
        )


@pytest.mark.asyncio
async def test_verify_google_credential_rejects_domain_not_allowed() -> None:
    with pytest.raises(AuthError, match="não tem acesso"):
        await verify_google_credential(
            "token",
            "client-id",
            allowed_domains=("outraempresa.com",),
            transport=_transport(_tokeninfo_payload()),
        )


@pytest.mark.asyncio
async def test_verify_google_credential_accepts_allowed_domain() -> None:
    email = await verify_google_credential(
        "token",
        "client-id",
        allowed_domains=("example.com",),
        transport=_transport(_tokeninfo_payload()),
    )

    assert email == "user@example.com"


@pytest.mark.asyncio
async def test_verify_google_credential_rejects_invalid_response() -> None:
    with pytest.raises(AuthError, match="validar a credencial"):
        await verify_google_credential(
            "token",
            "client-id",
            transport=_transport({}, status_code=401),
        )
