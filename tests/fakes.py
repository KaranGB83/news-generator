"""Fake external services shared by the pipeline and API tests."""

from types import SimpleNamespace

from app.clients.images import DownloadedImage
from app.schemas import GeneratedArticle, GNewsArticle, GNewsSource
from app.services.trending import ClusterResponse

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
EN_BODY = " ".join(["Officials said the measure will take effect next month."] * 8)
MR_TITLE = "राज्य सरकारने नवीन योजना जाहीर केली"
MR_BODY = " ".join(["मुंबईत आज मुख्यमंत्र्यांनी नव्या योजनेची घोषणा केली आणि ती पुढील महिन्यापासून लागू होईल."] * 6)

TEXT = "The government announced a new policy on Monday that affects millions of people nationwide, officials said."


def story(n: int, source: str | None = None) -> GNewsArticle:
    return GNewsArticle(
        title=f"Headline {n}",
        description=TEXT,
        content=f"{TEXT} Further details about item {n} will follow. … [1200 chars]",
        url=f"https://example.com/story-{n}",
        image=f"https://example.com/story-{n}.jpg",
        source=GNewsSource(name=source or f"Outlet {n}"),
    )


def default_stories() -> list[GNewsArticle]:
    return [story(1), story(2), story(3)]


class FakeGNews:
    """Returns configured stories per category and records how it was called."""

    def __init__(self, stories: dict[str, list[GNewsArticle]] | None = None):
        defaults = default_stories()
        self.stories = {"world": defaults, "india": defaults, "maharashtra": defaults}
        self.stories.update(stories or {})
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict]] = []

    def _answer(self, key: str, call: tuple[str, dict]):
        self.calls.append(call)
        if key in self.errors:
            raise self.errors[key]
        return list(self.stories[key])

    async def top_headlines(self, **kwargs):
        key = "india" if kwargs.get("country") == "in" else "world"
        return self._answer(key, ("top_headlines", kwargs))

    async def search(self, query, **kwargs):
        return self._answer("maharashtra", ("search", {"query": query, **kwargs}))


class FakeGemini:
    """Answers clustering and writing requests. Every headline is its own cluster by default."""

    def __init__(self):
        self.generation_errors: list[Exception] = []
        self.skip_urls: set[str] = set()
        self.generated_for: list[str] = []

    async def generate_json(self, prompt, response_model, **kwargs):
        if response_model is ClusterResponse:
            return ClusterResponse(clusters=[[i] for i in range(10)])

        assert response_model is GeneratedArticle
        if self.generation_errors:
            raise self.generation_errors.pop(0)
        self.generated_for.append(prompt)
        if any(url in prompt or self._headline_of(url) in prompt for url in self.skip_urls):
            return GeneratedArticle(skip=True, skip_reason="not usable")
        if "in Marathi" in prompt:
            return GeneratedArticle(title=MR_TITLE, body=MR_BODY)
        return GeneratedArticle(title="New policy takes effect next month", body=EN_BODY)

    @staticmethod
    def _headline_of(url: str) -> str:
        return "Headline " + url.rsplit("-", 1)[-1]


class FakeKbNews:
    def __init__(self):
        self.posts: list[dict] = []
        self.errors: list[Exception] = []

    async def create_news(self, *, title, content, category="General", image=None):
        if self.errors:
            raise self.errors.pop(0)
        self.posts.append({"title": title, "content": content, "category": category, "image": image})
        return SimpleNamespace(id=f"kb-{len(self.posts)}", cover_image=None)


async def fake_download(url):
    return DownloadedImage(PNG, "image/png", "cover.png") if url else None
