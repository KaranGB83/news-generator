import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

import httpx

from app.clients.images import DownloadedImage
from app.config import get_settings
from app.models import Category

logger = logging.getLogger(__name__)

T = TypeVar("T")

# kbNews's category dropdown only offers these values, so a post in any other category would show
# up under "All" but could never be filtered. Change a value here once the frontend list grows
# (for example to "India" or "Maharashtra").
KBNEWS_CATEGORY = {
    Category.WORLD: "World",
    Category.INDIA: "General",
    Category.MAHARASHTRA: "General",
}

# After an ambiguous failure, a post with the same title created this recently counts as ours.
RECENT_POST_WINDOW = timedelta(minutes=15)

# Identifies this service to whatever sits in front of kbNews; the default python-httpx agent is
# the kind of client edge filters tend to throttle.
USER_AGENT = "news-generator/1.0"
# Longest we will wait because a server asked us to (Retry-After).
MAX_RETRY_AFTER_SECONDS = 60.0

# Responses that mean "not ready yet" while a sleeping Render instance wakes up (about a minute).
NOT_READY_STATUS = {429, 500, 502, 503, 504}
WARM_UP_ATTEMPTS = 6
WARM_UP_INTERVAL_SECONDS = 15.0
WARM_UP_REQUEST_TIMEOUT = 30.0


def kbnews_category(category: Category) -> str:
    return KBNEWS_CATEGORY.get(category, "General")


class KbNewsError(Exception):
    """Base error for kbNews failures."""


class KbNewsAuthError(KbNewsError):
    """The bot account could not log in. Retrying with the same credentials is pointless."""


class KbNewsRejected(KbNewsError):
    """kbNews returned 400: the post itself was refused (for example empty title or content)."""


@dataclass(frozen=True)
class KbNewsPost:
    id: str
    cover_image: str | None = None


class _Transient(Exception):
    """A retryable failure. `ambiguous` means the server may have processed the request anyway."""

    def __init__(
        self, reason: str, *, ambiguous: bool, retry_after: float | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.ambiguous = ambiguous
        self.retry_after = retry_after


class KbNewsClient:
    """Posts articles to kbNews as a bot user.

    kbNews authenticates with an httpOnly `token` cookie set by POST /api/auth/login (the JWT is
    not in the response body), so the httpx client's cookie jar carries the session.
    """

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        password: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 4,
        backoff_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        warm_up_attempts: int = WARM_UP_ATTEMPTS,
        warm_up_interval: float = WARM_UP_INTERVAL_SECONDS,
    ) -> None:
        settings = get_settings()
        self._email = email if email is not None else settings.kbnews_bot_email
        self._password = password if password is not None else settings.kbnews_bot_password
        # Long timeout: Render's free tier can take a minute to wake up.
        self._client = client or httpx.AsyncClient(
            base_url=(base_url or settings.kbnews_base_url).rstrip("/"), timeout=90.0
        )
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._warm_up_attempts = warm_up_attempts
        self._warm_up_interval = warm_up_interval
        self._logged_in = False
        # Set when login fails, so later posts in the same run fail at once instead of repeating
        # a full round of login attempts for every category.
        self._unavailable: str | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "KbNewsClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- public API -------------------------------------------------------------------------

    async def create_news(
        self,
        *,
        title: str,
        content: str,
        category: str = "General",
        image: DownloadedImage | None = None,
    ) -> KbNewsPost:
        """Create a post and return its id.

        If a request fails in a way that may have created the post anyway (timeout, 502/503/504),
        kbNews is searched for a just-created post with this title before retrying, so a retry
        can't produce a duplicate.
        """
        await self._ensure_login()

        async def attempt() -> KbNewsPost:
            return await self._post_once(title, content, category, image)

        async def recover() -> KbNewsPost | None:
            return await self.find_recent_by_title(title)

        return await self._retrying("create post", attempt, recover=recover)

    async def find_recent_by_title(self, title: str) -> KbNewsPost | None:
        """Look for a post with exactly this title created within the last few minutes."""

        async def attempt() -> KbNewsPost | None:
            response = await self._attempt("GET", "/api/news", params={"search": title, "limit": 10})
            if response.status_code != 200:
                raise KbNewsError(f"search failed: HTTP {response.status_code}")
            now = datetime.now(timezone.utc)
            for item in response.json().get("articles", []):
                if str(item.get("title", "")).strip() != title.strip():
                    continue
                created = _parse_time(item.get("createdAt"))
                if created is not None and now - created > RECENT_POST_WINDOW:
                    continue
                return KbNewsPost(id=str(item["_id"]), cover_image=item.get("coverImage") or None)
            return None

        return await self._retrying("search for existing post", attempt)

    # --- auth -------------------------------------------------------------------------------

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return
        if self._unavailable is not None:
            raise KbNewsError(f"kbNews login already failed in this run: {self._unavailable}")
        self._require_credentials()  # no point waking kbNews if we cannot log in anyway
        try:
            await self._warm_up()
            await self._login()
        except KbNewsError as exc:
            self._unavailable = str(exc)
            raise

    async def _warm_up(self) -> None:
        """Wake kbNews if it is asleep, waiting until it answers normally.

        A free Render instance takes about a minute to start, and the proxy in front of it can answer
        with errors (including 429) meanwhile. Waiting here is gentler than burning the login retries.
        If it never answers normally we still try to log in; the login has its own retries.
        """
        for number in range(self._warm_up_attempts):
            try:
                response = await self._client.get(
                    "/", headers=_default_headers(), timeout=WARM_UP_REQUEST_TIMEOUT
                )
            except httpx.TransportError as exc:
                state = repr(exc)
            else:
                if _is_cloudflare_challenge(response):
                    # A challenge page is Cloudflare screening this client, not kbNews waking up,
                    # so waiting here will not change the outcome.
                    logger.warning(
                        "kbNews answered with a Cloudflare challenge (HTTP %s); skipping warm-up",
                        response.status_code,
                    )
                    return
                if response.status_code not in NOT_READY_STATUS:
                    return
                state = f"HTTP {response.status_code} [{_origin(response)}]"
            logger.info("kbNews not ready yet (%s)", state)
            if number < self._warm_up_attempts - 1:
                await self._sleep(self._warm_up_interval)
        if self._warm_up_attempts:
            logger.warning("kbNews still not answering normally after warm-up; trying to log in anyway")

    def _require_credentials(self) -> None:
        if not self._email or not self._password:
            raise KbNewsAuthError("KBNEWS_BOT_EMAIL / KBNEWS_BOT_PASSWORD are not set")

    async def _login(self) -> None:
        self._require_credentials()

        async def attempt() -> None:
            response = await self._attempt(
                "POST", "/api/auth/login", json={"email": self._email, "password": self._password}
            )
            if response.status_code == 400:
                # kbNews answers 400 "Invalid credentials" for unknown email and wrong password.
                raise KbNewsAuthError(f"login rejected: {_message(response)}")
            if response.status_code != 200:
                raise KbNewsError(f"login failed: HTTP {response.status_code} {_message(response)}")
            if not self._client.cookies.get("token"):
                raise KbNewsError("login succeeded but kbNews set no token cookie")

        await self._retrying("login", attempt)
        self._logged_in = True

    # --- requests ---------------------------------------------------------------------------

    async def _post_once(
        self, title: str, content: str, category: str, image: DownloadedImage | None
    ) -> KbNewsPost:
        response = await self._attempt("POST", "/api/news", files=_multipart(title, content, category, image))
        if response.status_code == 401:
            # Cookie expired or was invalidated: log in again and repeat once.
            logger.info("kbNews returned 401; logging in again")
            self._logged_in = False
            self._client.cookies.clear()
            await self._login()
            response = await self._attempt(
                "POST", "/api/news", files=_multipart(title, content, category, image)
            )
            if response.status_code == 401:
                raise KbNewsAuthError(f"still unauthorized after login: {_message(response)}")

        if response.status_code == 201:
            data = response.json()
            return KbNewsPost(id=str(data["_id"]), cover_image=data.get("coverImage") or None)
        if response.status_code == 400:
            raise KbNewsRejected(_message(response))
        raise KbNewsError(f"HTTP {response.status_code}: {_message(response)}")

    async def _attempt(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One HTTP call; converts retryable failures into `_Transient`."""
        headers = _default_headers(kwargs.pop("headers", None))
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Never reached the server, so repeating cannot duplicate anything.
            raise _Transient(f"could not connect: {exc!r}", ambiguous=False) from exc
        except httpx.TransportError as exc:
            # Timed out or dropped mid-request: the server may or may not have acted on it.
            raise _Transient(f"network error: {exc!r}", ambiguous=True) from exc

        status = response.status_code
        if status in (429, 500):
            # 429 is a rate limiter (kbNews's own, or one in front of it); kbNews returns 500
            # before saving anything. The origin note shows which layer produced the answer.
            raise _Transient(
                f"HTTP {status}: {_message(response)} [{_origin(response)}]",
                ambiguous=False,
                retry_after=_retry_after(response),
            )
        if status in (502, 503, 504):
            # Render's proxy while the free instance wakes up, or a gateway timeout.
            raise _Transient(f"HTTP {status} [{_origin(response)}]", ambiguous=True)
        return response

    async def _retrying(
        self,
        what: str,
        attempt: Callable[[], Awaitable[T]],
        *,
        recover: Callable[[], Awaitable[T | None]] | None = None,
    ) -> T:
        last_reason = "unknown error"
        for number in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                return await attempt()
            except _Transient as exc:
                last_reason = exc.reason
                retry_after = exc.retry_after
                if exc.ambiguous and recover is not None:
                    found = await recover()
                    if found is not None:
                        logger.info("%s: request had already succeeded; reusing result", what)
                        return found
            if number < self._max_retries:
                delay = self._backoff * (2**number)
                if retry_after:
                    # The server told us how long to wait; never retry sooner, but cap the wait.
                    delay = max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))
                logger.warning("kbNews %s failed (%s); retrying in %.1fs", what, last_reason, delay)
                await self._sleep(delay)
        raise KbNewsError(f"kbNews {what} failed after retries: {last_reason}")


def _multipart(
    title: str, content: str, category: str, image: DownloadedImage | None
) -> list[tuple[str, tuple[Any, ...]]]:
    """Build a multipart body. kbNews only parses multipart on this route (via multer), so even a
    post without an image must be sent as multipart, not JSON or urlencoded."""
    parts: list[tuple[str, tuple[Any, ...]]] = [
        ("title", (None, title)),
        ("content", (None, content)),
        ("category", (None, category)),
    ]
    if image is not None:
        parts.append(("coverImage", (image.filename, image.content, image.content_type)))
    return parts


def _default_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "application/json")
    return headers


def _origin(response: httpx.Response) -> str:
    """Which layer answered. kbNews's Express app always sends `x-powered-by: Express`, so if it
    is absent the response came from a proxy or edge in front of the app."""
    headers = response.headers
    note = (
        f"x-powered-by={headers.get('x-powered-by', 'absent')}, "
        f"server={headers.get('server', 'unknown')}"
    )
    return f"{note}, cloudflare-challenge" if _is_cloudflare_challenge(response) else note


def _is_cloudflare_challenge(response: httpx.Response) -> bool:
    """Cloudflare's bot screening answers with a "Just a moment..." page (and usually a
    `cf-mitigated: challenge` header). A script cannot solve it."""
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    return "<title>Just a moment...</title>" in response.text[:3000]


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After header (HTTP-date form is ignored)."""
    value = response.headers.get("retry-after", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    return str(data.get("message", ""))[:200] if isinstance(data, dict) else ""


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
