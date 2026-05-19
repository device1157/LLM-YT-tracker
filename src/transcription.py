"""Transcript extraction and persistence."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter

from src.database import DEFAULT_DB_PATH, get_connection, initialize_database, insert_job_run

DEFAULT_LANGUAGES = ("en", "en-US", "en-GB")
FAILED_TRANSCRIPT_TEXT = "[Transcript unavailable: captions could not be fetched and Whisper fallback did not run.]"
MAX_ERROR_LENGTH = 1200


@dataclass(frozen=True)
class VideoForTranscript:
    """Minimal video data needed to fetch a transcript."""

    video_id: str
    title: str
    url: str


@dataclass(frozen=True)
class TranscriptResult:
    """Normalized transcript fetch result."""

    video_id: str
    source: str
    language: str | None
    text: str
    status: str
    error_message: str | None


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_videos_missing_transcripts(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 10,
) -> list[VideoForTranscript]:
    """Return videos with no terminal transcript row yet."""
    logging.info("Querying up to %d videos missing transcripts", limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT v.video_id, v.title, v.url
            FROM videos v
            LEFT JOIN transcripts t ON t.video_id = v.video_id
            WHERE t.video_id IS NULL
               OR t.status = 'pending'
               OR v.transcript_status = 'pending'
            ORDER BY v.published_at DESC, v.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    videos = [
        VideoForTranscript(
            video_id=row["video_id"],
            title=row["title"],
            url=row["url"],
        )
        for row in rows
    ]
    logging.info("Found %d videos missing transcripts", len(videos))
    return videos


def pick_transcript(transcript_list: Any, languages: tuple[str, ...]) -> Any:
    """Pick the best available transcript for the configured languages."""
    logging.info("Selecting transcript from preferred languages: %s", ", ".join(languages))
    selection_attempts = (
        ("manual_preferred", lambda: transcript_list.find_manually_created_transcript(languages)),
        ("generated_preferred", lambda: transcript_list.find_generated_transcript(languages)),
        ("any_preferred", lambda: transcript_list.find_transcript(languages)),
    )

    last_error: Exception | None = None
    for label, selector in selection_attempts:
        try:
            transcript = selector()
            logging.info("Selected %s transcript with language %s", label, transcript.language_code)
            return transcript
        except Exception as exc:
            last_error = exc
            logging.info("Transcript selection %s did not match: %s", label, exc)

    for transcript in transcript_list:
        logging.info("Falling back to first listed transcript with language %s", transcript.language_code)
        return transcript

    if last_error:
        raise last_error
    raise RuntimeError("No transcripts are listed for this video")


def fetch_youtube_caption(video_id: str, languages: tuple[str, ...]) -> TranscriptResult:
    """Fetch a transcript using youtube-transcript-api."""
    logging.info("Fetching YouTube captions for video %s", video_id)
    transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    transcript = pick_transcript(transcript_list, languages)
    segments = transcript.fetch()
    logging.info("Fetched %d transcript segments for video %s", len(segments), video_id)

    text = TextFormatter().format_transcript(segments).strip()
    if not text:
        logging.error("Fetched transcript for %s was empty", video_id)
        raise RuntimeError("Fetched transcript was empty")

    source = "youtube_auto_captions" if transcript.is_generated else "youtube_captions"
    return TranscriptResult(
        video_id=video_id,
        source=source,
        language=transcript.language_code,
        text=text,
        status="complete",
        error_message=None,
    )


def fetch_whisper_fallback(video: VideoForTranscript, previous_error: Exception) -> TranscriptResult:
    """Download audio and use OpenAI Whisper as a fallback."""
    has_api_key = bool(os.getenv("OPENAI_API_KEY"))
    if not has_api_key:
        return TranscriptResult(
            video_id=video.video_id,
            source="failed",
            language=None,
            text=FAILED_TRANSCRIPT_TEXT,
            status="failed",
            error_message="OPENAI_API_KEY missing, Whisper fallback cannot run."
        )

    logging.info("Falling back to Whisper API for %s", video.video_id)
    import tempfile
    from yt_dlp import YoutubeDL
    from openai import OpenAI

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download the lowest quality audio to respect OpenAI's 25MB Whisper limit
        ydl_opts = {
            'format': 'worstaudio/worst',
            'outtmpl': f'{tmpdir}/%(id)s.%(ext)s',
            'quiet': True,
        }
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video.url, download=True)
                audio_path = ydl.prepare_filename(info)

            client = OpenAI()
            with open(audio_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )

            return TranscriptResult(
                video_id=video.video_id,
                source="openai_whisper",
                language="en",
                text=transcript.text,
                status="complete",
                error_message=None,
            )
        except Exception as e:
            logging.exception("Whisper fallback failed for %s", video.video_id)
            return TranscriptResult(
                video_id=video.video_id,
                source="failed",
                language=None,
                text=FAILED_TRANSCRIPT_TEXT,
                status="failed",
                error_message=truncate_error(f"Captions failed: {previous_error}. Whisper failed: {e}")
            )


def truncate_error(message: str) -> str:
    """Keep persisted extraction errors readable and bounded."""
    if len(message) <= MAX_ERROR_LENGTH:
        return message
    logging.info("Truncating long transcript error from %d characters", len(message))
    return message[: MAX_ERROR_LENGTH - 3] + "..."


def fetch_transcript(video: VideoForTranscript, languages: tuple[str, ...]) -> TranscriptResult:
    """Fetch transcript text for a video, falling back to Whisper when captions fail."""
    logging.info("Starting transcript fetch for %s (%s)", video.video_id, video.title)
    try:
        return fetch_youtube_caption(video.video_id, languages)
    except Exception as exc:
        logging.exception("YouTube caption fetch failed for %s", video.video_id)
        return fetch_whisper_fallback(video, exc)


def save_transcript(
    connection: Any,
    result: TranscriptResult,
    fetched_at: str,
) -> None:
    """Upsert transcript text and update the owning video's transcript status."""
    logging.info("Saving transcript result for %s with status %s", result.video_id, result.status)
    connection.execute(
        """
        INSERT INTO transcripts (
            video_id,
            source,
            language,
            text,
            status,
            error_message,
            fetched_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            source = excluded.source,
            language = excluded.language,
            text = excluded.text,
            status = excluded.status,
            error_message = excluded.error_message,
            fetched_at = excluded.fetched_at,
            updated_at = excluded.updated_at
        """,
        (
            result.video_id,
            result.source,
            result.language,
            result.text,
            result.status,
            result.error_message,
            fetched_at,
            fetched_at,
        ),
    )
    connection.execute(
        """
        UPDATE videos
        SET transcript_status = ?,
            analysis_status = CASE
                WHEN ? = 'complete' AND analysis_status = 'transcript_failed' THEN 'pending'
                ELSE analysis_status
            END,
            updated_at = ?
        WHERE video_id = ?
        """,
        (
            result.status,
            result.status,
            fetched_at,
            result.video_id,
        ),
    )


def transcribe_missing_videos(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 10,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
) -> dict[str, Any]:
    """Run the transcript extraction job."""
    logging.info("Starting transcript job")
    load_dotenv()
    started_at = utc_now()
    initialize_database(db_path)
    videos = get_videos_missing_transcripts(db_path=db_path, limit=limit)

    processed = 0
    succeeded = 0
    failed = 0
    errors: list[str] = []
    source_counts: dict[str, int] = {}

    with get_connection(db_path) as connection:
        for video in videos:
            processed += 1
            logging.info("Processing transcript for video %s", video.video_id)
            result = fetch_transcript(video, languages)
            save_transcript(connection, result, utc_now())
            connection.commit()

            source_counts[result.source] = source_counts.get(result.source, 0) + 1
            if result.status == "complete":
                succeeded += 1
                logging.info("Transcript complete for video %s", video.video_id)
            else:
                failed += 1
                message = f"{video.video_id}: {result.error_message}"
                errors.append(message)
                logging.error("Transcript failed for video %s", video.video_id)

    status = "success" if failed == 0 else "partial"
    finished_at = utc_now()
    details = {
        "limit": limit,
        "languages": list(languages),
        "source_counts": source_counts,
    }
    logging.info("Recording transcript job run with status %s", status)
    insert_job_run(
        job_name="transcript",
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        items_processed=processed,
        items_succeeded=succeeded,
        items_failed=failed,
        error_message="\n".join(errors) if errors else None,
        details=details,
        db_path=db_path,
    )

    result_payload = {
        "status": status,
        "videos_processed": processed,
        "videos_succeeded": succeeded,
        "videos_failed": failed,
        "errors": errors,
    }
    logging.info("Transcript job completed: %s", result_payload)
    return result_payload
