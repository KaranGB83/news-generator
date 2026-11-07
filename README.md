# news-generator

Daily auto-poster for [kbNews](https://kbnews.onrender.com). Every day it picks the most-covered story
in three categories, rewrites each as a short original post with Gemini, and publishes it to kbNews
through a bot account:

| Category | Source query | Language |
| --- | --- | --- |
| `world` | GNews top headlines (world) | English |
| `india` | GNews top headlines (country `in`) | English |
| `maharashtra` | GNews search: Maharashtra, Mumbai, Pune, Nagpur | Marathi |

**Stack:** FastAPI, PostgreSQL (Supabase), SQLAlchemy + Alembic, httpx, Docker. Hosted on Render's
free tier and triggered daily by a GitHub Actions workflow.

## How a run works

```
GitHub Actions (07:00 IST)
        |  POST /jobs/run  (X-API-Key)
        v
  for each category:
    already posted today?  -> skip
    GNews: fetch ~10 candidate stories
    Gemini: group headlines by event; the story covered by the most outlets wins
    Gemini: write title + body (English or Marathi), or decline with a reason
    check language and length; one retry; unusable -> try the next story (up to 3)
    download the image, log in to kbNews, POST /api/news (multipart)
    record everything in Postgres (runs, articles)
```

Things worth knowing:

- **Safe to run twice.** A category that already posted today is skipped, so retries only redo failures.
- **No duplicates.** A story URL that was posted or skipped is never picked again. If a request to kbNews
  fails in a way that might have saved the post (timeout, 502/503/504), the client searches kbNews for a
  post with the same title before retrying.
- **Original wording with credit.** Gemini rewrites rather than copies, and every post ends with
  `Source: <outlet> (<url>)` (`स्रोत:` for Marathi).
- **Images are uploaded as files.** The service downloads the image, checks the real file type and the
  5 MB limit, and uploads it. If the download fails, the post goes out without a picture.
- **One bad category never stops the others.** The response says which failed and why.

## Project layout

```
app/
  main.py               FastAPI app, /health
  api/jobs.py           POST /jobs/run (API-key protected)
  config.py             settings from environment
  db.py, models.py      async engine (Supabase pooler safe), Run and Article tables
  schemas.py            GNews and Gemini data shapes
  prompts/              per-category prompts (English / Marathi) and skip rules
  clients/
    gnews.py            GNews top-headlines and search, retries, quota handling
    gemini.py           Gemini JSON output, validation, retries, safety blocks
    kbnews.py           bot login (cookie), multipart post, duplicate-safe retries
    images.py           image download with size cap and file-type check
  services/
    trending.py         pick the most-covered story
    generator.py        write the post, check language/length, add source credit
    pipeline.py         the per-category flow above
    repository.py       database access
alembic/                migrations
tests/                  pytest suite (no network, SQLite)
.github/workflows/daily.yml
```

## Configuration

Copy `.env.example` to `.env` for local use. On Render, set the same names under **Environment**.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres URL. On Render use Supabase's **transaction pooler** string (port 6543). |
| `API_KEY` | Shared secret for `/jobs/run`. Generate one with `openssl rand -hex 32`. |
| `GNEWS_API_KEY` | [GNews](https://gnews.io) key. |
| `GEMINI_API_KEY` | Google AI Studio key. |
| `GEMINI_MODEL` | A current free-tier Flash model ID (names change; check AI Studio). |
| `KBNEWS_BASE_URL` | kbNews backend, default `https://kbnews.onrender.com`. |
| `KBNEWS_BOT_EMAIL`, `KBNEWS_BOT_PASSWORD` | A verified kbNews account used for posting. |

A plain `postgresql://` URL is fine; the asyncpg driver is added automatically.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in values
docker compose up -d db         # local Postgres
alembic upgrade head
uvicorn app.main:app --reload
```

Or run everything in containers: `docker compose up --build`.

Run the tests (they need no network or Postgres; database tests use SQLite):

```bash
python -m pytest -q
```

## Deployment

### 1. Database (Supabase)

Create a project, then copy two connection strings from **Connect** (use the pooler ones; the direct
connection is IPv6-only and Render's free tier may not reach it):

- **Session pooler** (port 5432): for migrations from your machine.
- **Transaction pooler** (port 6543): for the app on Render.

```bash
docker compose run --rm --no-deps \
  -e DATABASE_URL='<session pooler string>' \
  app alembic upgrade head
```

### 2. Service (Render)

New **Web Service** from this repo: runtime **Docker**, free instance, health check path `/health`.
Add the environment variables above. Render redeploys on every push to `main`.

### 3. Schedule (GitHub Actions)

Add two repository secrets (**Settings → Secrets and variables → Actions**):

- `APP_URL`: the Render URL, no trailing slash
- `API_KEY`: exactly the same value as on Render

`.github/workflows/daily.yml` calls `/jobs/run` at 01:30 UTC (07:00 IST) and can also be started from
**Actions → Daily news run → Run workflow**. Its curl retries cover Render's cold start. GitHub pauses
scheduled workflows after 60 days without repository activity.

## Running the job by hand

```bash
export API_KEY='<your key>'

curl -i --max-time 600 -X POST "https://<your-app>.onrender.com/jobs/run?source=manual" \
  -H "X-API-Key: $API_KEY"

# only some categories
curl -i --max-time 600 -X POST "https://<your-app>.onrender.com/jobs/run?categories=world,india" \
  -H "X-API-Key: $API_KEY"
```

The call waits until the run finishes, which can take a few minutes (the first request also wakes the
free instance).

| Response | Meaning |
| --- | --- |
| 200 | Every category posted, was already posted today, or had no usable story |
| 500 | At least one category failed; the JSON body says which and why |
| 409 | Another run is still in progress |
| 401 | Wrong or missing `X-API-Key` |
| 503 | `API_KEY` is not set on the server (it never runs unprotected) |
| 404 | The deployed code is older than `/jobs/run`; check the Render deploy |

Each result has an `outcome`: `posted`, `already_posted`, `no_story` (nothing usable after up to three
candidates) or `failed`.

## Looking at what happened

```sql
select id, status, triggered_by, started_at, finished_at from runs order by id desc limit 5;
select category, status, attempts, kbnews_post_id, error from articles order by id desc limit 10;
```

Article statuses: `selected`, `generated`, `posted`, `skipped` (with the reason in `error`), `failed`.

## Troubleshooting

- **409 "run N is still in progress".** A run is active, or one was cut off by a redeploy and its row is
  stuck on `running`. It is treated as crashed after 30 minutes. To clear it now:
  `update runs set status = 'failed', finished_at = now() where status = 'running';`
- **401 from `/jobs/run`.** The key in the request differs from `API_KEY` on Render. Look for a stray
  space or newline when pasting, and remember a changed variable only applies after Render redeploys.
- **`kbNews login failed ... HTTP 429`.** The error text ends with `[x-powered-by=..., server=...]`.
  `Express` means kbNews itself answered; `absent` means a proxy or edge in front of it did (kbNews's
  own limiter replies with JSON, and its login route has none). Before logging in, the client wakes
  kbNews with a patient warm-up (up to 6 checks, 15 s apart), sends a `news-generator/1.0` User-Agent,
  honours `Retry-After`, and after one failed login it fails the remaining categories at once instead
  of retrying for each. To find out whether kbNews was asleep or the request was blocked, look at the
  kbNews service's Render logs for the same minute: no login request there means it never got past the
  proxy. Avoid triggering repeated runs while investigating.
- **A story was skipped.** Free-plan GNews returns truncated text, so some stories are too thin to write
  from. The reason is in `articles.error`; the pipeline moves on to the next candidate on its own.
- **Slow first request.** Render's free instances sleep after about 15 minutes of silence and take up to a
  minute to wake. Both this service and kbNews can be asleep; timeouts and retries allow for it.

## Limits to be aware of

- GNews's free plan: about 100 requests a day, up to 10 articles per request, articles delayed by about
  12 hours, and truncated `content`. A daily run makes one GNews request per category (3 in total), plus
  a few more if the workflow retries after a failure.
- kbNews's category list only offers General, Technology, Politics, Sports, Business, Health and World,
  so India and Maharashtra posts are filed under General. Change `KBNEWS_CATEGORY` in
  `app/clients/kbnews.py` once the frontend has dedicated categories.
- A story that stays on top for several days can reappear from a different outlet, since only exact URLs
  are de-duplicated.

## Ideas for later

- An admin page showing runs, articles, errors and a retry button (the tables already hold the data).
- Feed recently posted headlines to the picker so it avoids repeating a running story.
- Dedicated India and Maharashtra categories in kbNews.
