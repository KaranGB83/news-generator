from datetime import date

import pytest
from sqlalchemy import select

from app.clients.gemini import GeminiError
from app.clients.gnews import GNewsError, GNewsQuotaExceeded
from app.clients.kbnews import KbNewsError
from app.models import Article, Category, Run
from app.services.pipeline import MAX_CANDIDATES, run_pipeline, today_ist
from app.services.repository import Repository, RunInProgress
from tests.fakes import (
    PNG,
    FakeGemini,
    FakeGNews,
    FakeKbNews,
    fake_download,
    story,
)

DAY = date(2026, 11, 5)


@pytest.fixture
def world():
    class World:
        gnews = FakeGNews()
        gemini = FakeGemini()
        kbnews = FakeKbNews()

    return World()


async def run(session_factory, world, **kwargs):
    kwargs.setdefault("today", DAY)
    kwargs.setdefault("download", fake_download)
    kwargs.setdefault("gnews_pause", 0)
    return await run_pipeline(
        Repository(session_factory), world.gnews, world.gemini, world.kbnews, **kwargs
    )


async def articles(session_factory) -> list[Article]:
    async with session_factory() as session:
        return list((await session.scalars(select(Article).order_by(Article.id))).all())


def outcomes(summary) -> dict[str, str]:
    return {r.category: r.outcome for r in summary.results}


async def test_posts_one_story_per_category(session_factory, world):
    summary = await run(session_factory, world, triggered_by="github-actions")

    assert summary.status == "success"
    assert outcomes(summary) == {"world": "posted", "india": "posted", "maharashtra": "posted"}
    assert [p["category"] for p in world.kbnews.posts] == ["World", "General", "General"]

    # World and India are written in English, Maharashtra in Marathi, each crediting the source.
    world_post, india_post, mh_post = world.kbnews.posts
    assert world_post["content"].rstrip().endswith("(https://example.com/story-1)")
    assert "Source:" in india_post["content"]
    assert "स्रोत:" in mh_post["content"]
    assert mh_post["title"].startswith("राज्य")

    rows = await articles(session_factory)
    assert [(r.category, r.status, r.kbnews_post_id) for r in rows] == [
        ("world", "posted", "kb-1"),
        ("india", "posted", "kb-2"),
        ("maharashtra", "posted", "kb-3"),
    ]
    assert rows[2].language == "mr"

    async with session_factory() as session:
        run_row = (await session.scalars(select(Run))).one()
        assert run_row.status == "success" and run_row.triggered_by == "github-actions"
        assert run_row.finished_at is not None


async def test_fetches_each_category_with_the_right_gnews_query(session_factory, world):
    await run(session_factory, world)

    (kind1, args1), (kind2, args2), (kind3, args3) = world.gnews.calls
    assert (kind1, args1) == ("top_headlines", {"category": "world", "lang": "en"})
    assert (kind2, args2) == (
        "top_headlines",
        {"category": "general", "lang": "en", "country": "in"},
    )
    assert kind3 == "search"
    assert "Maharashtra" in args3["query"] and "Mumbai" in args3["query"]
    assert args3["country"] == "in"


async def test_image_is_downloaded_and_passed_to_kbnews(session_factory, world):
    await run(session_factory, world, categories=[Category.WORLD])
    image = world.kbnews.posts[0]["image"]
    assert image.content == PNG and image.content_type == "image/png"


async def test_rerun_same_day_posts_nothing_new(session_factory, world):
    await run(session_factory, world)
    world.gnews.calls.clear()

    second = await run(session_factory, world)

    assert second.status == "success"
    assert set(outcomes(second).values()) == {"already_posted"}
    assert len(world.kbnews.posts) == 3
    assert world.gnews.calls == []


async def test_failed_category_does_not_stop_others_and_rerun_retries_only_it(session_factory, world):
    world.gnews.errors["india"] = GNewsQuotaExceeded("daily limit")

    first = await run(session_factory, world)

    assert first.status == "partial"
    assert outcomes(first) == {"world": "posted", "india": "failed", "maharashtra": "posted"}
    assert "daily limit" in next(r.reason for r in first.results if r.category == "india")
    assert len(world.kbnews.posts) == 2

    del world.gnews.errors["india"]
    second = await run(session_factory, world)

    assert second.status == "success"
    assert outcomes(second) == {"world": "already_posted", "india": "posted", "maharashtra": "already_posted"}
    assert len(world.kbnews.posts) == 3


async def test_every_category_failing_marks_run_failed(session_factory, world):
    for key in ("world", "india", "maharashtra"):
        world.gnews.errors[key] = GNewsError("down")

    summary = await run(session_factory, world)

    assert summary.status == "failed"
    assert set(outcomes(summary).values()) == {"failed"}
    assert world.kbnews.posts == []


async def test_kbnews_failure_marks_article_failed_and_rerun_reuses_it(session_factory, world):
    world.kbnews.errors = [KbNewsError("kbNews failed after retries: HTTP 503")]

    first = await run(session_factory, world, categories=[Category.WORLD])

    assert first.status == "failed"
    row = (await articles(session_factory))[0]
    assert row.status == "failed" and "503" in row.error and row.attempts == 1
    assert row.generated_title is not None  # the written text is kept for debugging

    second = await run(session_factory, world, categories=[Category.WORLD])

    assert second.status == "success"
    rows = await articles(session_factory)
    assert len(rows) == 1  # same story, same row
    assert rows[0].status == "posted" and rows[0].attempts == 2


async def test_gemini_error_while_writing_marks_failed(session_factory, world):
    world.gemini.generation_errors = [GeminiError("quota exhausted")]

    summary = await run(session_factory, world, categories=[Category.INDIA])

    assert outcomes(summary) == {"india": "failed"}
    assert (await articles(session_factory))[0].status == "failed"
    assert world.kbnews.posts == []


async def test_unusable_story_is_skipped_and_next_one_is_tried(session_factory, world):
    world.gemini.skip_urls = {story(1).url}

    summary = await run(session_factory, world, categories=[Category.WORLD])

    assert outcomes(summary) == {"world": "posted"}
    rows = await articles(session_factory)
    assert [(r.source_url, r.status) for r in rows] == [
        (story(1).url, "skipped"),
        (story(2).url, "posted"),
    ]
    assert rows[0].error == "not usable"


async def test_gives_up_after_max_candidates_all_skipped(session_factory, world):
    world.gnews.stories["world"] = [story(n) for n in range(1, 6)]
    world.gemini.skip_urls = {story(n).url for n in range(1, 6)}

    summary = await run(session_factory, world, categories=[Category.WORLD])

    result = summary.results[0]
    assert result.outcome == "no_story"
    assert "not usable" in result.reason
    assert summary.status == "success"  # nothing usable is not a failure
    rows = await articles(session_factory)
    assert len(rows) == MAX_CANDIDATES and {r.status for r in rows} == {"skipped"}
    assert world.kbnews.posts == []


async def test_no_candidates_returns_no_story(session_factory, world):
    world.gnews.stories["world"] = []
    summary = await run(session_factory, world, categories=[Category.WORLD])
    assert summary.results[0].outcome == "no_story"
    assert summary.results[0].reason == "no eligible articles"


async def test_story_posted_yesterday_is_not_posted_again(session_factory, world):
    await run(session_factory, world, categories=[Category.WORLD], today=date(2026, 11, 4))
    assert world.kbnews.posts[0]["content"].endswith("(https://example.com/story-1)")

    await run(session_factory, world, categories=[Category.WORLD], today=date(2026, 11, 5))

    assert world.kbnews.posts[1]["content"].endswith("(https://example.com/story-2)")


async def test_unexpected_error_in_one_category_is_contained(session_factory, world):
    async def exploding_download(url):
        if "story-1" in (url or ""):
            raise RuntimeError("disk full")
        return None

    summary = await run(session_factory, world, download=exploding_download)

    # Every category's first story is story-1, so all three hit the bug, but each is contained.
    assert summary.status == "failed"
    assert all("unexpected error" in r.reason for r in summary.results)
    async with session_factory() as session:
        assert (await session.scalars(select(Run))).one().status == "failed"


async def test_only_requested_categories_run(session_factory, world):
    summary = await run(session_factory, world, categories=[Category.MAHARASHTRA])
    assert [r.category for r in summary.results] == ["maharashtra"]
    assert len(world.kbnews.posts) == 1
    assert [kind for kind, _ in world.gnews.calls] == ["search"]


async def test_refuses_to_start_while_another_run_is_active(session_factory, world):
    await Repository(session_factory).start_run("github-actions")

    with pytest.raises(RunInProgress):
        await run(session_factory, world)

    assert world.gnews.calls == [] and world.kbnews.posts == []


async def test_run_is_closed_even_if_a_later_step_raises(session_factory, world):
    class BrokenRepo(Repository):
        async def has_posted(self, category, post_date):
            raise RuntimeError("database went away")

    summary = await run_pipeline(
        BrokenRepo(session_factory), world.gnews, world.gemini, world.kbnews,
        today=DAY, download=fake_download, gnews_pause=0,
    )

    assert summary.status == "failed"
    async with session_factory() as session:
        assert (await session.scalars(select(Run))).one().finished_at is not None


def test_today_ist_is_a_date():
    assert isinstance(today_ist(), date)
