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

## Docker

```bash
docker compose up --build
```
