"""Dashboard export helpers for JSON snapshot generation."""

from __future__ import annotations

import json
import logging
from contextlib import closing
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.database import DEFAULT_DB_PATH, get_connection, initialize_database

DEFAULT_SNAPSHOT_PATH = Path("docs/data/latest.json")


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_json_list(value: str | None) -> list[Any]:
    """Decode a JSON array safely."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logging.error("Failed to decode JSON list value")
        return []
    return parsed if isinstance(parsed, list) else []


def safe_json_object(value: str | None) -> dict[str, Any]:
    """Decode a JSON object safely."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logging.error("Failed to decode JSON object value")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def compute_video_status(transcript_status: str | None, analysis_status: str | None) -> str:
    """Derive a simple pipeline status for dashboard display."""
    transcript_status = transcript_status or "pending"
    analysis_status = analysis_status or "pending"
    if transcript_status == "complete" and analysis_status == "complete":
        return "ready"
    if transcript_status == "pending":
        return "awaiting_transcript"
    if transcript_status == "failed":
        return "transcript_failed"
    if analysis_status in {"pending", "retryable_error"}:
        return "awaiting_analysis"
    if analysis_status == "transcript_failed":
        return "transcript_failed"
    return analysis_status


def query_stats(connection: Any) -> dict[str, Any]:
    """Compute dashboard statistics from the current database state."""
    logging.info("Computing dashboard statistics")
    channel_total = connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    video_total = connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    transcript_total = connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    analysis_total = connection.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]

    transcript_status_counts = {
        row["status"]: row["count"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM transcripts GROUP BY status ORDER BY status"
        )
    }
    analysis_status_counts = {
        row["status"]: row["count"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM analysis GROUP BY status ORDER BY status"
        )
    }
    video_transcript_counts = {
        row["transcript_status"]: row["count"]
        for row in connection.execute(
            "SELECT transcript_status, COUNT(*) AS count FROM videos GROUP BY transcript_status ORDER BY transcript_status"
        )
    }
    video_analysis_counts = {
        row["analysis_status"]: row["count"]
        for row in connection.execute(
            "SELECT analysis_status, COUNT(*) AS count FROM videos GROUP BY analysis_status ORDER BY analysis_status"
        )
    }
    latest_job_runs = [
        dict(row)
        for row in connection.execute(
            """
            SELECT job_name, status, started_at, finished_at, items_processed,
                   items_succeeded, items_failed, error_message
            FROM job_runs
            ORDER BY id DESC
            LIMIT 5
            """
        ).fetchall()
    ]

    return {
        "channels": {"total": channel_total},
        "videos": {
            "total": video_total,
            "by_transcript_status": video_transcript_counts,
            "by_analysis_status": video_analysis_counts,
            "ready": video_analysis_counts.get("complete", 0),
            "awaiting_transcript": video_transcript_counts.get("pending", 0),
            "awaiting_analysis": video_analysis_counts.get("pending", 0)
            + video_analysis_counts.get("retryable_error", 0),
        },
        "transcripts": {
            "total": transcript_total,
            "by_status": transcript_status_counts,
        },
        "analysis": {
            "total": analysis_total,
            "by_status": analysis_status_counts,
        },
        "jobs": {
            "recent": latest_job_runs,
        },
    }


def query_channels(connection: Any) -> list[dict[str, Any]]:
    """Return dashboard-ready channel rows."""
    logging.info("Querying dashboard channels")
    rows = connection.execute(
        """
        SELECT
            c.channel_id,
            c.title,
            c.url,
            c.custom_url,
            c.thumbnail_url,
            c.subscriber_count,
            c.video_count,
            c.last_ingested_at,
            COUNT(v.video_id) AS videos_total,
            SUM(CASE WHEN t.status = 'complete' THEN 1 ELSE 0 END) AS transcripts_complete,
            SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS transcripts_failed,
            SUM(CASE WHEN a.status = 'complete' THEN 1 ELSE 0 END) AS analysis_complete,
            SUM(CASE WHEN a.status = 'transcript_failed' THEN 1 ELSE 0 END) AS analysis_transcript_failed,
            MAX(v.published_at) AS latest_published_at
        FROM channels c
        LEFT JOIN videos v ON v.channel_id = c.channel_id
        LEFT JOIN transcripts t ON t.video_id = v.video_id
        LEFT JOIN analysis a ON a.video_id = v.video_id
        GROUP BY c.channel_id
        ORDER BY COALESCE(MAX(v.published_at), c.last_ingested_at, '') DESC, c.title ASC
        """
    ).fetchall()

    channels: list[dict[str, Any]] = []
    for row in rows:
        channels.append(
            {
                "channel_id": row["channel_id"],
                "title": row["title"],
                "url": row["url"],
                "custom_url": row["custom_url"],
                "thumbnail_url": row["thumbnail_url"],
                "subscriber_count": row["subscriber_count"],
                "video_count": row["video_count"],
                "last_ingested_at": row["last_ingested_at"],
                "videos_total": row["videos_total"] or 0,
                "transcripts_complete": row["transcripts_complete"] or 0,
                "transcripts_failed": row["transcripts_failed"] or 0,
                "analysis_complete": row["analysis_complete"] or 0,
                "analysis_transcript_failed": row["analysis_transcript_failed"] or 0,
                "latest_published_at": row["latest_published_at"],
            }
        )
    return channels


def query_videos(connection: Any) -> list[dict[str, Any]]:
    """Return dashboard-ready video rows."""
    logging.info("Querying dashboard videos")
    rows = connection.execute(
        """
        SELECT
            v.video_id,
            v.title,
            v.published_at,
            v.url,
            v.thumbnail_url,
            v.transcript_status,
            v.analysis_status,
            v.ingestion_source,
            c.channel_id,
            c.title AS channel_title,
            t.source AS transcript_source,
            t.status AS transcript_row_status,
            t.error_message AS transcript_error,
            a.summary,
            a.speakers_json,
            a.topics_json,
            a.keywords_json,
            a.themes_json,
            a.confidence,
            a.model,
            a.prompt_version,
            a.status AS analysis_row_status,
            a.error_message AS analysis_error
        FROM videos v
        JOIN channels c ON c.channel_id = v.channel_id
        LEFT JOIN transcripts t ON t.video_id = v.video_id
        LEFT JOIN analysis a ON a.video_id = v.video_id
        ORDER BY COALESCE(v.published_at, '') DESC, v.id DESC
        """
    ).fetchall()

    videos: list[dict[str, Any]] = []
    for row in rows:
        transcript_source = row["transcript_source"] or "pending"
        analysis_status = row["analysis_status"] or row["analysis_row_status"] or "pending"
        transcript_status = row["transcript_status"] or row["transcript_row_status"] or "pending"
        videos.append(
            {
                "video_id": row["video_id"],
                "date": (row["published_at"] or "")[:10] or row["published_at"],
                "published_at": row["published_at"],
                "channel_id": row["channel_id"],
                "channel": row["channel_title"],
                "title": row["title"],
                "url": row["url"],
                "thumbnail_url": row["thumbnail_url"],
                "speakers": safe_json_list(row["speakers_json"]),
                "topics": safe_json_list(row["topics_json"]),
                "themes": safe_json_list(row["themes_json"]),
                "summary": row["summary"] or "",
                "transcript_source": transcript_source,
                "transcript_status": transcript_status,
                "analysis_status": analysis_status,
                "analysis_model": row["model"],
                "prompt_version": row["prompt_version"],
                "status": compute_video_status(transcript_status, analysis_status),
                "confidence": row["confidence"],
                "ingestion_source": row["ingestion_source"],
                "transcript_error": row["transcript_error"],
                "analysis_error": row["analysis_error"],
            }
        )
    return videos


def build_dashboard_data(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Build the full dashboard snapshot payload from SQLite."""
    logging.info("Building dashboard snapshot from database %s", db_path)
    initialize_database(db_path)
    with closing(get_connection(db_path)) as connection:
        stats = query_stats(connection)
        channels = query_channels(connection)
        videos = query_videos(connection)

    payload = {
        "generated_at": utc_now(),
        "stats": stats,
        "channels": channels,
        "videos": videos,
    }
    logging.info(
        "Built dashboard snapshot with %d channels and %d videos",
        len(channels),
        len(videos),
    )
    return payload


def export_dashboard_snapshot(
    db_path: Path | str = DEFAULT_DB_PATH,
    output_path: Path | str = DEFAULT_SNAPSHOT_PATH,
) -> dict[str, Any]:
    """Write the current dashboard snapshot to disk and return the payload."""
    payload = build_dashboard_data(db_path)
    target_path = Path(output_path)
    logging.info("Writing dashboard snapshot to %s", target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    logging.info("Dashboard snapshot written successfully")
    return payload
