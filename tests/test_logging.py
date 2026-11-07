import logging

import httpx

import app.main  # noqa: F401  (importing configures logging)


async def test_httpx_request_urls_are_not_logged_so_api_keys_stay_out_of_logs(caplog):
    # GNews takes its key as ?apikey=..., and httpx's INFO log prints the whole URL.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as client:
        with caplog.at_level(logging.INFO):
            await client.get("https://gnews.io/api/v4/search?q=x&apikey=SECRET-KEY-123")

    assert "SECRET-KEY-123" not in caplog.text
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
