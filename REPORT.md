# LLM YouTube Landscape Tracker Report

## Problem Statement

The goal is to keep a browser-accessible table of popular LLM-focused YouTube channels up to date. Each row should reflect transcript-grounded evidence about what a creator says, not only metadata such as titles or thumbnails. The tracker must ingest recent videos, collect transcripts or explicit failure placeholders, analyze available transcript text into structured themes, and publish both an API and a static dashboard snapshot.

## Methodology

The system is a Python 3.13 CLI pipeline backed by SQLite. The pipeline order is `init-db`, `ingest`, `transcript`, `analyse`, and `export-dashboard`.

Ingestion reads `config/channels.json`, uses the YouTube Data API when `YOUTUBE_API_KEY` is available, and falls back to `yt-dlp`. The fallback retries derived uploads playlist URLs because channel `/videos` pages can return empty lists in some environments. Channel and video records are upserted with unique IDs to avoid duplicates.

Transcript extraction uses `youtube-transcript-api` first. If captions cannot be retrieved, the system records a terminal failed placeholder in the `transcripts` table and updates `videos.transcript_status`, preventing infinite retries. The OpenAI Whisper fallback is represented as explicit placeholder logic because this build does not download audio.

Analysis uses `prompts/video_analysis.md` and a Pydantic `VideoAnalysis` model to enforce strict JSON fields: `summary`, `speakers`, `topics`, `keywords`, `themes`, and `confidence`. Complete transcripts are sent through OpenAI structured parsing. Failed transcript placeholders are recorded as schema-valid `Unavailable` analyses with `analysis_status='transcript_failed'`.

The dashboard export joins channels, videos, transcripts, and analysis rows into `docs/data/latest.json` with `generated_at`, `stats`, `channels`, and `videos`. FastAPI exposes `/health`, `/channels`, `/stats`, `/videos`, `/dashboard-data`, and `/refresh`. The static dashboard loads `/dashboard-data` first and falls back to `./data/latest.json` for GitHub Pages.

Automation is handled by `.github/workflows/tracker.yml`, scheduled every six hours with manual dispatch support. It installs dependencies, runs the full CLI pipeline, exports the dashboard snapshot, and commits `tracker.db` plus `docs/data/latest.json` when they change.

## Evaluation Dataset

The local dataset uses two configured LLM-focused channels:

- Andrej Karpathy (`UCXUPKJO5MZQN11PqgIvyuvQ`)
- AI Explained (`UCNJ1Ymd5yFuUPtn21xtRbbw`)

The Phase 3 local ingestion run populated 20 videos total, 10 per channel. The database contains the five required tables: `channels`, `videos`, `transcripts`, `analysis`, and `job_runs`.

## Evaluation Methods

The build was evaluated phase by phase with direct CLI commands and SQLite checks.

- Database initialization was verified by creating `tracker.db` and confirming exactly five user tables.
- Ingestion was run twice and checked for idempotency by comparing total rows and distinct IDs.
- Transcription was verified by ensuring each processed video receives a terminal transcript row and `videos.transcript_status` update.
- Analysis was verified by loading stored `raw_json`, checking the required JSON keys, and confirming status updates in `videos.analysis_status`.
- Export was verified by generating `docs/data/latest.json` and checking top-level keys, channel count, and video count.
- API verification started `serve-api` locally and confirmed `GET /health` returned `status=ok`, 2 channels, and 20 videos.
- Frontend verification served `docs/` locally and used headless Chrome to confirm the static fallback rendered the dashboard, `20 videos`, and known video titles.

## Experimental Results

Local verification completed the full runnable pipeline through export and frontend rendering. The current snapshot contains 2 channels and 20 videos.

Transcript retrieval reached YouTube but was rate-limited with HTTP 429 in this environment. The failure behavior worked as designed: all 20 videos received failed transcript placeholders, preventing infinite retries. Analysis then created 20 schema-valid `Unavailable` records with `confidence=0.0` and `analysis_status='transcript_failed'`.

Because the local environment did not provide usable captions or an `OPENAI_API_KEY`, no transcript-grounded OpenAI summaries were generated locally. The structured OpenAI path is implemented and will run automatically for future rows whose transcript status is `complete` when `OPENAI_API_KEY` is configured.
