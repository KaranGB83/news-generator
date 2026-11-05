from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import ArticleStatus, Run, RunStatus
from app.services.repository import Repository, RunInProgress
from tests.fakes import story

DAY = date(2026, 11, 5)


async def select_article(repo: Repository, **kwargs) -> int:
    return await repo.record_selected(
        run_id=kwargs.pop("run_id", 1),
        category=kwargs.pop("category", "world"),
        language="en",
        post_date=kwargs.pop("post_date", DAY),
        article=kwargs.pop("article", story(1)),
    )


async def test_start_run_creates_a_running_run(session_factory):
    repo = Repository(session_factory)
    run_id = await repo.start_run("github-actions")
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        assert run.status == RunStatus.RUNNING.value
        assert run.triggered_by == "github-actions"
        assert run.finished_at is None


async def test_second_run_allowed_after_first_finishes(session_factory):
    repo = Repository(session_factory)
    first = await repo.start_run("manual")
    await repo.finish_run(first, RunStatus.SUCCESS)
    second = await repo.start_run("manual")
    assert second != first


async def test_concurrent_run_is_rejected(session_factory):
    repo = Repository(session_factory)
    await repo.start_run("github-actions")
    with pytest.raises(RunInProgress, match="still in progress"):
        await repo.start_run("manual")


async def test_stale_running_run_is_marked_failed_and_new_run_starts(session_factory):
    repo = Repository(session_factory)
    stale_id = await repo.start_run("manual")
    async with session_factory() as session:
        run = await session.get(Run, stale_id)
        run.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.commit()

    new_id = await repo.start_run("manual")

    assert new_id != stale_id
    async with session_factory() as session:
        stale = await session.get(Run, stale_id)
        assert stale.status == RunStatus.FAILED.value
        assert stale.finished_at is not None


async def test_finish_run_records_status_and_time(session_factory):
    repo = Repository(session_factory)
    run_id = await repo.start_run("manual")
    await repo.finish_run(run_id, RunStatus.PARTIAL)
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        assert run.status == "partial"
        assert run.finished_at is not None
        assert run.triggered_by == "manual"


async def test_record_selected_stores_article_details(session_factory):
    repo = Repository(session_factory)
    article = story(7, source="The Times")
    article_id = await select_article(repo, article=article)

    row = await repo.get_article(article_id)
    assert row.status == ArticleStatus.SELECTED.value
    assert row.attempts == 1
    assert row.category == "world" and row.language == "en" and row.post_date == DAY
    assert row.source_name == "The Times"
    assert row.source_url == article.url
    assert row.image_url == article.image
    assert row.raw_json["title"] == "Headline 7"


async def test_record_selected_reuses_row_and_counts_attempts(session_factory):
    repo = Repository(session_factory)
    first = await select_article(repo)
    await repo.mark_failed(first, "kbNews down")

    second = await select_article(repo, run_id=2, post_date=date(2026, 11, 6))

    assert second == first
    row = await repo.get_article(first)
    assert row.attempts == 2
    assert row.status == ArticleStatus.SELECTED.value
    assert row.error is None
    assert row.run_id == 2 and row.post_date == date(2026, 11, 6)


async def test_status_transitions_and_fields(session_factory):
    repo = Repository(session_factory)
    article_id = await select_article(repo)

    await repo.mark_generated(article_id, "Title", "Body")
    row = await repo.get_article(article_id)
    assert (row.status, row.generated_title, row.generated_body) == ("generated", "Title", "Body")

    await repo.mark_posted(article_id, "kb-42")
    row = await repo.get_article(article_id)
    assert row.status == "posted" and row.kbnews_post_id == "kb-42" and row.posted_at is not None


async def test_mark_skipped_and_failed_store_reason_truncated(session_factory):
    repo = Repository(session_factory)
    a = await select_article(repo, article=story(1))
    b = await select_article(repo, article=story(2))

    await repo.mark_skipped(a, "thin source")
    await repo.mark_failed(b, "x" * 5000)

    assert (await repo.get_article(a)).error == "thin source"
    failed = await repo.get_article(b)
    assert failed.status == "failed" and len(failed.error) == 2000


async def test_updating_missing_article_raises(session_factory):
    with pytest.raises(LookupError):
        await Repository(session_factory).mark_failed(999, "nope")


async def test_has_posted_checks_category_date_and_status(session_factory):
    repo = Repository(session_factory)
    article_id = await select_article(repo)

    assert not await repo.has_posted("world", DAY)  # selected is not posted
    await repo.mark_posted(article_id, "kb-1")

    assert await repo.has_posted("world", DAY)
    assert not await repo.has_posted("india", DAY)
    assert not await repo.has_posted("world", date(2026, 11, 6))


async def test_excluded_urls_are_posted_and_skipped_only(session_factory):
    repo = Repository(session_factory)
    ids = {}
    for n, category in enumerate(["world", "india", "maharashtra", "world", "india"], start=1):
        ids[n] = await select_article(repo, article=story(n), category=category)

    await repo.mark_posted(ids[1], "kb-1")
    await repo.mark_skipped(ids[2], "thin")
    await repo.mark_failed(ids[3], "boom")
    await repo.mark_generated(ids[4], "t", "b")
    # ids[5] stays "selected"

    assert await repo.excluded_urls() == {story(1).url, story(2).url}


async def test_only_one_posted_article_per_category_per_day(session_factory):
    repo = Repository(session_factory)
    first = await select_article(repo, article=story(1))
    second = await select_article(repo, article=story(2))
    await repo.mark_posted(first, "kb-1")

    with pytest.raises(IntegrityError):
        await repo.mark_posted(second, "kb-2")


async def test_many_skipped_articles_same_day_are_allowed(session_factory):
    repo = Repository(session_factory)
    for n in range(1, 5):
        article_id = await select_article(repo, article=story(n))
        await repo.mark_skipped(article_id, "thin")
