import hmac
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.gemini import GeminiClient
from app.clients.gnews import GNewsClient
from app.clients.kbnews import KbNewsClient
from app.config import get_settings
from app.db import SessionLocal
from app.models import Category
from app.services.pipeline import ALL_CATEGORIES, run_pipeline
from app.services.repository import Repository, RunInProgress

logger = logging.getLogger(__name__)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().api_key
    if not expected:
        # Fail closed: an unset key must never mean "open to everyone".
        raise HTTPException(status_code=503, detail="API_KEY is not configured on the server")
    if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


router = APIRouter(dependencies=[Depends(require_api_key)])


@dataclass
class Clients:
    gnews: GNewsClient
    gemini: GeminiClient
    kbnews: KbNewsClient


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return SessionLocal


async def get_clients() -> AsyncIterator[Clients]:
    async with GNewsClient() as gnews, GeminiClient() as gemini, KbNewsClient() as kbnews:
        yield Clients(gnews, gemini, kbnews)


def parse_categories(raw: str | None) -> tuple[Category, ...]:
    if not raw:
        return ALL_CATEGORIES
    chosen: list[Category] = []
    for part in raw.split(","):
        try:
            category = Category(part.strip().lower())
        except ValueError:
            valid = ", ".join(c.value for c in Category)
            raise HTTPException(status_code=422, detail=f"Unknown category {part!r}; use {valid}")
        if category not in chosen:
            chosen.append(category)
    return tuple(chosen)


@router.post("/jobs/run")
async def run_job(
    source: str = Query("manual", max_length=32, description="Who triggered the run"),
    categories: str | None = Query(None, description="Comma-separated subset, e.g. world,india"),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    clients: Clients = Depends(get_clients),
) -> JSONResponse:
    """Run the daily pipeline and wait for it to finish.

    Runs synchronously on purpose: the open request keeps Render's free instance awake, and a
    failure returns HTTP 500 so the GitHub Actions job turns red and curl retries. Retrying is safe
    because categories that already posted today are skipped.
    """
    selected = parse_categories(categories)
    try:
        summary = await run_pipeline(
            Repository(session_factory),
            clients.gnews,
            clients.gemini,
            clients.kbnews,
            categories=selected,
            triggered_by=source,
        )
    except RunInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    body = {
        "run_id": summary.run_id,
        "status": summary.status,
        "results": [asdict(result) for result in summary.results],
    }
    return JSONResponse(body, status_code=200 if summary.status == "success" else 500)
