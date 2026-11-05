import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# kbNews (multer) accepts image/* only and rejects files over 5 MB with a 500, so check up front.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT = 15.0

# Some publishers block unknown clients; a generic browser-style agent avoids most of that.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; news-generator/0.1)",
    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
}


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes
    content_type: str
    filename: str


def sniff_image_type(data: bytes) -> tuple[str, str] | None:
    """Return (content_type, extension) from the file's magic bytes, or None if not an image.

    The server's Content-Type header is not trusted: error pages and redirects often claim image/*.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


async def download_image(
    url: str | None,
    *,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> DownloadedImage | None:
    """Download an image for upload to kbNews. Returns None on any problem; never raises.

    A missing image should cost a post its picture, not the whole post.
    """
    if not url or urlparse(url).scheme not in {"http", "https"}:
        return None

    owns_client = client is None
    http = client or httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT, follow_redirects=True, headers=_HEADERS
    )
    try:
        async with http.stream("GET", url) as response:
            if response.status_code != 200:
                logger.info("Image download %s returned HTTP %s", url, response.status_code)
                return None

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                logger.info("Image %s is too large (%s bytes)", url, declared)
                return None

            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    logger.info("Image %s exceeded %d bytes while downloading", url, max_bytes)
                    return None
    except httpx.HTTPError as exc:
        logger.info("Image download %s failed: %r", url, exc)
        return None
    finally:
        if owns_client:
            await http.aclose()

    sniffed = sniff_image_type(bytes(data))
    if sniffed is None:
        logger.info("Image %s is not a supported image type", url)
        return None
    content_type, extension = sniffed
    return DownloadedImage(bytes(data), content_type, f"cover.{extension}")
