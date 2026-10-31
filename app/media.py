"""Customer image download for the model, restricted to public https images."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, query string included, and image URLs can carry tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_IMAGE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 2.0
ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})

AUDIO_ONLY_REPLY = "음성은 지금 확인하기 어려워요. 내용을 글로 남겨 주시겠어요?"

GetAddrInfo = Callable[..., list[tuple]]


class MediaError(Exception):
    """An image was refused or could not be fetched. ``code`` is a stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def load_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    getaddrinfo: GetAddrInfo | None = None,
) -> dict:
    """Fetch ``url`` as an Anthropic base64 image block, or raise ``MediaError``.

    Only the host appears in logs; the query string and path can carry tokens.
    """
    host = "?"
    try:
        host = _host_of(url)
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            # Looked up per call so a replacement of socket.getaddrinfo takes effect.
            await _require_public_host(host, getaddrinfo or socket.getaddrinfo)
            media_type, data = await _download(client, url)
    except MediaError as exc:
        logger.warning("Image refused: host=%s reason=%s", host, exc.code)
        raise
    except TimeoutError:
        logger.warning("Image refused: host=%s reason=timeout", host)
        raise MediaError("timeout") from None
    except httpx.HTTPError as exc:
        logger.warning("Image refused: host=%s reason=network (%s)", host, type(exc).__name__)
        raise MediaError("network") from exc
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode("ascii")},
    }


def _host_of(url: str) -> str:
    """The normalized host of a plain https URL on the default port, else ``MediaError``."""
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise MediaError("invalid-url") from None
    if parts.scheme != "https":
        raise MediaError("scheme")
    if "@" in parts.netloc:
        raise MediaError("userinfo")
    if port not in (None, 443):
        raise MediaError("port")
    if not hostname:
        raise MediaError("invalid-url")
    try:
        # IDNA maps case and Unicode variants (e.g. fullwidth letters) to one ASCII form.
        host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise MediaError("invalid-host") from None
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise MediaError("private-address")
    return host


async def _require_public_host(host: str, getaddrinfo: GetAddrInfo) -> None:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().run_in_executor(
                None, lambda: getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
            )
        except OSError:
            raise MediaError("dns") from None
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not addresses:
        raise MediaError("dns")
    # Every resolved address must be public: one private record is enough to reach the inside.
    for address in addresses:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise MediaError("private-address")


async def _download(client: httpx.AsyncClient, url: str) -> tuple[str, bytes]:
    async with client.stream(
        "GET", url, follow_redirects=False, timeout=REQUEST_TIMEOUT_SECONDS, headers={"Accept": "image/*"}
    ) as response:
        if response.is_redirect:
            raise MediaError("redirect")
        if response.status_code != 200:
            raise MediaError("http-status")
        media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise MediaError("content-type")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            raise MediaError("too-large")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                raise MediaError("too-large")
    if not data:
        raise MediaError("empty")
    return media_type, bytes(data)
