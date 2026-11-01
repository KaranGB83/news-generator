import httpx
import pytest
from pydantic import BaseModel

from app.clients.gemini import GeminiBlocked, GeminiClient, GeminiError


class Answer(BaseModel):
    title: str
    score: int


def reply(text: str, **extra) -> dict:
    candidate = {"content": {"parts": [{"text": text}]}, "finishReason": "STOP", **extra}
    return {"candidates": [candidate]}


def make_client(handler, **kwargs) -> GeminiClient:
    http = httpx.AsyncClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        transport=httpx.MockTransport(handler),
    )
    return GeminiClient("test-key", "test-model", client=http, backoff_seconds=0, **kwargs)


async def test_request_shape_and_parsed_result():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=reply('{"title": "Hi", "score": 3}'))

    result = await make_client(handler).generate_json(
        "Say hi", Answer, system_instruction="Be brief", temperature=0.5
    )

    assert result == Answer(title="Hi", score=3)
    assert seen["path"] == "/v1beta/models/test-model:generateContent"
    assert seen["key"] == "test-key"
    assert seen["query"] == {}  # key must not leak into the URL
    assert '"responseMimeType":"application/json"' in seen["body"].replace(" ", "")
    assert "Be brief" in seen["body"] and "Say hi" in seen["body"]


async def test_code_fenced_json_is_accepted():
    def handler(request):
        return httpx.Response(200, json=reply('```json\n{"title": "A", "score": 1}\n```'))

    assert (await make_client(handler).generate_json("p", Answer)).title == "A"


async def test_thought_parts_are_ignored():
    def handler(request):
        parts = [
            {"text": "let me think...", "thought": True},
            {"text": '{"title": "B", "score": 2}'},
        ]
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
        )

    assert (await make_client(handler).generate_json("p", Answer)).score == 2


async def test_invalid_json_is_retried_then_succeeds():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        text = "not json" if calls == 1 else '{"title": "C", "score": 5}'
        return httpx.Response(200, json=reply(text))

    result = await make_client(handler).generate_json("p", Answer)
    assert calls == 2
    assert result.title == "C"


async def test_invalid_json_gives_up_after_validation_retries():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=reply('{"title": "missing score"}'))

    with pytest.raises(GeminiError, match="did not match the schema"):
        await make_client(handler, validation_retries=1).generate_json("p", Answer)
    assert calls == 2


async def test_429_is_retried_then_succeeds():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(
                429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "slow"}}
            )
        return httpx.Response(200, json=reply('{"title": "D", "score": 1}'))

    result = await make_client(handler).generate_json("p", Answer)
    assert calls == 3
    assert result.title == "D"


async def test_400_is_not_retried_and_message_is_surfaced():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            400, json={"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid"}}
        )

    with pytest.raises(GeminiError, match="INVALID_ARGUMENT: API key not valid"):
        await make_client(handler).generate_json("p", Answer)
    assert calls == 1


async def test_gives_up_after_max_retries():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(GeminiError, match="after retries"):
        await make_client(handler, max_retries=2).generate_json("p", Answer)
    assert calls == 3


async def test_network_error_is_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=reply('{"title": "E", "score": 1}'))

    assert (await make_client(handler).generate_json("p", Answer)).title == "E"
    assert calls == 2


async def test_prompt_block_raises_blocked_without_retry():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(GeminiBlocked, match="SAFETY"):
        await make_client(handler).generate_json("p", Answer)
    assert calls == 1


async def test_answer_block_raises_blocked():
    def handler(request):
        return httpx.Response(
            200, json={"candidates": [{"finishReason": "PROHIBITED_CONTENT", "content": {}}]}
        )

    with pytest.raises(GeminiBlocked, match="PROHIBITED_CONTENT"):
        await make_client(handler).generate_json("p", Answer)


async def test_max_tokens_and_empty_answers_raise():
    def truncated(request):
        return httpx.Response(200, json=reply('{"title": "cut', finishReason="MAX_TOKENS"))

    with pytest.raises(GeminiError, match="token limit"):
        await make_client(truncated).generate_json("p", Answer)

    def empty(request):
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {}}]})

    with pytest.raises(GeminiError, match="empty answer"):
        await make_client(empty).generate_json("p", Answer)

    def no_candidates(request):
        return httpx.Response(200, json={"candidates": []})

    with pytest.raises(GeminiError, match="no candidates"):
        await make_client(no_candidates).generate_json("p", Answer)


async def test_missing_api_key_raises():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(GeminiError, match="GEMINI_API_KEY"):
        await GeminiClient("", "m", client=http).generate_json("p", Answer)
