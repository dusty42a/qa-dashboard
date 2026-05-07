from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import router
from app.config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await db.init_db()
    yield


app = FastAPI(title="qa-dashboard", lifespan=lifespan)
app.include_router(router)

# Static assets (CSS/JS) are loaded by the HTML pages from /static/...
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.middleware("http")
async def no_cache_for_assets(request: Request, call_next):
    """Disable browser caching for HTML pages and JS/CSS so the dashboard
    can never get stuck rendering a stale combination of files. Cheap to
    revalidate against localhost; not worth a versioned-URL scheme."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/v/") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response
