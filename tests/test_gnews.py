import httpx
import pytest

from app.clients.gnews import GNewsClient, GNewsError, GNewsQuotaExceeded

ARTICLE = {
    "id": "abc",
    "title": "Big story",
    "description": "Short summary",
    "content": "Truncated body... [1000 chars]",
    "url": "https://example.com/big-story",
    "image": "https://example.com/img.jpg",
    "publishedAt": "2026-11-01T05:30:00Z",
    "lang": "en",
    "source": {"id": "s1", "name": "Example News", "url": "https://example.com"},
}


def make_client(handler, **kwargs) -> GNewsClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="https://gnews.io/api/v4", transport=transport)
    return GNewsClient("test-key", client=http, backoff_seconds=0, **kwargs)


async def test_top_headlines_sends_expected_params_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"totalArticles": 1, "articles": [ARTICLE]})

    client = make_client(handler)
    articles = await client.top_headlines(category="world", lang="en", country="in", max_results=5)

    assert seen["path"] == "/api/v4/top-headlines"
    assert seen["params"] == {
        "category": "world",
        "max": "5",
        "lang": "en",
        "country": "in",
        "apikey": "test-key",
    }
    assert len(articles) == 1
    article = articles[0]
    assert article.title == "Big story"
    assert article.source.name == "Example News"
    assert article.published_at is not None and article.published_at.year == 2026


async def test_search_sends_query_and_sort():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"articles": []})

    client = make_client(handler)
    result = await client.search("Maharashtra OR Mumbai OR Pune", country="in")

    assert result == []
    assert seen["path"] == "/api/v4/search"
    assert seen["params"]["q"] == "Maharashtra OR Mumbai OR Pune"
    assert seen["params"]["sortby"] == "publishedAt"
    assert seen["params"]["country"] == "in"


async def test_optional_params_omitted():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"articles": []})

    await make_client(handler).top_headlines(lang=None)
    assert "lang" not in seen["params"]
    assert "country" not in seen["params"]
    assert "q" not in seen["params"]


async def test_malformed_article_is_skipped():
    bad = {"description": "no title or url"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"articles": [bad, ARTICLE]})

    articles = await make_client(handler).top_headlines()
    assert [a.title for a in articles] == ["Big story"]


async def test_quota_exceeded_raises_without_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"errors": ["You have reached your request limit"]})

    with pytest.raises(GNewsQuotaExceeded, match="request limit"):
        await make_client(handler).top_headlines()
    assert calls == 1


async def test_bad_request_raises_without_retry_and_formats_dict_errors():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"errors": {"q": "invalid syntax"}})

    with pytest.raises(GNewsError, match="q: invalid syntax"):
        await make_client(handler).search("bad")
    assert calls == 1


async def test_retries_429_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"errors": ["too many requests"]})
        return httpx.Response(200, json={"articles": [ARTICLE]})

    articles = await make_client(handler).top_headlines()
    assert calls == 3
    assert len(articles) == 1


async def test_gives_up_after_max_retries():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"errors": ["maintenance"]})

    with pytest.raises(GNewsError, match="after retries"):
        await make_client(handler, max_retries=2).top_headlines()
    assert calls == 3


async def test_network_error_is_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"articles": [ARTICLE]})

    articles = await make_client(handler).top_headlines()
    assert calls == 2
    assert len(articles) == 1


async def test_missing_api_key_raises():
    client = GNewsClient("", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)))
    with pytest.raises(GNewsError, match="GNEWS_API_KEY"):
        await client.top_headlines()


async def test_query_over_200_chars_rejected():
    client = make_client(lambda r: httpx.Response(200, json={"articles": []}))
    with pytest.raises(ValueError, match="limit is 200"):
        await client.search("x" * 201)
