"""Run the daily pipeline from the command line (used by the GitHub Actions workflow).

    python -m app.cli run [--categories world,india] [--source github-actions]

Exit code 0 when every category posted, was already posted or had no usable story; 1 otherwise.
Safe to run repeatedly: categories that already posted today are skipped.
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from app.api.jobs import Clients, parse_categories
from app.clients.gemini import GeminiClient
from app.clients.gnews import GNewsClient
from app.clients.kbnews import KbNewsClient
from app.db import SessionLocal
from app.services.pipeline import run_pipeline
from app.services.repository import Repository, RunInProgress

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO, and GNews takes its API key in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run(categories: str | None, source: str, session_factory=SessionLocal, clients=None) -> int:
    try:
        selected = parse_categories(categories)
    except Exception as exc:  # HTTPException carries the message in .detail
        print(getattr(exc, "detail", exc), file=sys.stderr)
        return 2

    async def execute(c: Clients) -> int:
        try:
            summary = await run_pipeline(
                Repository(session_factory), c.gnews, c.gemini, c.kbnews,
                categories=selected, triggered_by=source,
            )
        except RunInProgress as exc:
            print(f"Not started: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({
            "run_id": summary.run_id,
            "status": summary.status,
            "results": [asdict(r) for r in summary.results],
        }, indent=2, ensure_ascii=False))
        return 0 if summary.status == "success" else 1

    if clients is not None:
        return await execute(clients)
    async with GNewsClient() as gnews, GeminiClient() as gemini, KbNewsClient() as kbnews:
        return await execute(Clients(gnews, gemini, kbnews))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run the daily pipeline now")
    run_parser.add_argument("--categories", help="comma-separated subset, e.g. world,india")
    run_parser.add_argument("--source", default="cli", help="label stored on the run")
    args = parser.parse_args(argv)
    configure_logging()
    return asyncio.run(run(args.categories, args.source))


if __name__ == "__main__":
    sys.exit(main())
