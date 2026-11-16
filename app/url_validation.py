from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import sys
import threading
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


class ResolverCapacityError(UrlValidationError):
    pass


_RESOLVER_MAX_CONCURRENT = 4
_RESOLVER_TIMEOUT_SECONDS = 10
_RESOLVER_SLOTS = threading.BoundedSemaphore(_RESOLVER_MAX_CONCURRENT)
_RESOLVER_CLEANUP_TASKS: set[asyncio.Task[None]] = set()
_RESOLVER_SCRIPT = """
import json
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
print(json.dumps(addresses))
"""


async def _terminate_resolver_process(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


def _release_resolver_slot_after_cleanup(task: asyncio.Task[None]) -> None:
    _RESOLVER_CLEANUP_TASKS.discard(task)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()
    _RESOLVER_SLOTS.release()


def _kill_resolver_process_when_ready(
    task: asyncio.Task[asyncio.subprocess.Process],
) -> None:
    if task.cancelled():
        _RESOLVER_SLOTS.release()
        return
    try:
        process = task.result()
    except Exception:
        _RESOLVER_SLOTS.release()
        return
    cleanup_task = asyncio.create_task(_terminate_resolver_process(process))
    _RESOLVER_CLEANUP_TASKS.add(cleanup_task)
    cleanup_task.add_done_callback(_release_resolver_slot_after_cleanup)


async def _resolve_addresses_async(hostname: str, port: int) -> tuple[str, ...]:
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise ResolverCapacityError("A validação de links está temporariamente ocupada.")
    release_slot = True
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RESOLVER_TIMEOUT_SECONDS
        creation_task: asyncio.Task[asyncio.subprocess.Process] = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _RESOLVER_SCRIPT,
                hostname,
                str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            process = await asyncio.wait_for(
                asyncio.shield(creation_task),
                timeout=remaining,
            )
        except TimeoutError as exc:
            release_slot = False
            creation_task.add_done_callback(_kill_resolver_process_when_ready)
            raise UrlValidationError("A validação do link excedeu o tempo limite.") from exc
        except asyncio.CancelledError:
            release_slot = False
            creation_task.add_done_callback(_kill_resolver_process_when_ready)
            raise

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=remaining,
            )
        except TimeoutError as exc:
            await _terminate_resolver_process(process)
            raise UrlValidationError("A validação do link excedeu o tempo limite.") from exc
        except asyncio.CancelledError:
            await _terminate_resolver_process(process)
            raise

        if process.returncode != 0:
            raise UrlValidationError("Não foi possível resolver o endereço do link.")
        try:
            raw_addresses = json.loads(stdout)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise UrlValidationError("Não foi possível resolver o endereço do link.") from exc
        addresses = (
            {str(address) for address in raw_addresses if isinstance(address, str)}
            if isinstance(raw_addresses, list)
            else set()
        )
        if not addresses:
            raise UrlValidationError("O endereço do link não foi encontrado.")
        return tuple(sorted(addresses, key=lambda address: (":" in address, address)))
    finally:
        if release_slot:
            _RESOLVER_SLOTS.release()


async def shutdown_resolvers() -> None:
    cleanup_tasks = list(_RESOLVER_CLEANUP_TASKS)
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)


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


def _reject_local_hostname(hostname: str, allow_private_hosts: bool) -> None:
    if not allow_private_hosts and (hostname == "localhost" or hostname.endswith(".localhost")):
        raise UrlValidationError("Hosts locais não são permitidos.")


def _validate_public_address(
    hostname: str,
    port: int,
    allow_private_hosts: bool,
) -> tuple[str, ...]:
    _reject_local_hostname(hostname, allow_private_hosts)
    addresses = _resolve_addresses(hostname, port)
    if not allow_private_hosts and any(is_private_address(address) for address in addresses):
        raise UrlValidationError("Endereços privados ou locais não são permitidos.")
    return addresses


async def _validate_public_address_async(
    hostname: str,
    port: int,
    allow_private_hosts: bool,
) -> tuple[str, ...]:
    _reject_local_hostname(hostname, allow_private_hosts)
    addresses = await _resolve_addresses_async(hostname, port)
    if not allow_private_hosts and any(is_private_address(address) for address in addresses):
        raise UrlValidationError("Endereços privados ou locais não são permitidos.")
    return addresses


def _build_validated_url(
    normalized: str,
    hostname: str,
    config: Settings,
) -> ValidatedUrl:
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


def validate_media_url(url: str, config: Settings) -> ValidatedUrl:
    normalized, hostname, port = _parse_url(url)
    _validate_public_address(hostname, port, config.allow_private_hosts)
    return _build_validated_url(normalized, hostname, config)


async def validate_media_url_async(url: str, config: Settings) -> ValidatedUrl:
    normalized, hostname, port = _parse_url(url)
    await _validate_public_address_async(hostname, port, config.allow_private_hosts)
    return _build_validated_url(normalized, hostname, config)


def resolve_public_url(url: str, config: Settings) -> ResolvedUrl:
    normalized, hostname, port = _parse_url(url)
    addresses = _validate_public_address(hostname, port, config.allow_private_hosts)
    return ResolvedUrl(url=normalized, hostname=hostname, port=port, addresses=addresses)


async def resolve_public_url_async(url: str, config: Settings) -> ResolvedUrl:
    normalized, hostname, port = _parse_url(url)
    addresses = await _validate_public_address_async(
        hostname,
        port,
        config.allow_private_hosts,
    )
    return ResolvedUrl(url=normalized, hostname=hostname, port=port, addresses=addresses)


def pinned_connection(resolved: ResolvedUrl, address: str) -> tuple[str, str]:
    origin = httpx.URL(resolved.url)
    connect_url = str(origin.copy_with(host=address))
    return connect_url, origin.netloc.decode("ascii")


def http_error_for_url(error: UrlValidationError) -> HTTPException:
    if isinstance(error, ResolverCapacityError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
            headers={"Retry-After": "1"},
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=str(error),
    )
