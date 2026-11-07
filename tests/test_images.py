import httpx

from app.clients.images import download_image, sniff_image_type

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_sniff_recognises_supported_types():
    assert sniff_image_type(PNG) == ("image/png", "png")
    assert sniff_image_type(JPEG) == ("image/jpeg", "jpg")
    assert sniff_image_type(GIF) == ("image/gif", "gif")
    assert sniff_image_type(WEBP) == ("image/webp", "webp")
    assert sniff_image_type(b"<html>not an image</html>") is None
    assert sniff_image_type(b"") is None


async def test_downloads_valid_image_and_uses_sniffed_type():
    # Server says text/html but the bytes are a PNG: trust the bytes.
    def handler(request):
        return httpx.Response(200, content=PNG, headers={"content-type": "text/html"})

    image = await download_image("https://img.example.com/a", client=client_for(handler))
    assert image is not None
    assert image.content == PNG
    assert image.content_type == "image/png"
    assert image.filename == "cover.png"


async def test_html_error_page_with_image_content_type_is_rejected():
    def handler(request):
        return httpx.Response(
            200, content=b"<html>blocked</html>", headers={"content-type": "image/jpeg"}
        )

    assert await download_image("https://img.example.com/a", client=client_for(handler)) is None


async def test_non_200_returns_none():
    def handler(request):
        return httpx.Response(403)

    assert await download_image("https://img.example.com/a", client=client_for(handler)) is None


async def test_declared_content_length_over_limit_returns_none():
    def handler(request):
        return httpx.Response(200, content=PNG, headers={"content-length": "999999"})

    assert await download_image("https://i.example.com/a", client=client_for(handler), max_bytes=1000) is None


async def test_streamed_body_over_limit_returns_none():
    big = PNG + b"\x00" * 5000

    def handler(request):
        return httpx.Response(200, content=big)

    assert await download_image("https://i.example.com/a", client=client_for(handler), max_bytes=1000) is None


async def test_network_error_returns_none():
    def handler(request):
        raise httpx.ConnectError("boom")

    assert await download_image("https://i.example.com/a", client=client_for(handler)) is None


async def test_bad_or_missing_url_returns_none_without_requesting():
    def handler(request):
        raise AssertionError("no request should be made")

    client = client_for(handler)
    assert await download_image(None, client=client) is None
    assert await download_image("", client=client) is None
    assert await download_image("file:///etc/passwd", client=client) is None
    assert await download_image("ftp://example.com/a.png", client=client) is None


async def test_request_does_not_offer_avif_and_sends_a_user_agent():
    # Offering AVIF made image CDNs return AVIF, which we then rejected (live logs, 7 Nov).
    seen = {}

    def handler(request):
        seen["accept"] = request.headers["accept"]
        seen["agent"] = request.headers["user-agent"]
        return httpx.Response(200, content=PNG)

    await download_image("https://img.example.com/a", client=client_for(handler))

    assert "avif" not in seen["accept"].lower()
    assert "image/jpeg" in seen["accept"] and "image/png" in seen["accept"]
    assert seen["agent"].startswith("Mozilla/5.0")


async def test_unsupported_image_logs_its_first_bytes(caplog):
    avif = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 16

    def handler(request):
        return httpx.Response(200, content=avif, headers={"content-type": "image/avif"})

    with caplog.at_level("INFO", logger="app.clients.images"):
        result = await download_image("https://img.example.com/a", client=client_for(handler))

    assert result is None
    assert "ftypavif" in caplog.text
