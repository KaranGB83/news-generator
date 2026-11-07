from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.clients.images import DownloadedImage
from app.clients.kbnews import (
    KbNewsAuthError,
    KbNewsClient,
    KbNewsError,
    KbNewsRejected,
    kbnews_category,
)
from app.models import Category

COOKIE = {"set-cookie": "token=jwt123; Path=/; HttpOnly"}
IMAGE = DownloadedImage(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/png", "cover.png")


class FakeKbNews:
    """Routes requests by (method, path) to queued responses and records every call."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    def queue(self, method: str, path: str, *responses):
        self.routes.setdefault((method, path), []).extend(responses)

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.calls if (r.method, r.url.path) == (method, path))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.bodies.append(request.read())
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            raise AssertionError(f"unexpected call {request.method} {request.url.path}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def login_ok() -> httpx.Response:
    return httpx.Response(200, json={"success": True}, headers=COOKIE)


def created(post_id="abc123", cover="https://res.cloudinary.com/x/cover.png") -> httpx.Response:
    return httpx.Response(201, json={"_id": post_id, "coverImage": cover, "title": "T"})


def make(fake: FakeKbNews, **kwargs) -> KbNewsClient:
    http = httpx.AsyncClient(
        base_url="https://kbnews.test", transport=httpx.MockTransport(fake.handler)
    )
    # Warm-up is off by default so these tests only see the calls they queue.
    defaults = dict(max_retries=3, backoff_seconds=0, warm_up_attempts=0)
    return KbNewsClient("https://kbnews.test", "bot@example.com", "pw", client=http, **{**defaults, **kwargs})


async def test_login_then_multipart_post_with_image():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created())

    post = await make(fake).create_news(
        title="Big story", content="Body text", category="World", image=IMAGE
    )

    assert post.id == "abc123"
    assert post.cover_image == "https://res.cloudinary.com/x/cover.png"

    login, news = fake.calls
    assert login.read() == b'{"email":"bot@example.com","password":"pw"}'
    assert news.headers["content-type"].startswith("multipart/form-data")
    assert news.headers["cookie"] == "token=jwt123"  # session cookie reused on the next request
    body = fake.bodies[1]
    assert b'name="title"' in body and b"Big story" in body
    assert b'name="content"' in body and b"Body text" in body
    assert b'name="category"' in body and b"World" in body
    assert b'name="coverImage"; filename="cover.png"' in body
    assert b"Content-Type: image/png" in body


async def test_post_without_image_is_still_multipart():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created(cover=""))

    post = await make(fake).create_news(title="T", content="C")

    news = fake.calls[1]
    assert news.headers["content-type"].startswith("multipart/form-data")
    assert b'name="coverImage"' not in fake.bodies[1]
    assert post.cover_image is None


async def test_login_happens_once_for_several_posts():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created("1"), created("2"))
    client = make(fake)

    await client.create_news(title="A", content="C")
    await client.create_news(title="B", content="C")

    assert fake.count("POST", "/api/auth/login") == 1


async def test_expired_cookie_triggers_relogin_and_retry():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue(
        "POST", "/api/news",
        httpx.Response(401, json={"message": "Unauthorized — invalid token"}),
        created("after-relogin"),
    )

    post = await make(fake).create_news(title="T", content="C")

    assert post.id == "after-relogin"
    assert fake.count("POST", "/api/auth/login") == 2
    assert fake.count("POST", "/api/news") == 2


async def test_second_401_after_relogin_raises_auth_error():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.Response(401, json={"message": "nope"}))

    with pytest.raises(KbNewsAuthError, match="still unauthorized"):
        await make(fake).create_news(title="T", content="C")


async def test_bad_credentials_raise_without_retry():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", httpx.Response(400, json={"message": "Invalid credentials"}))

    with pytest.raises(KbNewsAuthError, match="Invalid credentials"):
        await make(fake).create_news(title="T", content="C")
    assert fake.count("POST", "/api/auth/login") == 1


async def test_missing_credentials_raise_before_any_request():
    fake = FakeKbNews()
    http = httpx.AsyncClient(base_url="https://kbnews.test", transport=httpx.MockTransport(fake.handler))
    client = KbNewsClient("https://kbnews.test", "", "", client=http)
    with pytest.raises(KbNewsAuthError, match="not set"):
        await client.create_news(title="T", content="C")
    assert fake.calls == []


async def test_login_without_cookie_raises():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", httpx.Response(200, json={"success": True}))
    with pytest.raises(KbNewsError, match="no token cookie"):
        await make(fake).create_news(title="T", content="C")


async def test_400_rejection_is_not_retried():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.Response(400, json={"message": "Title and content are required"}))

    with pytest.raises(KbNewsRejected, match="Title and content"):
        await make(fake).create_news(title="", content="")
    assert fake.count("POST", "/api/news") == 1


async def test_429_and_500_are_retried_without_duplicate_check():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue(
        "POST", "/api/news",
        httpx.Response(429, json={"message": "Too many requests. Please try again later."}),
        httpx.Response(500, json={"message": "Server error"}),
        created("finally"),
    )

    post = await make(fake).create_news(title="T", content="C")

    assert post.id == "finally"
    assert fake.count("POST", "/api/news") == 3
    assert fake.count("GET", "/api/news") == 0  # these failures cannot have saved a post


async def test_connect_error_is_retried_without_duplicate_check():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.ConnectError("refused"), created("ok"))

    post = await make(fake).create_news(title="T", content="C")

    assert post.id == "ok"
    assert fake.count("GET", "/api/news") == 0


async def test_ambiguous_failure_reuses_post_that_was_already_created():
    fake = FakeKbNews()
    now = datetime.now(timezone.utc).isoformat()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.ReadTimeout("slow"))
    fake.queue(
        "GET", "/api/news",
        httpx.Response(200, json={"articles": [
            {"_id": "other", "title": "Different title", "createdAt": now},
            {"_id": "mine", "title": "Big story", "coverImage": "https://c/x.png", "createdAt": now},
        ]}),
    )

    post = await make(fake).create_news(title="Big story", content="C")

    assert post.id == "mine"
    assert post.cover_image == "https://c/x.png"
    assert fake.count("POST", "/api/news") == 1  # no second POST, so no duplicate


async def test_ambiguous_failure_with_nothing_found_retries_post():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.Response(503), created("second-try"))
    fake.queue("GET", "/api/news", httpx.Response(200, json={"articles": []}))

    post = await make(fake).create_news(title="T", content="C")

    assert post.id == "second-try"
    assert fake.count("POST", "/api/news") == 2
    assert fake.count("GET", "/api/news") == 1


async def test_old_post_with_same_title_is_not_mistaken_for_ours():
    fake = FakeKbNews()
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.Response(504), created("new-one"))
    fake.queue(
        "GET", "/api/news",
        httpx.Response(200, json={"articles": [{"_id": "old", "title": "Same title", "createdAt": old}]}),
    )

    post = await make(fake).create_news(title="Same title", content="C")

    assert post.id == "new-one"


async def test_gives_up_after_max_retries():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.Response(500, json={"message": "Server error"}))

    with pytest.raises(KbNewsError, match="after retries"):
        await make(fake, max_retries=2).create_news(title="T", content="C")
    assert fake.count("POST", "/api/news") == 3


async def test_unexpected_status_raises_without_retry():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", httpx.Response(413, json={"message": "too big"}))

    with pytest.raises(KbNewsError, match="HTTP 413"):
        await make(fake).create_news(title="T", content="C")
    assert fake.count("POST", "/api/news") == 1


def test_category_mapping_uses_values_the_kbnews_ui_offers():
    assert kbnews_category(Category.WORLD) == "World"
    assert kbnews_category(Category.INDIA) == "General"
    assert kbnews_category(Category.MAHARASHTRA) == "General"


class SleepRecorder:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


async def test_every_request_identifies_the_service():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created())

    await make(fake).create_news(title="T", content="C")

    assert len(fake.calls) == 2
    for request in fake.calls:
        assert request.headers["user-agent"] == "news-generator/1.0"
        assert request.headers["accept"] == "application/json"


async def test_retry_after_is_honoured_when_longer_than_backoff():
    fake = FakeKbNews()
    sleeper = SleepRecorder()
    fake.queue("POST", "/api/auth/login", httpx.Response(429, headers={"retry-after": "7"}), login_ok())
    fake.queue("POST", "/api/news", created())

    await make(fake, sleep=sleeper).create_news(title="T", content="C")

    assert sleeper.delays == [7.0]


async def test_retry_after_never_shortens_the_backoff():
    fake = FakeKbNews()
    sleeper = SleepRecorder()
    fake.queue("POST", "/api/auth/login", httpx.Response(429, headers={"retry-after": "2"}), login_ok())
    fake.queue("POST", "/api/news", created())

    await make(fake, sleep=sleeper, backoff_seconds=10).create_news(title="T", content="C")

    assert sleeper.delays == [10.0]


async def test_retry_after_is_capped_at_one_minute():
    fake = FakeKbNews()
    sleeper = SleepRecorder()
    fake.queue("POST", "/api/auth/login", httpx.Response(429, headers={"retry-after": "3600"}), login_ok())
    fake.queue("POST", "/api/news", created())

    await make(fake, sleep=sleeper).create_news(title="T", content="C")

    assert sleeper.delays == [60.0]


async def test_unparseable_retry_after_is_ignored():
    fake = FakeKbNews()
    sleeper = SleepRecorder()
    date = "Wed, 21 Oct 2026 07:28:00 GMT"
    fake.queue("POST", "/api/auth/login", httpx.Response(429, headers={"retry-after": date}), login_ok())
    fake.queue("POST", "/api/news", created())

    await make(fake, sleep=sleeper, backoff_seconds=3).create_news(title="T", content="C")

    assert sleeper.delays == [3.0]


async def test_login_429_from_an_edge_proxy_is_named_in_the_error():
    # The shape seen in production: plain-text 429 with no x-powered-by header.
    fake = FakeKbNews()
    fake.queue(
        "POST", "/api/auth/login",
        httpx.Response(429, text="Too Many Requests", headers={"server": "cloudflare"}),
    )

    with pytest.raises(KbNewsError) as excinfo:
        await make(fake, max_retries=2).create_news(title="T", content="C")

    message = str(excinfo.value)
    assert "login failed after retries" in message
    assert "HTTP 429: Too Many Requests" in message
    assert "x-powered-by=absent, server=cloudflare" in message
    assert fake.count("POST", "/api/auth/login") == 3


async def test_429_from_kbnews_itself_is_named_as_express():
    fake = FakeKbNews()
    fake.queue(
        "POST", "/api/auth/login",
        httpx.Response(
            429,
            json={"message": "Too many requests. Please try again later."},
            headers={"x-powered-by": "Express"},
        ),
    )

    with pytest.raises(KbNewsError, match="x-powered-by=Express"):
        await make(fake, max_retries=1).create_news(title="T", content="C")


async def test_warm_up_waits_until_kbnews_answers_before_logging_in():
    fake = FakeKbNews()
    sleeper = SleepRecorder()
    fake.queue("GET", "/", httpx.Response(429), httpx.Response(503), httpx.Response(200, text="<html>"))
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created())

    client = make(fake, sleep=sleeper, warm_up_attempts=6, warm_up_interval=15.0)
    post = await client.create_news(title="T", content="C")

    assert post.id == "abc123"
    assert fake.count("GET", "/") == 3
    assert sleeper.delays == [15.0, 15.0]
    order = [(r.method, r.url.path) for r in fake.calls]
    assert order == [
        ("GET", "/"), ("GET", "/"), ("GET", "/"),
        ("POST", "/api/auth/login"), ("POST", "/api/news"),
    ]
    assert fake.calls[0].headers["user-agent"] == "news-generator/1.0"


async def test_warm_up_stops_at_once_when_kbnews_is_already_awake():
    fake = FakeKbNews()
    sleeper = SleepRecorder()
    fake.queue("GET", "/", httpx.Response(200, text="<html>"))
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created())

    await make(fake, sleep=sleeper, warm_up_attempts=6).create_news(title="T", content="C")

    assert fake.count("GET", "/") == 1
    assert sleeper.delays == []


async def test_warm_up_treats_connection_errors_as_not_ready():
    fake = FakeKbNews()
    fake.queue("GET", "/", httpx.ConnectError("refused"), httpx.Response(200, text="<html>"))
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created())

    await make(fake, sleep=SleepRecorder(), warm_up_attempts=3).create_news(title="T", content="C")

    assert fake.count("GET", "/") == 2


async def test_warm_up_gives_up_but_still_tries_to_log_in():
    fake = FakeKbNews()
    sleeper = SleepRecorder()
    fake.queue("GET", "/", httpx.Response(429, text="Too Many Requests"))
    fake.queue("POST", "/api/auth/login", login_ok())
    fake.queue("POST", "/api/news", created())

    post = await make(fake, sleep=sleeper, warm_up_attempts=3, warm_up_interval=15.0).create_news(
        title="T", content="C"
    )

    assert post.id == "abc123"
    assert fake.count("GET", "/") == 3
    assert sleeper.delays == [15.0, 15.0]  # no pointless wait after the last attempt


async def test_failed_login_makes_later_posts_fail_fast_without_more_login_attempts():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", httpx.Response(429, text="Too Many Requests"))
    client = make(fake, max_retries=2)

    with pytest.raises(KbNewsError, match="login failed after retries"):
        await client.create_news(title="First", content="C")
    attempts_after_first = fake.count("POST", "/api/auth/login")
    assert attempts_after_first == 3

    with pytest.raises(KbNewsError, match="already failed in this run"):
        await client.create_news(title="Second", content="C")
    with pytest.raises(KbNewsError, match="already failed in this run"):
        await client.create_news(title="Third", content="C")

    assert fake.count("POST", "/api/auth/login") == attempts_after_first
    assert fake.count("POST", "/api/news") == 0


async def test_rejected_credentials_also_fail_later_posts_fast():
    fake = FakeKbNews()
    fake.queue("POST", "/api/auth/login", httpx.Response(400, json={"message": "Invalid credentials"}))
    client = make(fake)

    with pytest.raises(KbNewsAuthError):
        await client.create_news(title="A", content="C")
    with pytest.raises(KbNewsError, match="Invalid credentials"):
        await client.create_news(title="B", content="C")

    assert fake.count("POST", "/api/auth/login") == 1
