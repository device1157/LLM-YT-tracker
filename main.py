"""Command-line interface for the LLM YouTube Landscape Tracker."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.analysis import (
    DEFAULT_GEMINI_ANALYSIS_MODEL,
    DEFAULT_OPENAI_ANALYSIS_MODEL,
    DEFAULT_PROMPT_PATH,
    SUPPORTED_ANALYSIS_PROVIDERS,
    analyze_pending_transcripts,
)
from src.api import run_server
from src.database import DEFAULT_DB_PATH, EXPECTED_TABLES, initialize_database, list_tables
from src.export import DEFAULT_SNAPSHOT_PATH, export_dashboard_snapshot
from src.ingestion import DEFAULT_CHANNELS_CONFIG, ingest_channels
from src.transcription import DEFAULT_LANGUAGES, transcribe_missing_videos


def configure_logging() -> None:
    """Configure consistent CLI logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("Logging configured")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    logging.info("Building CLI parser")
    parser = argparse.ArgumentParser(
        description="LLM YouTube Landscape Tracker command-line tools.",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to the SQLite database file. Defaults to tracker.db.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Initialize the SQLite database schema.")

    ingest_parser = subparsers.add_parser("ingest", help="Fetch and upsert channel/video metadata.")
    ingest_parser.add_argument(
        "--channels-config",
        default=str(DEFAULT_CHANNELS_CONFIG),
        help="Path to the channel configuration JSON file.",
    )
    ingest_parser.add_argument(
        "--max-videos",
        type=int,
        default=10,
        help="Maximum recent videos to fetch per channel.",
    )

    transcript_parser = subparsers.add_parser("transcript", help="Fetch missing video transcripts.")
    transcript_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum videos to transcribe in one run.",
    )
    transcript_parser.add_argument(
        "--languages",
        default=",".join(DEFAULT_LANGUAGES),
        help="Comma-separated transcript language preferences.",
    )

    analyse_parser = subparsers.add_parser("analyse", help="Analyze transcripts with an LLM.")
    analyse_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum transcripts to analyze in one run.",
    )
    analyse_parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model to use for transcript analysis. Defaults to "
            f"{DEFAULT_OPENAI_ANALYSIS_MODEL} for OpenAI or {DEFAULT_GEMINI_ANALYSIS_MODEL} for Gemini."
        ),
    )
    analyse_parser.add_argument(
        "--provider",
        choices=SUPPORTED_ANALYSIS_PROVIDERS,
        default=None,
        help="LLM provider for transcript analysis. Defaults to ANALYSIS_PROVIDER or auto.",
    )
    analyse_parser.add_argument(
        "--prompt",
        default=str(DEFAULT_PROMPT_PATH),
        help="Path to the video analysis prompt.",
    )
    analyse_parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="LLM request timeout in seconds.",
    )

    export_parser = subparsers.add_parser(
        "export-dashboard",
        help="Export the current dashboard snapshot to docs/data/latest.json.",
    )
    export_parser.add_argument(
        "--output",
        default=str(DEFAULT_SNAPSHOT_PATH),
        help="Path to write the dashboard JSON snapshot.",
    )

    serve_parser = subparsers.add_parser("serve-api", help="Run the FastAPI backend.")
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface for the API server.",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the API server.",
    )
    return parser


def handle_init_db(db_path: Path) -> int:
    """Run the init-db command."""
    logging.info("Handling init-db command for %s", db_path)
    initialize_database(db_path)

    tables = list_tables(db_path)
    expected = set(EXPECTED_TABLES)
    actual = set(tables)
    if actual != expected:
        logging.error("Schema validation failed. Expected %s but found %s", expected, actual)
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(f"Schema mismatch. Missing={missing}; unexpected={unexpected}")

    logging.info("Schema validation passed with exactly %d tables", len(tables))
    print(f"Initialized {db_path} with tables: {', '.join(tables)}")
    return 0


def handle_ingest(db_path: Path, channels_config: Path, max_videos: int) -> int:
    """Run the ingest command."""
    logging.info(
        "Handling ingest command for db=%s channels_config=%s max_videos=%d",
        db_path,
        channels_config,
        max_videos,
    )
    if max_videos < 1:
        logging.error("Invalid max_videos value: %d", max_videos)
        raise ValueError("--max-videos must be at least 1")

    result = ingest_channels(
        db_path=db_path,
        config_path=channels_config,
        max_videos=max_videos,
    )
    print(
        "Ingest complete: "
        f"status={result['status']} "
        f"channels={result['channels_succeeded']}/{result['channels_processed']} "
        f"videos_seen={result['videos_seen']}"
    )
    if result["status"] == "failed":
        logging.error("Ingestion failed for all channels")
        return 1
    return 0


def handle_transcript(db_path: Path, limit: int, languages_text: str) -> int:
    """Run the transcript command."""
    logging.info(
        "Handling transcript command for db=%s limit=%d languages=%s",
        db_path,
        limit,
        languages_text,
    )
    if limit < 1:
        logging.error("Invalid transcript limit value: %d", limit)
        raise ValueError("--limit must be at least 1")

    languages = tuple(language.strip() for language in languages_text.split(",") if language.strip())
    if not languages:
        logging.error("No transcript languages were provided")
        raise ValueError("--languages must include at least one language code")

    result = transcribe_missing_videos(
        db_path=db_path,
        limit=limit,
        languages=languages,
    )
    print(
        "Transcript complete: "
        f"status={result['status']} "
        f"videos={result['videos_succeeded']}/{result['videos_processed']} "
        f"failed={result['videos_failed']}"
    )
    return 0


def handle_analyse(
    db_path: Path,
    limit: int,
    provider: str | None,
    model: str | None,
    prompt_path: Path,
    timeout: float,
) -> int:
    """Run the analyse command."""
    logging.info(
        "Handling analyse command for db=%s limit=%d provider=%s model=%s prompt=%s timeout=%s",
        db_path,
        limit,
        provider,
        model,
        prompt_path,
        timeout,
    )
    if limit < 1:
        logging.error("Invalid analyse limit value: %d", limit)
        raise ValueError("--limit must be at least 1")
    if timeout <= 0:
        logging.error("Invalid analyse timeout value: %s", timeout)
        raise ValueError("--timeout must be greater than 0")

    result = analyze_pending_transcripts(
        db_path=db_path,
        prompt_path=prompt_path,
        limit=limit,
        provider=provider,
        model=model,
        timeout_seconds=timeout,
    )
    print(
        "Analyse complete: "
        f"status={result['status']} "
        f"videos={result['videos_succeeded']}/{result['videos_processed']} "
        f"retryable={result['videos_retryable']} "
        f"skipped={result['videos_skipped']}"
    )
    return 0


def handle_export_dashboard(db_path: Path, output_path: Path) -> int:
    """Run the export-dashboard command."""
    logging.info("Handling export-dashboard command for db=%s output=%s", db_path, output_path)
    payload = export_dashboard_snapshot(db_path=db_path, output_path=output_path)
    print(
        "Export complete: "
        f"generated_at={payload['generated_at']} "
        f"channels={len(payload['channels'])} "
        f"videos={len(payload['videos'])} "
        f"output={output_path}"
    )
    return 0


def handle_serve_api(db_path: Path, host: str, port: int) -> int:
    """Run the serve-api command."""
    logging.info("Handling serve-api command for db=%s host=%s port=%d", db_path, host, port)
    if port < 1 or port > 65535:
        logging.error("Invalid port value: %d", port)
        raise ValueError("--port must be between 1 and 65535")
    run_server(db_path=db_path, snapshot_path=DEFAULT_SNAPSHOT_PATH, host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db)

    logging.info("Dispatching command: %s", args.command)
    try:
        if args.command == "init-db":
            return handle_init_db(db_path)
        if args.command == "ingest":
            return handle_ingest(db_path, Path(args.channels_config), args.max_videos)
        if args.command == "transcript":
            return handle_transcript(db_path, args.limit, args.languages)
        if args.command == "analyse":
            return handle_analyse(db_path, args.limit, args.provider, args.model, Path(args.prompt), args.timeout)
        if args.command == "export-dashboard":
            return handle_export_dashboard(db_path, Path(args.output))
        if args.command == "serve-api":
            return handle_serve_api(db_path, args.host, args.port)

        logging.error("Unsupported command: %s", args.command)
        parser.error(f"Unsupported command: {args.command}")
        return 2
    except Exception as exc:
        logging.exception("Command failed: %s", args.command)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
