"""FastAPI backend for the tracker dashboard."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.database import DEFAULT_DB_PATH
from src.export import DEFAULT_SNAPSHOT_PATH, build_dashboard_data, export_dashboard_snapshot


def create_app(
    db_path: Path | str = DEFAULT_DB_PATH,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
) -> FastAPI:
    """Create the FastAPI application."""
    logging.info("Creating FastAPI application for db=%s snapshot=%s", db_path, snapshot_path)
    app = FastAPI(title="LLM YouTube Landscape Tracker", version="1.0.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Report basic service health."""
        logging.info("Health check requested")
        try:
            payload = build_dashboard_data(db_path)
            return {
                "status": "ok",
                "database": str(db_path),
                "channels": len(payload["channels"]),
                "videos": len(payload["videos"]),
            }
        except Exception as exc:
            logging.exception("Health check failed")
            return {"status": "error", "database": str(db_path), "error": str(exc)}

    @app.get("/channels")
    def channels() -> list[dict[str, Any]]:
        """Return the current dashboard channels."""
        logging.info("/channels requested")
        payload = build_dashboard_data(db_path)
        return payload["channels"]

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        """Return dashboard statistics."""
        logging.info("/stats requested")
        payload = build_dashboard_data(db_path)
        return payload["stats"]

    @app.get("/videos")
    def videos() -> list[dict[str, Any]]:
        """Return the current dashboard videos."""
        logging.info("/videos requested")
        payload = build_dashboard_data(db_path)
        return payload["videos"]

    @app.get("/dashboard-data")
    def dashboard_data() -> dict[str, Any]:
        """Return the full dashboard snapshot."""
        logging.info("/dashboard-data requested")
        return build_dashboard_data(db_path)

    @app.post("/refresh")
    def refresh() -> dict[str, Any]:
        """Refresh the snapshot file and return the regenerated payload."""
        logging.info("/refresh requested")
        payload = export_dashboard_snapshot(db_path=db_path, output_path=snapshot_path)
        return {
            "status": "refreshed",
            "snapshot_path": str(snapshot_path),
            "dashboard_data": payload,
        }

    @app.exception_handler(Exception)
    def handle_exception(_: Any, exc: Exception) -> JSONResponse:
        """Return a consistent error payload for unexpected failures."""
        logging.exception("Unhandled API error")
        return JSONResponse(status_code=500, content={"status": "error", "error": str(exc)})

    return app


def run_server(
    db_path: Path | str = DEFAULT_DB_PATH,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the FastAPI server with Uvicorn."""
    logging.info("Starting API server on %s:%d", host, port)
    import uvicorn

    app = create_app(db_path=db_path, snapshot_path=snapshot_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
