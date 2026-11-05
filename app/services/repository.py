import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Article, ArticleStatus, Run, RunStatus
from app.schemas import GNewsArticle

logger = logging.getLogger(__name__)

# A run that has been "running" longer than this is assumed to have crashed.
STALE_RUN_AFTER = timedelta(minutes=30)
ERROR_MAX_CHARS = 2000


class RunInProgress(Exception):
    """Another run started recently and has not finished."""


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres returns aware ones. Treat naive as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Repository:
    """Database access for the pipeline. Every method uses its own short transaction and commits,
    so progress is saved even if a later step crashes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- runs -------------------------------------------------------------------------------

    async def start_run(self, triggered_by: str) -> int:
        async with self._session_factory() as session:
            running = (
                await session.execute(select(Run).where(Run.status == RunStatus.RUNNING.value))
            ).scalars().all()
            active_id: int | None = None
            for run in running:
                if _now() - _utc(run.started_at) > STALE_RUN_AFTER:
                    logger.warning("Marking stale run %s as failed", run.id)
                    run.status = RunStatus.FAILED.value
                    run.finished_at = _now()
                else:
                    active_id = run.id

            if active_id is not None:
                await session.commit()  # keep any stale-run cleanup done above
                raise RunInProgress(f"run {active_id} is still in progress")

            run = Run(triggered_by=triggered_by[:32], status=RunStatus.RUNNING.value)
            session.add(run)
            await session.commit()
            return run.id

    async def finish_run(self, run_id: int, status: RunStatus) -> None:
        async with self._session_factory() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = status.value
                run.finished_at = _now()
                await session.commit()

    # --- articles ---------------------------------------------------------------------------

    async def has_posted(self, category: str, post_date: date) -> bool:
        async with self._session_factory() as session:
            found = await session.scalar(
                select(Article.id).where(
                    Article.category == category,
                    Article.post_date == post_date,
                    Article.status == ArticleStatus.POSTED.value,
                )
            )
            return found is not None

    async def excluded_urls(self) -> set[str]:
        """URLs that must not be picked again: already posted, or skipped as unusable."""
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Article.source_url).where(
                    Article.status.in_([ArticleStatus.POSTED.value, ArticleStatus.SKIPPED.value])
                )
            )
            return set(rows)

    async def record_selected(
        self,
        *,
        run_id: int,
        category: str,
        language: str,
        post_date: date,
        article: GNewsArticle,
    ) -> int:
        """Save the chosen article, or reuse its row if an earlier attempt failed part-way."""
        raw = article.model_dump(mode="json", by_alias=True)
        async with self._session_factory() as session:
            row = await session.scalar(select(Article).where(Article.source_url == article.url))
            if row is None:
                row = Article(source_url=article.url, attempts=0)
                session.add(row)

            row.run_id = run_id
            row.category = category
            row.language = language
            row.post_date = post_date
            row.source_name = article.source.name
            row.source_published_at = article.published_at
            row.raw_json = raw
            row.image_url = article.image
            row.status = ArticleStatus.SELECTED.value
            row.attempts = (row.attempts or 0) + 1
            row.error = None
            try:
                await session.commit()
            except IntegrityError:
                # Two runs inserted the same URL at once.
                await session.rollback()
                raise
            return row.id

    async def mark_generated(self, article_id: int, title: str, body: str) -> None:
        await self._update(
            article_id,
            status=ArticleStatus.GENERATED.value,
            generated_title=title,
            generated_body=body,
        )

    async def mark_skipped(self, article_id: int, reason: str) -> None:
        await self._update(
            article_id, status=ArticleStatus.SKIPPED.value, error=reason[:ERROR_MAX_CHARS]
        )

    async def mark_failed(self, article_id: int, error: str) -> None:
        await self._update(
            article_id, status=ArticleStatus.FAILED.value, error=error[:ERROR_MAX_CHARS]
        )

    async def mark_posted(self, article_id: int, kbnews_post_id: str) -> None:
        await self._update(
            article_id,
            status=ArticleStatus.POSTED.value,
            kbnews_post_id=kbnews_post_id,
            posted_at=_now(),
            error=None,
        )

    async def get_article(self, article_id: int) -> Article | None:
        async with self._session_factory() as session:
            return await session.get(Article, article_id)

    async def _update(self, article_id: int, **fields: object) -> None:
        async with self._session_factory() as session:
            row = await session.get(Article, article_id)
            if row is None:
                raise LookupError(f"article {article_id} not found")
            for name, value in fields.items():
                setattr(row, name, value)
            await session.commit()
