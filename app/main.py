import logging

from fastapi import FastAPI

from app.api.jobs import router as jobs_router

# Render shows stdout; without this, INFO logs from the pipeline would be dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="news-generator", version="0.1.0")
app.include_router(jobs_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
