import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.schemas import GNewsArticle

logger = logging.getLogger(__name__)

BASE_URL = "https://gnews.io/api/v4"
MAX_QUERY_LENGTH = 200  # GNews limit on `q`

# Free plan allows 1 request/second; retry 429 and transient 5xx with backoff.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GNewsError(Exception):
    """Base error for GNews failures."""


class GNewsQuotaExceeded(GNewsError):
    """HTTP 403: daily quota used up (resets 00:00 UTC). Retrying today is pointless."""


class GNewsClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
    ) -> None:
        self._api_key = api_key if api_key is not None else get_settings().gnews_api_key
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=20.0)
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GNewsClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def top_headlines(
        self,
        *,
        category: str = "general",
        lang: str | None = "en",
        country: str | None = None,
        max_results: int = 10,
        query: str | None = None,
    ) -> list[GNewsArticle]:
        params: dict[str, Any] = {"category": category, "max": max_results}
        if lang:
            params["lang"] = lang
        if country:
            params["country"] = country
        if query:
            params["q"] = self._check_query(query)
        return await self._fetch("/top-headlines", params)

    async def search(
        self,
        query: str,
        *,
        lang: str | None = "en",
        country: str | None = None,
        max_results: int = 10,
        sort_by: str = "publishedAt",
    ) -> list[GNewsArticle]:
        params: dict[str, Any] = {
            "q": self._check_query(query),
            "max": max_results,
            "sortby": sort_by,
        }
        if lang:
            params["lang"] = lang
        if country:
            params["country"] = country
        return await self._fetch("/search", params)

    @staticmethod
    def _check_query(query: str) -> str:
        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError(f"GNews query is {len(query)} chars; limit is {MAX_QUERY_LENGTH}")
        return query

    async def _fetch(self, path: str, params: dict[str, Any]) -> list[GNewsArticle]:
        if not self._api_key:
            raise GNewsError("GNEWS_API_KEY is not set")
        params = {**params, "apikey": self._api_key}

        last_error = "unknown error"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = f"network error: {exc!r}"
            else:
                if response.status_code == 200:
                    return self._parse(response)
                if response.status_code == 403:
                    raise GNewsQuotaExceeded(_error_text(response))
                if response.status_code not in RETRYABLE_STATUS:
                    # 400 (bad params / key) and 401 will not fix themselves.
                    raise GNewsError(f"HTTP {response.status_code}: {_error_text(response)}")
                last_error = f"HTTP {response.status_code}: {_error_text(response)}"

            if attempt < self._max_retries:
                delay = self._backoff * (2**attempt)
                logger.warning("GNews %s failed (%s); retrying in %.1fs", path, last_error, delay)
                await asyncio.sleep(delay)

        raise GNewsError(f"GNews {path} failed after retries: {last_error}")

    @staticmethod
    def _parse(response: httpx.Response) -> list[GNewsArticle]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GNewsError("GNews returned invalid JSON") from exc

        articles: list[GNewsArticle] = []
        for raw in payload.get("articles", []):
            try:
                articles.append(GNewsArticle.model_validate(raw))
            except ValueError:
                # Skip malformed entries (e.g. missing title/url) rather than fail the run.
                logger.warning("Skipping malformed GNews article: %r", raw.get("url"))
        return articles


def _error_text(response: httpx.Response) -> str:
    """GNews errors are {"errors": [..]} or {"errors": {"param": ".."}}."""
    try:
        errors = response.json().get("errors")
    except ValueError:
        return response.text[:200]
    if isinstance(errors, dict):
        return "; ".join(f"{key}: {value}" for key, value in errors.items())
    if isinstance(errors, list):
        return "; ".join(str(item) for item in errors)
    return response.text[:200]
