# news-generator

Daily auto-poster for [kbNews](https://kbnews.onrender.com). Fetches the most-covered story of the day
from GNews in three categories (world, India, Maharashtra in Marathi), rewrites it with Gemini Flash and
posts it to kbNews as a bot account.

Stack: FastAPI, PostgreSQL (Supabase), Docker. Triggered daily by a GitHub Actions workflow.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in values
uvicorn app.main:app --reload
```

Health check: `GET /health`

## Database

```bash
alembic upgrade head
```

## Running the daily job

`POST /jobs/run` fetches, writes and posts one story per category (world, india, maharashtra). It
needs the `X-API-Key` header to match `API_KEY`, and it waits for the whole run to finish.

```bash
curl -X POST "http://localhost:8000/jobs/run" -H "X-API-Key: $API_KEY"

# only some categories, tagged as a manual run
curl -X POST "http://localhost:8000/jobs/run?categories=world,india&source=manual" \
  -H "X-API-Key: $API_KEY"
```

| Response | Meaning |
| --- | --- |
| 200 | Every category posted, was already posted today, or had no usable story |
| 500 | At least one category failed; the JSON body says which and why |
| 409 | Another run is still in progress |
| 401 / 503 | Wrong or missing API key / `API_KEY` not set on the server |

It is safe to run again the same day: a category that already posted is skipped, so only failed
categories are retried. The GitHub Actions workflow relies on this when curl retries after a 500.
Each attempt is recorded in the `runs` and `articles` tables.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The database tests run on SQLite, so no Postgres is needed.

## Docker

```bash
docker compose up --build
```
