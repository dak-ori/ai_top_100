import base64
import logging
import socket
import time

import httpx
import pytest

from app.media import MAX_IMAGE_BYTES, MediaError, load_image

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
URL = "https://img.example.com/photo.png"


def public_dns(host, port, *args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def dns_returning(*addresses):
    def resolve(host, port, *args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    return resolve


def serve(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped)), seen


def png_response(request):
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})


async def test_builds_anthropic_image_block():
    client, seen = serve(png_response)
    block = await load_image(client, URL + "?token=abc", getaddrinfo=public_dns)
    assert block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()},
    }
    assert len(seen) == 1


async def test_content_type_parameters_are_ignored():
    client, _ = serve(lambda r: httpx.Response(200, content=PNG, headers={"content-type": "Image/JPEG; charset=binary"}))
    assert (await load_image(client, URL, getaddrinfo=public_dns))["source"]["media_type"] == "image/jpeg"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://img.example.com/a.png", "scheme"),
        ("ftp://img.example.com/a.png", "scheme"),
        ("https://user@img.example.com/a.png", "userinfo"),
        ("https://img.example.com@127.0.0.1/a.png", "userinfo"),
        ("https://img.example.com:8443/a.png", "port"),
        ("https://localhost/a.png", "private-address"),
        ("https://LOCALHOST./a.png", "private-address"),
        ("https://app.localhost/a.png", "private-address"),
        ("https://127.0.0.1/a.png", "private-address"),
        ("https://10.0.0.5/a.png", "private-address"),
        ("https://192.168.1.1/a.png", "private-address"),
        ("https://172.16.0.1/a.png", "private-address"),
        ("https://169.254.169.254/latest/meta-data", "private-address"),
        ("https://100.64.0.1/a.png", "private-address"),
        ("https://0.0.0.0/a.png", "private-address"),
        ("https://[::1]/a.png", "private-address"),
        ("https://[fe80::1]/a.png", "private-address"),
        ("https://[fc00::1]/a.png", "private-address"),
        ("https://[::ffff:127.0.0.1]/a.png", "private-address"),
        ("https://[::ffff:10.0.0.1]/a.png", "private-address"),
        ("https:///a.png", "invalid-url"),
        ("https://[bad/a.png", "invalid-url"),
    ],
)
async def test_unsafe_urls_are_refused_without_a_request(url, code):
    client, seen = serve(png_response)
    with pytest.raises(MediaError) as info:
        await load_image(client, url, getaddrinfo=public_dns)
    assert info.value.code == code
    assert seen == []


@pytest.mark.parametrize("answers", [["10.0.0.7"], ["93.184.216.34", "127.0.0.1"], ["::1"], ["169.254.169.254"]])
async def test_host_resolving_to_a_private_address_is_refused(answers):
    client, seen = serve(png_response)
    with pytest.raises(MediaError) as info:
        await load_image(client, "https://cdn.attacker.example/a.png", getaddrinfo=dns_returning(*answers))
    assert info.value.code == "private-address" and seen == []


async def test_unicode_and_mixed_case_hosts_are_normalized_before_checks():
    client, seen = serve(png_response)
    hosts = []

    def resolve(host, port, *args):
        hosts.append(host)
        return dns_returning("10.0.0.1")(host, port)

    fullwidth_localhost = "".join(chr(ord(c) + 0xFEE0) for c in "LOCALHOST")
    with pytest.raises(MediaError) as info:
        await load_image(client, f"https://{fullwidth_localhost}/a.png", getaddrinfo=resolve)
    assert info.value.code == "private-address"

    with pytest.raises(MediaError):
        await load_image(client, "https://Intern" + chr(0xE4) + "L.example/a.png", getaddrinfo=resolve)
    assert hosts == ["xn--internl-bxa.example"] and seen == []


async def test_unresolvable_host_is_refused():
    def resolve(host, port, *args):
        raise socket.gaierror("no such host")

    client, seen = serve(png_response)
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=resolve)
    assert info.value.code == "dns" and seen == []


async def test_redirect_is_not_followed():
    client, seen = serve(lambda r: httpx.Response(302, headers={"location": "https://127.0.0.1/a.png"}))
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=public_dns)
    assert info.value.code == "redirect" and len(seen) == 1


async def test_oversized_body_is_stopped_while_streaming():
    chunks_read = 0

    async def body():
        nonlocal chunks_read
        for _ in range(100):
            chunks_read += 1
            yield b"x" * (MAX_IMAGE_BYTES // 4)

    client, _ = serve(lambda r: httpx.Response(200, content=body(), headers={"content-type": "image/png"}))
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=public_dns)
    assert info.value.code == "too-large" and chunks_read <= 6


async def test_declared_oversize_is_refused_without_reading():
    read = False

    async def body():
        nonlocal read
        read = True
        yield b"x"

    client, _ = serve(
        lambda r: httpx.Response(
            200, content=body(), headers={"content-type": "image/png", "content-length": str(MAX_IMAGE_BYTES + 1)}
        )
    )
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=public_dns)
    assert info.value.code == "too-large" and not read


async def test_exactly_the_limit_is_accepted():
    client, _ = serve(lambda r: httpx.Response(200, content=b"x" * MAX_IMAGE_BYTES, headers={"content-type": "image/gif"}))
    block = await load_image(client, URL, getaddrinfo=public_dns)
    assert block["source"]["media_type"] == "image/gif"


@pytest.mark.parametrize("content_type", ["text/html", "image/svg+xml", "application/octet-stream", ""])
async def test_wrong_media_type_is_refused(content_type):
    client, _ = serve(lambda r: httpx.Response(200, content=PNG, headers={"content-type": content_type}))
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=public_dns)
    assert info.value.code == "content-type"


async def test_error_status_and_network_failure_are_media_errors():
    client, _ = serve(lambda r: httpx.Response(404))
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=public_dns)
    assert info.value.code == "http-status"

    def boom(request):
        raise httpx.ConnectError("refused")

    client, _ = serve(boom)
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=public_dns)
    assert info.value.code == "network"


async def test_slow_dns_times_out(monkeypatch):
    monkeypatch.setattr("app.media.REQUEST_TIMEOUT_SECONDS", 0.05)

    def slow(host, port, *args):
        time.sleep(0.3)
        return public_dns(host, port)

    client, _ = serve(png_response)
    with pytest.raises(MediaError) as info:
        await load_image(client, URL, getaddrinfo=slow)
    assert info.value.code == "timeout"


async def test_logs_hold_no_query_or_token(caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = serve(lambda r: httpx.Response(302))
    with pytest.raises(MediaError):
        await load_image(client, "https://img.example.com/secret/path.png?token=SECRETTOKEN", getaddrinfo=public_dns)
    with pytest.raises(MediaError):
        await load_image(client, "http://img.example.com/x.png?token=SECRETTOKEN", getaddrinfo=public_dns)
    assert "img.example.com" in caplog.text
    assert "SECRETTOKEN" not in caplog.text and "secret/path" not in caplog.text
