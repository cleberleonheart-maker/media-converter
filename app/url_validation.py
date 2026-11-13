from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
import idna
from fastapi import HTTPException, status

from .config import Settings
from .media import ALLOWED_EXTENSIONS


class UrlValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    hostname: str
    direct_media: bool


@dataclass(frozen=True)
class ResolvedUrl:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def is_private_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_link_local
        or getattr(address, "is_site_local", False)
    )


def _hostname_matches(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    return any(hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts)


def _parse_url(url: str) -> tuple[str, str, int]:
    normalized = url.strip()
    if not normalized:
        raise UrlValidationError("Informe um link válido.")

    try:
        parsed = urlparse(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UrlValidationError("O link contém uma porta ou endereço inválido.") from exc

    if parsed.scheme not in {"http", "https"}:
        raise UrlValidationError("O link deve usar http:// ou https://.")
    if not hostname:
        raise UrlValidationError("O link não contém um endereço válido.")
    if parsed.username or parsed.password:
        raise UrlValidationError("O link não pode conter credenciais.")

    try:
        ipaddress.ip_address(hostname)
        ascii_hostname = hostname.lower()
    except ValueError:
        try:
            ascii_hostname = idna.encode(hostname, uts46=True).decode("ascii").lower().rstrip(".")
        except (idna.IDNAError, UnicodeError) as exc:
            raise UrlValidationError("O endereço do link é inválido.") from exc

    resolved_port = port or (443 if parsed.scheme == "https" else 80)
    return normalized, ascii_hostname, resolved_port


def _resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        addresses = {
            str(item[4][0]) for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise UrlValidationError("Não foi possível resolver o endereço do link.") from exc

    if not addresses:
        raise UrlValidationError("O endereço do link não foi encontrado.")

    return tuple(sorted(addresses, key=lambda address: (":" in address, address)))


def _validate_public_address(
    hostname: str,
    port: int,
    allow_private_hosts: bool,
) -> tuple[str, ...]:
    if not allow_private_hosts and (hostname == "localhost" or hostname.endswith(".localhost")):
        raise UrlValidationError("Hosts locais não são permitidos.")

    addresses = _resolve_addresses(hostname, port)
    if not allow_private_hosts and any(is_private_address(address) for address in addresses):
        raise UrlValidationError("Endereços privados ou locais não são permitidos.")
    return addresses


def validate_media_url(url: str, config: Settings) -> ValidatedUrl:
    normalized, hostname, port = _parse_url(url)
    _validate_public_address(hostname, port, config.allow_private_hosts)

    parsed = urlparse(normalized)
    path_suffix = Path(unquote(parsed.path)).suffix.lower()
    direct_media = path_suffix in ALLOWED_EXTENSIONS
    host_allowed = _hostname_matches(hostname, config.allowed_hosts)

    if not host_allowed and not direct_media:
        raise UrlValidationError(
            "Este host não está na lista de plataformas permitidas. "
            "Configure MEDIA_CONVERTER_ALLOWED_HOSTS para adicioná-lo."
        )

    return ValidatedUrl(url=normalized, hostname=hostname, direct_media=direct_media)


async def validate_media_url_async(url: str, config: Settings) -> ValidatedUrl:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(validate_media_url, url, config),
            timeout=10,
        )
    except TimeoutError as exc:
        raise UrlValidationError("A validação do link excedeu o tempo limite.") from exc


def resolve_public_url(url: str, config: Settings) -> ResolvedUrl:
    normalized, hostname, port = _parse_url(url)
    addresses = _validate_public_address(hostname, port, config.allow_private_hosts)
    return ResolvedUrl(url=normalized, hostname=hostname, port=port, addresses=addresses)


async def resolve_public_url_async(url: str, config: Settings) -> ResolvedUrl:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(resolve_public_url, url, config),
            timeout=10,
        )
    except TimeoutError as exc:
        raise UrlValidationError("A validação do link excedeu o tempo limite.") from exc


def pinned_connection(resolved: ResolvedUrl, address: str) -> tuple[str, str]:
    origin = httpx.URL(resolved.url)
    connect_url = str(origin.copy_with(host=address))
    return connect_url, origin.netloc.decode("ascii")


def http_error_for_url(error: UrlValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=str(error),
    )
