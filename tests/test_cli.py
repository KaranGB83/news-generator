import json
from types import SimpleNamespace

from sqlalchemy import select

from app import cli
from app.api.jobs import Clients
from app.models import Run
from tests.fakes import FakeGemini, FakeGNews, FakeKbNews, fake_download


def make_clients(**overrides):
    fakes = SimpleNamespace(gnews=FakeGNews(), gemini=FakeGemini(), kbnews=FakeKbNews())
    for key, value in overrides.items():
        setattr(fakes, key, value)
    return fakes, Clients(fakes.gnews, fakes.gemini, fakes.kbnews)


def fast(monkeypatch):
    real = cli.run_pipeline

    async def quick(*args, **kwargs):
        return await real(*args, download=fake_download, gnews_pause=0, **kwargs)

    monkeypatch.setattr(cli, "run_pipeline", quick)


async def test_success_exits_zero_and_prints_summary(session_factory, monkeypatch, capsys):
    fast(monkeypatch)
    fakes, clients = make_clients()
    code = await cli.run(None, "github-actions", session_factory=session_factory, clients=clients)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "success"
    assert [r["outcome"] for r in out["results"]] == ["posted"] * 3
    assert len(fakes.kbnews.posts) == 3
    async with session_factory() as s:
        run = (await s.execute(select(Run))).scalar_one()
    assert run.triggered_by == "github-actions"


async def test_failure_exits_one(session_factory, monkeypatch, capsys):
    from app.clients.gnews import GNewsError

    fast(monkeypatch)
    gnews = FakeGNews()
    gnews.errors["world"] = GNewsError("boom")
    _, clients = make_clients(gnews=gnews)
    clients.gnews = gnews
    code = await cli.run("world", "cli", session_factory=session_factory, clients=clients)
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


async def test_second_run_is_a_no_op_and_succeeds(session_factory, monkeypatch, capsys):
    fast(monkeypatch)
    fakes, clients = make_clients()
    await cli.run(None, "cli", session_factory=session_factory, clients=clients)
    capsys.readouterr()
    code = await cli.run(None, "cli", session_factory=session_factory, clients=clients)
    assert code == 0
    assert len(fakes.kbnews.posts) == 3


async def test_unknown_category_exits_two(session_factory, capsys):
    _, clients = make_clients()
    code = await cli.run("nope", "cli", session_factory=session_factory, clients=clients)
    assert code == 2
    assert "Unknown category" in capsys.readouterr().err


async def test_run_in_progress_exits_one(session_factory, monkeypatch, capsys):
    fast(monkeypatch)
    await __import__("app.services.repository", fromlist=["Repository"]).Repository(session_factory).start_run("other")
    _, clients = make_clients()
    code = await cli.run(None, "cli", session_factory=session_factory, clients=clients)
    assert code == 1
    assert "still in progress" in capsys.readouterr().err


def test_main_rejects_missing_command():
    import pytest

    with pytest.raises(SystemExit):
        cli.main([])
