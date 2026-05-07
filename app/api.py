from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import db, refresh, storage
from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"


def _bg(coro) -> None:
    """Fire-and-forget a coroutine on the running loop."""
    asyncio.create_task(coro)


# ---- HTML page routes (just serve the static files) -----------------------


@router.get("/", include_in_schema=False)
async def landing() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/v/{name}", include_in_schema=False)
async def version_page(name: str) -> FileResponse:
    # The page itself loads regardless; the JS calls /api/v/{name} and shows
    # the not-found state if the API responds 404.
    return FileResponse(STATIC_DIR / "version.html")


# ---- JSON API --------------------------------------------------------------


@router.get("/api/versions")
async def api_versions():
    await refresh.ensure_versions_present()
    async with db.connect() as conn:
        await refresh.maybe_refresh_versions_in_background(_bg)
        versions = await storage.list_versions_cached(conn)
        last = await storage.last_fetched_at(conn, "versions")
    return {"versions": versions, "last_fetched_at": last}


@router.get("/api/v/{name}")
async def api_version(name: str):
    await refresh.ensure_versions_present()
    async with db.connect() as conn:
        version = await storage.find_version_by_name(conn, name)

    if not version:
        # One synchronous retry in case the cache was warm but stale and the
        # version has since been added in Redmine.
        await refresh.refresh_versions()
        async with db.connect() as conn:
            version = await storage.find_version_by_name(conn, name)
        if not version:
            raise HTTPException(status_code=404, detail={"error": "version_not_found", "name": name})

    await refresh.maybe_refresh_version_in_background(version["id"], _bg)

    async with db.connect() as conn:
        # If we've never fetched this version, do a synchronous bootstrap so
        # the first page load returns something usable.
        last = await storage.last_fetched_at(conn, f"version:{version['id']}")
    if not last:
        await refresh.refresh_version(version["id"])

    async with db.connect() as conn:
        payload = await storage.build_version_payload(conn, version)
    payload["redmine_base"] = settings.redmine_base
    return payload
