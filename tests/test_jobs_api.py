from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.api import jobs
from app.clients.gnews import GNewsError
from app.main import app
from app.models import Run
from app.services.repository import Repository
from tests.fakes import FakeGemini, FakeGNews, FakeKbNews, fake_download

KEY = "secret-key"


@pytest.fixture
def world(session_factory, monkeypatch):
    fakes = SimpleNamespace(gnews=FakeGNews(), gemini=FakeGemini(), kbnews=FakeKbNews())

    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(api_key=KEY))
    app.dependency_overrides[jobs.get_session_factory] = lambda: session_factory
    app.dependency_overrides[jobs.get_clients] = lambda: jobs.Clients(
        fakes.gnews, fakes.gemini, fakes.kbnews
    )

    # No real image downloads or one-second pauses in tests.
    real_run = jobs.run_pipeline

    async def fast_run(*args, **kwargs):
        return await real_run(*args, download=fake_download, gnews_pause=0, **kwargs)

    monkeypatch.setattr(jobs, "run_pipeline", fast_run)

    yield fakes
    app.dependency_overrides.clear()


@pytest.fixture
async def client(world):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def auth(key: str = KEY) -> dict[str, str]:
    return {"X-API-Key": key}


async def test_health_needs_no_key(client):
    response = await client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_missing_key_is_rejected(client, world):
    response = await client.post("/jobs/run")
    assert response.status_code == 401
    assert world.gnews.calls == []


async def test_wrong_key_is_rejected(client, world):
    response = await client.post("/jobs/run", headers=auth("not-the-key"))
    assert response.status_code == 401
    assert world.kbnews.posts == []


async def test_unset_server_key_fails_closed(client, monkeypatch):
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(api_key=""))
    for headers in ({}, {"X-API-Key": ""}, auth()):
        response = await client.post("/jobs/run", headers=headers)
        assert response.status_code == 503


async def test_successful_run_returns_200_with_per_category_results(client, world, session_factory):
    response = await client.post("/jobs/run?source=github-actions", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert [(r["category"], r["outcome"]) for r in body["results"]] == [
        ("world", "posted"),
        ("india", "posted"),
        ("maharashtra", "posted"),
    ]
    assert all(r["kbnews_post_id"] for r in body["results"])
    assert len(world.kbnews.posts) == 3

    async with session_factory() as session:
        run = (await session.scalars(select(Run))).one()
        assert run.id == body["run_id"] and run.triggered_by == "github-actions"


async def test_default_source_is_manual(client, session_factory):
    await client.post("/jobs/run", headers=auth())
    async with session_factory() as session:
        assert (await session.scalars(select(Run))).one().triggered_by == "manual"


async def test_failed_category_returns_500_with_details(client, world):
    world.gnews.errors["india"] = GNewsError("GNews is down")

    response = await client.post("/jobs/run", headers=auth())

    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "partial"
    india = next(r for r in body["results"] if r["category"] == "india")
    assert india["outcome"] == "failed" and "GNews is down" in india["reason"]


async def test_second_call_same_day_is_a_safe_no_op(client, world):
    await client.post("/jobs/run", headers=auth())
    response = await client.post("/jobs/run", headers=auth())

    assert response.status_code == 200
    assert {r["outcome"] for r in response.json()["results"]} == {"already_posted"}
    assert len(world.kbnews.posts) == 3


async def test_category_subset(client, world):
    response = await client.post("/jobs/run?categories=world, maharashtra", headers=auth())

    assert response.status_code == 200
    assert [r["category"] for r in response.json()["results"]] == ["world", "maharashtra"]
    assert len(world.kbnews.posts) == 2


async def test_unknown_category_is_422_and_runs_nothing(client, world, session_factory):
    response = await client.post("/jobs/run?categories=world,sports", headers=auth())

    assert response.status_code == 422
    assert "sports" in response.json()["detail"]
    assert world.gnews.calls == []
    async with session_factory() as session:
        assert (await session.scalars(select(Run))).all() == []


async def test_run_in_progress_returns_409(client, world, session_factory):
    await Repository(session_factory).start_run("github-actions")

    response = await client.post("/jobs/run", headers=auth())

    assert response.status_code == 409
    assert "still in progress" in response.json()["detail"]
    assert world.kbnews.posts == []
