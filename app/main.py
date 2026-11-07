import logging

from fastapi import FastAPI

from app.api.jobs import router as jobs_router

# Render shows stdout; without this, INFO logs from the pipeline would be dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO, and GNews takes its API key in the query string, so the key
# ended up in Render's logs. Our own clients log what is useful without URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="news-generator", version="0.1.0")
app.include_router(jobs_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
