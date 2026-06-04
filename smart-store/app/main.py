"""FastAPI entrypoint.

Wires routers, starts/stops storage in lifespan, applies request-logging
middleware, mounts the dashboard static files, and registers a clean global
exception handler that turns storage outages into HTTP 503 with a structured
JSON body — never raw stack traces in responses.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import StorageUnavailable, init_storage, shutdown_storage
from .logging_setup import RequestLogMiddleware, configure_logging
from .pos import load_pos_csv

# Routers
from .anomalies import router as anomalies_router
from .funnel import router as funnel_router
from .health import router as health_router
from .heatmap import router as heatmap_router
from .ingestion import router as ingest_router
from .insights import router as insights_router
from .metrics import router as metrics_router
from .ws import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.api_log_level)
    log = logging.getLogger("api.main")
    await init_storage()
    try:
        n = await load_pos_csv()
        log.info("startup.pos_loaded rows=%d", n)
    except Exception as e:  # noqa: BLE001
        log.warning("startup.pos_load_failed err=%s", e)
    yield
    await shutdown_storage()


app = FastAPI(
    title="Apex Retail — Store Intelligence API",
    version="0.1.0",
    description=(
        "Ingests CCTV-derived behavioural events, exposes real-time metrics, funnel, "
        "heatmap, and anomaly endpoints. North Star: offline store conversion rate."
    ),
    lifespan=lifespan,
)

app.add_middleware(RequestLogMiddleware)


@app.exception_handler(StorageUnavailable)
async def _storage_unavailable_handler(request: Request, exc: StorageUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "DB_UNAVAILABLE", "retry_after": 5, "message": str(exc)},
    )


app.include_router(ingest_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(heatmap_router)
app.include_router(anomalies_router)
app.include_router(insights_router)
app.include_router(health_router)
app.include_router(ws_router)


# Mount the live dashboard at /dashboard if the static folder exists.
_dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard"
if _dashboard_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")


@app.get("/", include_in_schema=False)
async def _root():
    return {
        "service": "store-intelligence",
        "endpoints": [
            "POST /events/ingest",
            "GET /stores/{id}/metrics",
            "GET /stores/{id}/funnel",
            "GET /stores/{id}/heatmap",
            "GET /stores/{id}/anomalies",
            "GET /stores/{id}/insights",
            "GET /health",
            "WS /ws/{store_id}",
            "GET /dashboard/",
        ],
    }
