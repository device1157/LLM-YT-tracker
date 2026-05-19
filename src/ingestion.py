"""YouTube channel and video ingestion."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.database import DEFAULT_DB_PATH, get_connection, initialize_database, insert_job_run

DEFAULT_CHANNELS_CONFIG = Path("config/channels.json")


@dataclass(frozen=True)
class ChannelConfig:
    """Configured YouTube channel target."""

    channel_id: str
    name: str
    url: str


@dataclass(frozen=True)
class ChannelRecord:
    """Normalized channel data ready for database upsert."""

    channel_id: str
    title: str
    description: str | None
    custom_url: str | None
    url: str
    uploads_playlist_id: str | None
    thumbnail_url: str | None
    subscriber_count: int | None
    video_count: int | None


@dataclass(frozen=True)
class VideoRecord:
    """Normalized video data ready for database upsert."""

    video_id: str
    channel_id: str
    title: str
    description: str | None
    published_at: str | None
    url: str
    thumbnail_url: str | None
    duration_seconds: int | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    ingestion_source: str


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_channel_config(config_path: Path | str = DEFAULT_CHANNELS_CONFIG) -> list[ChannelConfig]:
    """Load channel targets from config/channels.json."""
    path = Path(config_path)
    logging.info("Loading channel configuration from %s", path)
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    channels: list[ChannelConfig] = []
    for item in payload.get("channels", []):
        channel_id = str(item.get("id", "")).strip()
        name = str(item.get("name", channel_id)).strip()
        url = str(item.get("url") or f"https://www.youtube.com/channel/{channel_id}/videos").strip()
        if not channel_id:
            logging.error("Skipping channel config entry without an id: %s", item)
            continue
        channels.append(ChannelConfig(channel_id=channel_id, name=name, url=url))

    logging.info("Loaded %d channel targets", len(channels))
    if not channels:
        raise ValueError(f"No channels configured in {path}")
    return channels


def parse_int(value: Any) -> int | None:
    """Parse an optional integer from API strings or numbers."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logging.error("Unable to parse integer value: %s", value)
        return None


def parse_iso8601_duration(value: str | None) -> int | None:
    """Parse a YouTube ISO-8601 duration like PT1H2M3S to seconds."""
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        value,
    )
    if not match:
        logging.error("Unable to parse ISO-8601 duration: %s", value)
        return None
    parts = {key: int(number) if number else 0 for key, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def normalize_upload_date(value: Any) -> str | None:
    """Normalize ytdlp upload dates to ISO-ish dates."""
    if not value:
        return None
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}T00:00:00Z"
    return text


def best_thumbnail(thumbnails: Any) -> str | None:
    """Return the highest-quality thumbnail URL available."""
    if isinstance(thumbnails, dict):
        ordered = ["maxres", "standard", "high", "medium", "default"]
        for key in ordered:
            url = thumbnails.get(key, {}).get("url")
            if url:
                return str(url)
    if isinstance(thumbnails, list) and thumbnails:
        sorted_items = sorted(
            thumbnails,
            key=lambda item: parse_int(item.get("width")) or 0 if isinstance(item, dict) else 0,
            reverse=True,
        )
        first = sorted_items[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
    return None


def video_url(video_id: str) -> str:
    """Build a canonical YouTube watch URL."""
    return f"https://www.youtube.com/watch?v={video_id}"


def uploads_playlist_url(channel_id: str) -> str:
    """Derive the uploads playlist URL from a YouTube channel id."""
    if not channel_id.startswith("UC") or len(channel_id) < 3:
        return ""
    playlist_id = "UU" + channel_id[2:]
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def build_youtube_service(api_key: str) -> Any:
    """Build a YouTube Data API client lazily."""
    logging.info("Building YouTube Data API client")
    from googleapiclient.discovery import build

    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def fetch_with_youtube_api(
    channel: ChannelConfig,
    youtube: Any,
    max_videos: int,
) -> tuple[ChannelRecord, list[VideoRecord]]:
    """Fetch channel and latest videos through YouTube Data API v3."""
    logging.info("Fetching channel %s via YouTube Data API", channel.channel_id)
    channel_response = (
        youtube.channels()
        .list(part="snippet,contentDetails,statistics", id=channel.channel_id)
        .execute()
    )
    channel_items = channel_response.get("items", [])
    if not channel_items:
        raise RuntimeError(f"YouTube API returned no channel for {channel.channel_id}")

    channel_item = channel_items[0]
    snippet = channel_item.get("snippet", {})
    statistics = channel_item.get("statistics", {})
    content_details = channel_item.get("contentDetails", {})
    related_playlists = content_details.get("relatedPlaylists", {})

    channel_record = ChannelRecord(
        channel_id=channel.channel_id,
        title=str(snippet.get("title") or channel.name),
        description=snippet.get("description"),
        custom_url=snippet.get("customUrl"),
        url=channel.url,
        uploads_playlist_id=related_playlists.get("uploads"),
        thumbnail_url=best_thumbnail(snippet.get("thumbnails")),
        subscriber_count=parse_int(statistics.get("subscriberCount")),
        video_count=parse_int(statistics.get("videoCount")),
    )

    search_response = (
        youtube.search()
        .list(
            part="snippet",
            channelId=channel.channel_id,
            maxResults=min(max_videos, 50),
            order="date",
            type="video",
        )
        .execute()
    )
    ids = [
        item.get("id", {}).get("videoId")
        for item in search_response.get("items", [])
        if item.get("id", {}).get("videoId")
    ]
    logging.info("YouTube API returned %d video ids for %s", len(ids), channel.channel_id)
    if not ids:
        return channel_record, []

    videos_response = (
        youtube.videos()
        .list(part="snippet,contentDetails,statistics", id=",".join(ids))
        .execute()
    )

    videos: list[VideoRecord] = []
    for item in videos_response.get("items", []):
        video_id = item.get("id")
        if not video_id:
            logging.error("Skipping YouTube API video item without id: %s", item)
            continue
        video_snippet = item.get("snippet", {})
        video_statistics = item.get("statistics", {})
        video_content = item.get("contentDetails", {})
        videos.append(
            VideoRecord(
                video_id=str(video_id),
                channel_id=channel.channel_id,
                title=str(video_snippet.get("title") or video_id),
                description=video_snippet.get("description"),
                published_at=video_snippet.get("publishedAt"),
                url=video_url(str(video_id)),
                thumbnail_url=best_thumbnail(video_snippet.get("thumbnails")),
                duration_seconds=parse_iso8601_duration(video_content.get("duration")),
                view_count=parse_int(video_statistics.get("viewCount")),
                like_count=parse_int(video_statistics.get("likeCount")),
                comment_count=parse_int(video_statistics.get("commentCount")),
                ingestion_source="youtube_api",
            )
        )

    logging.info("YouTube API normalized %d videos for %s", len(videos), channel.channel_id)
    return channel_record, videos


class YtdlpLogger:
    """Bridge yt-dlp logs into application logging."""

    def debug(self, message: str) -> None:
        if message.startswith("[debug]"):
            logging.debug("yt-dlp: %s", message)
        else:
            logging.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logging.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logging.error("yt-dlp: %s", message)


def fetch_with_ytdlp(channel: ChannelConfig, max_videos: int) -> tuple[ChannelRecord, list[VideoRecord]]:
    """Fetch channel and latest videos through yt-dlp."""
    logging.info("Fetching channel %s via yt-dlp fallback", channel.channel_id)
    from yt_dlp import YoutubeDL

    options = {
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "logger": YtdlpLogger(),
        "no_warnings": False,
        "playlistend": max_videos,
        "quiet": True,
        "skip_download": True,
    }

    def extract(url: str) -> Any:
        logging.info("Running yt-dlp extraction for %s", url)
        with YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    info = extract(channel.url)
    fallback_url = uploads_playlist_url(channel.channel_id)
    if (not info or not info.get("entries")) and fallback_url:
        logging.info(
            "yt-dlp returned no playlist entries for %s; retrying with uploads playlist %s",
            channel.url,
            fallback_url,
        )
        fallback_info = extract(fallback_url)
        if fallback_info:
            info = fallback_info

    if not info:
        raise RuntimeError(f"yt-dlp returned no data for {channel.url}")

    channel_id = str(
        info.get("channel_id")
        or info.get("uploader_id")
        or info.get("id")
        or channel.channel_id
    )
    if channel_id != channel.channel_id:
        logging.info(
            "Using configured channel id %s instead of yt-dlp id %s for stable foreign keys",
            channel.channel_id,
            channel_id,
        )

    channel_record = ChannelRecord(
        channel_id=channel.channel_id,
        title=str(info.get("channel") or info.get("uploader") or info.get("title") or channel.name),
        description=info.get("description"),
        custom_url=info.get("channel_url"),
        url=channel.url,
        uploads_playlist_id=None,
        thumbnail_url=best_thumbnail(info.get("thumbnails")),
        subscriber_count=parse_int(info.get("channel_follower_count")),
        video_count=parse_int(info.get("playlist_count")),
    )

    videos: list[VideoRecord] = []
    for entry in info.get("entries") or []:
        if not entry:
            logging.error("Skipping empty yt-dlp entry for channel %s", channel.channel_id)
            continue
        entry_id = entry.get("id") or entry.get("url")
        if not entry_id:
            logging.error("Skipping yt-dlp entry without id/url: %s", entry)
            continue
        video_id_text = str(entry_id).split("watch?v=")[-1].split("&")[0]
        if len(video_id_text) > 32 and entry.get("id"):
            video_id_text = str(entry["id"])

        videos.append(
            VideoRecord(
                video_id=video_id_text,
                channel_id=channel.channel_id,
                title=str(entry.get("title") or video_id_text),
                description=entry.get("description"),
                published_at=normalize_upload_date(entry.get("timestamp") or entry.get("upload_date")),
                url=entry.get("webpage_url") or video_url(video_id_text),
                thumbnail_url=best_thumbnail(entry.get("thumbnails")),
                duration_seconds=parse_int(entry.get("duration")),
                view_count=parse_int(entry.get("view_count")),
                like_count=parse_int(entry.get("like_count")),
                comment_count=parse_int(entry.get("comment_count")),
                ingestion_source="yt_dlp",
            )
        )

    logging.info("yt-dlp normalized %d videos for %s", len(videos), channel.channel_id)
    return channel_record, videos


def upsert_channel(connection: Any, channel: ChannelRecord, ingested_at: str) -> None:
    """Upsert a channel row."""
    logging.info("Upserting channel %s", channel.channel_id)
    connection.execute(
        """
        INSERT INTO channels (
            channel_id,
            title,
            description,
            custom_url,
            url,
            uploads_playlist_id,
            thumbnail_url,
            subscriber_count,
            video_count,
            last_ingested_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            custom_url = excluded.custom_url,
            url = excluded.url,
            uploads_playlist_id = excluded.uploads_playlist_id,
            thumbnail_url = excluded.thumbnail_url,
            subscriber_count = excluded.subscriber_count,
            video_count = excluded.video_count,
            last_ingested_at = excluded.last_ingested_at,
            updated_at = excluded.updated_at
        """,
        (
            channel.channel_id,
            channel.title,
            channel.description,
            channel.custom_url,
            channel.url,
            channel.uploads_playlist_id,
            channel.thumbnail_url,
            channel.subscriber_count,
            channel.video_count,
            ingested_at,
            ingested_at,
        ),
    )


def upsert_video(connection: Any, video: VideoRecord, updated_at: str) -> None:
    """Upsert a video row without resetting downstream statuses."""
    logging.info("Upserting video %s", video.video_id)
    connection.execute(
        """
        INSERT INTO videos (
            video_id,
            channel_id,
            title,
            description,
            published_at,
            url,
            thumbnail_url,
            duration_seconds,
            view_count,
            like_count,
            comment_count,
            ingestion_source,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            channel_id = excluded.channel_id,
            title = excluded.title,
            description = excluded.description,
            published_at = excluded.published_at,
            url = excluded.url,
            thumbnail_url = excluded.thumbnail_url,
            duration_seconds = excluded.duration_seconds,
            view_count = excluded.view_count,
            like_count = excluded.like_count,
            comment_count = excluded.comment_count,
            ingestion_source = excluded.ingestion_source,
            updated_at = excluded.updated_at
        """,
        (
            video.video_id,
            video.channel_id,
            video.title,
            video.description,
            video.published_at,
            video.url,
            video.thumbnail_url,
            video.duration_seconds,
            video.view_count,
            video.like_count,
            video.comment_count,
            video.ingestion_source,
            updated_at,
        ),
    )


def ingest_channels(
    db_path: Path | str = DEFAULT_DB_PATH,
    config_path: Path | str = DEFAULT_CHANNELS_CONFIG,
    max_videos: int = 10,
) -> dict[str, Any]:
    """Run the ingestion job and return run details."""
    logging.info("Starting ingestion job")
    load_dotenv()
    started_at = utc_now()
    initialize_database(db_path)
    channels = load_channel_config(config_path)

    api_key = os.getenv("YOUTUBE_API_KEY")
    youtube = None
    if api_key:
        logging.info("YOUTUBE_API_KEY found; YouTube Data API will be attempted first")
        youtube = build_youtube_service(api_key)
    else:
        logging.info("YOUTUBE_API_KEY missing; using yt-dlp fallback for all channels")

    processed = 0
    succeeded = 0
    failed = 0
    total_videos = 0
    errors: list[str] = []
    source_counts: dict[str, int] = {}
    ingested_at = utc_now()

    with get_connection(db_path) as connection:
        for channel in channels:
            processed += 1
            logging.info("Processing channel %s (%s)", channel.name, channel.channel_id)
            try:
                try:
                    if youtube is None:
                        raise RuntimeError("YouTube API unavailable because YOUTUBE_API_KEY is missing")
                    channel_record, videos = fetch_with_youtube_api(channel, youtube, max_videos)
                except Exception as api_error:
                    logging.error(
                        "YouTube API ingestion failed for %s; falling back to yt-dlp: %s",
                        channel.channel_id,
                        api_error,
                    )
                    channel_record, videos = fetch_with_ytdlp(channel, max_videos)

                upsert_channel(connection, channel_record, ingested_at)
                for video in videos:
                    upsert_video(connection, video, ingested_at)
                    total_videos += 1
                    source_counts[video.ingestion_source] = source_counts.get(video.ingestion_source, 0) + 1
                connection.commit()
                succeeded += 1
                logging.info("Finished channel %s with %d videos", channel.channel_id, len(videos))
            except Exception as exc:
                connection.rollback()
                failed += 1
                message = f"{channel.channel_id}: {exc}"
                errors.append(message)
                logging.exception("Failed to ingest channel %s", channel.channel_id)

    status = "success" if failed == 0 else "partial" if succeeded else "failed"
    finished_at = utc_now()
    details = {
        "channels_config": str(config_path),
        "max_videos": max_videos,
        "source_counts": source_counts,
        "total_videos_seen": total_videos,
    }
    logging.info("Recording ingestion job run with status %s", status)
    insert_job_run(
        job_name="ingest",
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

    result = {
        "status": status,
        "channels_processed": processed,
        "channels_succeeded": succeeded,
        "channels_failed": failed,
        "videos_seen": total_videos,
        "errors": errors,
    }
    logging.info("Ingestion job completed: %s", result)
    return result
