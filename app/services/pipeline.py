import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from app.clients.gemini import GeminiError
from app.clients.gnews import GNewsClient, GNewsError
from app.clients.images import DownloadedImage, download_image
from app.clients.kbnews import KbNewsError, kbnews_category
from app.models import Category, RunStatus
from app.prompts import CATEGORY_CONFIGS
from app.schemas import GNewsArticle
from app.services.generator import JsonGenerator, generate_article, with_attribution
from app.services.repository import Repository
from app.services.trending import pick_most_covered

logger = logging.getLogger(__name__)

# India has no daylight saving, so a fixed offset is exact and needs no tz database.
IST = timezone(timedelta(hours=5, minutes=30))

# How many different stories to try for one category when earlier ones are skipped as unusable.
MAX_CANDIDATES = 3
# GNews's free plan allows one request per second.
GNEWS_PAUSE_SECONDS = 1.2
# Everything this pipeline posts is in the order below.
ALL_CATEGORIES: tuple[Category, ...] = (Category.WORLD, Category.INDIA, Category.MAHARASHTRA)

MAHARASHTRA_QUERY = "Maharashtra OR Mumbai OR Pune OR Nagpur"


class KbNewsPoster(Protocol):
    async def create_news(
        self, *, title: str, content: str, category: str = ..., image: DownloadedImage | None = ...
    ): ...


@dataclass(frozen=True)
class CategoryResult:
    category: str
    outcome: str  # posted | already_posted | no_story | failed
    article_id: int | None = None
    kbnews_post_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    status: str
    results: list[CategoryResult]


def today_ist() -> date:
    return datetime.now(IST).date()


async def fetch_candidates(gnews: GNewsClient, category: Category) -> list[GNewsArticle]:
    """Today's candidate stories for a category, in GNews's own ranking order."""
    if category is Category.WORLD:
        return await gnews.top_headlines(category="world", lang="en")
    if category is Category.INDIA:
        return await gnews.top_headlines(category="general", lang="en", country="in")
    return await gnews.search(MAHARASHTRA_QUERY, lang="en", country="in")


async def run_pipeline(
    repo: Repository,
    gnews: GNewsClient,
    gemini: JsonGenerator,
    kbnews: KbNewsPoster,
    *,
    categories: Sequence[Category] = ALL_CATEGORIES,
    triggered_by: str = "manual",
    today: date | None = None,
    download: Callable[[str | None], Awaitable[DownloadedImage | None]] = download_image,
    gnews_pause: float = GNEWS_PAUSE_SECONDS,
) -> RunSummary:
    """Post one story per category for today. Safe to run again: finished categories are skipped.

    Raises RunInProgress if another run is active. Each category is isolated, so a failure in one
    never stops the others.
    """
    post_date = today or today_ist()
    run_id = await repo.start_run(triggered_by)
    results: list[CategoryResult] = []
    try:
        for index, category in enumerate(categories):
            if index:
                await asyncio.sleep(gnews_pause)
            try:
                result = await process_category(
                    repo, gnews, gemini, kbnews, category, run_id, post_date, download
                )
            except Exception as exc:  # one bad category must not abort the rest of the run
                logger.exception("Unexpected error in category %s", category.value)
                result = CategoryResult(category.value, "failed", reason=f"unexpected error: {exc!r}")
            logger.info("Category %s: %s %s", result.category, result.outcome, result.reason or "")
            results.append(result)
    finally:
        # Even if the loop was cancelled, do not leave the run marked as running.
        status = _run_status(results)
        await repo.finish_run(run_id, status)

    return RunSummary(run_id=run_id, status=status.value, results=results)


async def process_category(
    repo: Repository,
    gnews: GNewsClient,
    gemini: JsonGenerator,
    kbnews: KbNewsPoster,
    category: Category,
    run_id: int,
    post_date: date,
    download: Callable[[str | None], Awaitable[DownloadedImage | None]],
) -> CategoryResult:
    name = category.value
    if await repo.has_posted(name, post_date):
        return CategoryResult(name, "already_posted")

    try:
        candidates = await fetch_candidates(gnews, category)
    except GNewsError as exc:
        return CategoryResult(name, "failed", reason=f"GNews: {exc}")

    excluded = await repo.excluded_urls()
    config = CATEGORY_CONFIGS[category]
    skip_reasons: list[str] = []

    for _ in range(MAX_CANDIDATES):
        pick = await pick_most_covered(candidates, gemini, exclude_urls=excluded)
        if pick is None:
            break
        article = pick.article
        article_id = await repo.record_selected(
            run_id=run_id,
            category=name,
            language=config.language_code,
            post_date=post_date,
            article=article,
        )

        try:
            outcome = await generate_article(article, category, gemini)
            if outcome.skipped:
                await repo.mark_skipped(article_id, outcome.skip_reason or "skipped")
                excluded.add(article.url)
                skip_reasons.append(f"{article.url}: {outcome.skip_reason}")
                continue

            body = with_attribution(outcome.body, article.source.name, article.url, config.language_code)
            await repo.mark_generated(article_id, outcome.title, body)

            image = await download(article.image)
            post = await kbnews.create_news(
                title=outcome.title,
                content=body,
                category=kbnews_category(category),
                image=image,
            )
        except (GeminiError, KbNewsError) as exc:
            await repo.mark_failed(article_id, str(exc))
            return CategoryResult(name, "failed", article_id=article_id, reason=str(exc))

        # The post is live from here on; if recording it fails, the error surfaces loudly with the
        # kbNews id so it can be reconciled by hand rather than posted twice.
        try:
            await repo.mark_posted(article_id, post.id)
        except Exception as exc:
            raise RuntimeError(
                f"posted to kbNews as {post.id} but could not record it for article {article_id}"
            ) from exc
        return CategoryResult(name, "posted", article_id=article_id, kbnews_post_id=post.id)

    reason = "; ".join(skip_reasons) if skip_reasons else "no eligible articles"
    return CategoryResult(name, "no_story", reason=reason)


def _run_status(results: Sequence[CategoryResult]) -> RunStatus:
    failed = sum(1 for r in results if r.outcome == "failed")
    if failed == 0:
        return RunStatus.SUCCESS
    return RunStatus.FAILED if failed == len(results) else RunStatus.PARTIAL
