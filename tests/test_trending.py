from app.clients.gemini import GeminiError
from app.schemas import GNewsArticle, GNewsSource
from app.services.trending import ClusterResponse, pick_most_covered


def art(n: int, source: str | None, *, image: bool = True, content: str = "") -> GNewsArticle:
    return GNewsArticle(
        title=f"Headline {n}",
        url=f"https://example.com/{n}",
        image=f"https://example.com/{n}.jpg" if image else None,
        content=content or None,
        source=GNewsSource(name=source),
    )


class FakeGemini:
    def __init__(self, clusters=None, error: Exception | None = None):
        self.clusters = clusters
        self.error = error
        self.prompts: list[str] = []

    async def generate_json(self, prompt, response_model, *, system_instruction=None):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        assert response_model is ClusterResponse
        return ClusterResponse(clusters=self.clusters)


async def test_picks_cluster_with_most_distinct_outlets():
    articles = [art(0, "A"), art(1, "B"), art(2, "C"), art(3, "D"), art(4, "E")]
    gemini = FakeGemini(clusters=[[0], [1, 2, 3], [4]])

    pick = await pick_most_covered(articles, gemini)

    assert pick.method == "gemini"
    assert pick.cluster_size == 3
    assert pick.source_count == 3
    assert pick.article.url in {"https://example.com/1", "https://example.com/2", "https://example.com/3"}


async def test_same_outlet_repeated_does_not_inflate_coverage():
    articles = [art(0, "A"), art(1, "A"), art(2, "A"), art(3, "B"), art(4, "C")]
    # Cluster [0,1,2] is one outlet three times; [3,4] is two outlets, so it wins.
    pick = await pick_most_covered(articles, FakeGemini(clusters=[[0, 1, 2], [3, 4]]))
    assert pick.source_count == 2
    assert pick.article.url in {"https://example.com/3", "https://example.com/4"}


async def test_tie_goes_to_earliest_ranked_cluster():
    articles = [art(0, "A"), art(1, "B"), art(2, "C"), art(3, "D")]
    pick = await pick_most_covered(articles, FakeGemini(clusters=[[2, 3], [0, 1]]))
    assert pick.article.url in {"https://example.com/0", "https://example.com/1"}


async def test_within_cluster_prefers_image_then_more_text():
    articles = [
        art(0, "A", image=False, content="x" * 900),
        art(1, "B", image=True, content="short"),
        art(2, "C", image=True, content="a much longer body of text here"),
    ]
    pick = await pick_most_covered(articles, FakeGemini(clusters=[[0, 1, 2]]))
    assert pick.article.url == "https://example.com/2"


async def test_missing_source_names_count_as_separate_outlets():
    articles = [art(0, None), art(1, None), art(2, "A")]
    pick = await pick_most_covered(articles, FakeGemini(clusters=[[0, 1], [2]]))
    assert pick.source_count == 2
    assert pick.cluster_size == 2


async def test_bad_indices_are_ignored():
    articles = [art(0, "A"), art(1, "B"), art(2, "C")]
    clusters = [[7, -1], [1, 1, 99], [1, 2]]  # junk, duplicates, and a repeat across clusters
    pick = await pick_most_covered(articles, FakeGemini(clusters=clusters))
    assert pick.method == "gemini"
    assert pick.article.url in {"https://example.com/1", "https://example.com/2"}


async def test_gemini_failure_falls_back_to_first_article_with_image():
    articles = [art(0, "A", image=False), art(1, "B"), art(2, "C")]
    pick = await pick_most_covered(articles, FakeGemini(error=GeminiError("down")))
    assert pick.method == "fallback"
    assert pick.article.url == "https://example.com/1"


async def test_no_usable_clusters_falls_back():
    articles = [art(0, "A"), art(1, "B")]
    pick = await pick_most_covered(articles, FakeGemini(clusters=[[5], []]))
    assert pick.method == "fallback"
    assert pick.article.url == "https://example.com/0"


async def test_excluded_urls_are_removed_before_clustering():
    articles = [art(0, "A"), art(1, "B"), art(2, "C")]
    gemini = FakeGemini(clusters=[[0], [1]])
    pick = await pick_most_covered(articles, gemini, exclude_urls={"https://example.com/0"})
    prompt = gemini.prompts[0]
    assert "Headline 0" not in prompt
    assert "0. [B] Headline 1" in prompt  # numbering restarts over the remaining candidates
    assert pick.article.url in {"https://example.com/1", "https://example.com/2"}


async def test_single_candidate_skips_gemini():
    gemini = FakeGemini(error=AssertionError("should not be called"))
    pick = await pick_most_covered([art(0, "A")], gemini)
    assert pick.article.url == "https://example.com/0"
    assert gemini.prompts == []


async def test_nothing_left_returns_none():
    assert await pick_most_covered([], FakeGemini(clusters=[])) is None
    only = [art(0, "A")]
    assert await pick_most_covered(only, FakeGemini(clusters=[]), exclude_urls={"https://example.com/0"}) is None
