"""FastAPI app entrypoint: wires up the webhook receiver and the Approval UI."""
from __future__ import annotations

import logging

from fastapi import FastAPI

from . import audit
from .logging_utils import logger
from .ui.routes import router as ui_router
from .webhook import router as webhook_router

app = FastAPI(title="Kubernetes Incident Remediation Agent")


@app.on_event("startup")
def on_startup() -> None:
    audit.init_db()
    logging.getLogger("uvicorn.access").propagate = False
    logger.info("=== Remediation agent started: audit DB ready, waiting for Alertmanager webhooks ===")


app.include_router(webhook_router)
app.include_router(ui_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
