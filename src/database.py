"""SQLite database setup and shared helpers for the tracker."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(os.getenv("TRACKER_DB_PATH", "tracker.db"))
EXPECTED_TABLES = ("channels", "videos", "transcripts", "analysis", "job_runs")


def utc_timestamp_sql() -> str:
    """Return SQLite expression text for consistent UTC timestamps."""
    return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with tracker defaults enabled."""
    logging.info("Opening SQLite connection to %s", db_path)
    connection = sqlite3.connect(Path(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create the tracker schema idempotently."""
    database_path = Path(db_path)
    logging.info("Initializing database at %s", database_path)

    if database_path.parent != Path("."):
        logging.info("Ensuring database parent directory exists: %s", database_path.parent)
        database_path.parent.mkdir(parents=True, exist_ok=True)

    schema_statements = [
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY,
            channel_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT,
            custom_url TEXT,
            url TEXT,
            uploads_playlist_id TEXT,
            thumbnail_url TEXT,
            subscriber_count INTEGER,
            video_count INTEGER,
            last_ingested_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY,
            video_id TEXT NOT NULL UNIQUE,
            channel_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            published_at TEXT,
            url TEXT NOT NULL,
            thumbnail_url TEXT,
            duration_seconds INTEGER,
            view_count INTEGER,
            like_count INTEGER,
            comment_count INTEGER,
            ingestion_source TEXT NOT NULL DEFAULT 'unknown',
            transcript_status TEXT NOT NULL DEFAULT 'pending',
            analysis_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY,
            video_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            language TEXT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            fetched_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (video_id) REFERENCES videos(video_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS analysis (
            id INTEGER PRIMARY KEY,
            video_id TEXT NOT NULL UNIQUE,
            summary TEXT,
            speakers_json TEXT NOT NULL DEFAULT '[]',
            topics_json TEXT NOT NULL DEFAULT '[]',
            keywords_json TEXT NOT NULL DEFAULT '[]',
            themes_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            model TEXT,
            prompt_version TEXT,
            raw_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            analyzed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (video_id) REFERENCES videos(video_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY,
            job_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            items_processed INTEGER NOT NULL DEFAULT 0,
            items_succeeded INTEGER NOT NULL DEFAULT 0,
            items_failed INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id)",
        "CREATE INDEX IF NOT EXISTS idx_videos_published_at ON videos(published_at)",
        "CREATE INDEX IF NOT EXISTS idx_transcripts_status ON transcripts(status)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_status ON analysis(status)",
        "CREATE INDEX IF NOT EXISTS idx_job_runs_job_name ON job_runs(job_name)",
    ]

    try:
        with get_connection(database_path) as connection:
            for statement in schema_statements:
                logging.info("Applying database schema statement")
                connection.execute(statement)
            connection.commit()
            logging.info("Database schema committed successfully")
    except sqlite3.Error:
        logging.exception("Database initialization failed")
        raise

    actual_tables = list_tables(database_path)
    missing_tables = sorted(set(EXPECTED_TABLES) - set(actual_tables))
    if missing_tables:
        logging.error("Database initialization missing tables: %s", ", ".join(missing_tables))
        raise RuntimeError(f"Missing required tables: {', '.join(missing_tables)}")

    logging.info("Database initialized with tables: %s", ", ".join(actual_tables))


def list_tables(db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    """Return user-created SQLite tables."""
    logging.info("Listing tables in database %s", db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    tables = [row["name"] for row in rows]
    logging.info("Found %d user tables", len(tables))
    return tables


def insert_job_run(
    job_name: str,
    status: str,
    started_at: str,
    finished_at: str | None = None,
    items_processed: int = 0,
    items_succeeded: int = 0,
    items_failed: int = 0,
    error_message: str | None = None,
    details: dict[str, Any] | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Insert a job run record and return its row id."""
    logging.info("Recording job run for %s with status %s", job_name, status)
    details_json = json.dumps(details or {}, sort_keys=True)
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO job_runs (
                job_name,
                status,
                started_at,
                finished_at,
                items_processed,
                items_succeeded,
                items_failed,
                error_message,
                details_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_name,
                status,
                started_at,
                finished_at,
                items_processed,
                items_succeeded,
                items_failed,
                error_message,
                details_json,
            ),
        )
        connection.commit()
        logging.info("Recorded job run id %s", cursor.lastrowid)
        return int(cursor.lastrowid)
