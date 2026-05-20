# LLM YouTube Landscape Tracker

An automated tracker for LLM-focused YouTube channels. It ingests recent videos, fetches transcripts where available, analyzes transcript text into structured themes, stores everything in SQLite, and publishes both a static dashboard and a FastAPI backend.

## What This this

- A Python CLI pipeline for `init-db`, `ingest`, `transcript`, `analyse`, `export-dashboard`, and `serve-api`.
- A SQLite database at `tracker.db` with channels, videos, transcripts, analysis rows, and job run history.
- YouTube ingestion with API-first behavior and a `yt-dlp` fallback.
- Transcript extraction using YouTube captions first, with an OpenAI Whisper audio fallback and terminal failure records to prevent retry loops.
- LLM analysis using OpenAI or Gemini structured output plus a strict Pydantic contract.
- A static dashboard in `docs/` that works on GitHub Pages.
- A FastAPI backend with dashboard data endpoints.
- A GitHub Actions workflow that refreshes data every 6 hours.
- A submission report in `REPORT.md`.

## Recommended Stack In This Repo

- Python 3.13
- SQLite for local durable storage
- FastAPI and Uvicorn for the API backend
- YouTube Data API v3 via `google-api-python-client`
- `yt-dlp` as ingestion fallback
- `youtube-transcript-api` for captions
- OpenAI Python SDK for Whisper transcription fallback and OpenAI transcript analysis
- Google Gen AI SDK for Gemini transcript analysis
- Pydantic for output validation
- `ffmpeg` installed on the host OS for Whisper audio chunking through `pydub` (`apt`, `brew`, or the official Windows builds)
- Vanilla HTML, CSS, and JavaScript for the static dashboard
- GitHub Pages plus GitHub Actions for public hosting and scheduled refresh

## Project Layout

```text
.
|-- .github/workflows/tracker.yml  # Scheduled refresh workflow
|-- config/channels.json           # Tracked YouTube channel config
|-- docs/
|   |-- index.html                 # Static dashboard
|   |-- style.css                  # Dashboard styling
|   |-- app.js                     # Dashboard data loading and rendering
|   `-- data/latest.json           # Exported static dashboard snapshot
|-- prompts/video_analysis.md      # Strict JSON analysis prompt
|-- src/
|   |-- api.py                     # FastAPI app and routes
|   |-- analysis.py                # OpenAI/Gemini structured analysis
|   |-- database.py                # SQLite schema and helpers
|   |-- export.py                  # Dashboard JSON export
|   |-- ingestion.py               # YouTube API and yt-dlp ingestion
|   `-- transcription.py           # Caption fetching and Whisper fallback
|-- main.py                        # CLI entry point
|-- requirements.txt               # Pinned Python dependencies
|-- .env.example                   # Required environment variable template
|-- tracker.db                     # Local SQLite database after init/run
`-- REPORT.md                      # Submission report
```

## Quick Start

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Unix/Mac:

```bash
source .venv/bin/activate
```

Activate it on Windows:

```powershell
.venv\Scripts\activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Install `ffmpeg` on the host OS before using the Whisper fallback. `pydub` requires `ffmpeg` to chunk downloaded audio; use `apt`, `brew`, or download an official Windows build.

If `python` is not on PATH, use the interpreter available in your environment. In this workspace, commands were verified with:

```bash
python main.py init-db
python main.py ingest
python main.py transcript
python main.py analyse
python main.py export-dashboard
```

Optional API keys:

```powershell
Copy-Item .env.example .env
```

Then set `YOUTUBE_API_KEY`, `OPENAI_API_KEY`, and/or `GEMINI_API_KEY` in `.env`.

Serve the API:

```bash
python main.py serve-api
```

Serve the static dashboard locally:

```bash
python -m http.server 8765 --directory docs
```

## CLI Commands

- `init-db`: Creates the SQLite schema idempotently.
- `ingest`: Reads `config/channels.json`, fetches channel/video metadata, and upserts rows.
- `transcript`: Finds videos missing terminal transcript rows, fetches captions, falls back to Whisper, and stores text or a terminal failure record.
- `analyse`: Finds transcripts needing analysis, calls OpenAI or Gemini for complete transcripts, and stores schema-valid analysis rows.
- `export-dashboard`: Writes `docs/data/latest.json`.
- `serve-api`: Starts the FastAPI backend on `127.0.0.1:8000` by default.

Useful flags:

```bash
python main.py --db tracker.db init-db
python main.py ingest --max-videos 10
python main.py transcript --limit 50
python main.py analyse --limit 50 --model gpt-4o-mini
python main.py analyse --limit 50 --provider gemini --model gemini-2.5-flash
python main.py export-dashboard --output docs/data/latest.json
python main.py serve-api --host 127.0.0.1 --port 8000
```

## Data Model

The database has exactly five application tables:

- `channels`: YouTube channel metadata, ingest timestamps, subscriber/video counts when available.
- `videos`: Video metadata keyed by unique `video_id`, including transcript and analysis pipeline statuses.
- `transcripts`: One row per video transcript result, including source, language, text, status, and error details.
- `analysis`: One row per video analysis result, including summary, speakers, topics, keywords, themes, confidence, model, prompt version, raw JSON, and status.
- `job_runs`: History for pipeline jobs, processed counts, success/failure counts, timestamps, errors, and run details.

Important uniqueness rules:

- `channels.channel_id` is unique.
- `videos.video_id` is unique.
- `transcripts.video_id` is unique.
- `analysis.video_id` is unique.

These constraints make repeated ingestion, transcript, analysis, and export runs safe.

## Automation Strategy

`.github/workflows/tracker.yml` runs on:

- Cron: `0 */6 * * *`
- Manual dispatch: `workflow_dispatch`

The workflow:

1. Checks out the repo.
2. Sets up Python 3.13.
3. Installs `ffmpeg` and `requirements.txt`.
4. Runs `init-db`.
5. Runs `ingest` with `YOUTUBE_API_KEY` from repository secrets.
6. Runs `transcript` with `OPENAI_API_KEY` available for fallback logic.
7. Runs `analyse` with `OPENAI_API_KEY` or `GEMINI_API_KEY`.
8. Runs `export-dashboard`.
9. Commits `tracker.db` and `docs/data/latest.json` if they changed.

For GitHub Pages, configure Pages to serve from the `docs/` directory. The dashboard will load `./data/latest.json` when the API is not present.

## Notes On Analysis Providers

`analyse` supports `--provider auto`, `--provider openai`, and `--provider gemini`. In `auto` mode, OpenAI is used when `OPENAI_API_KEY` exists, otherwise Gemini is used when `GEMINI_API_KEY` exists. If both keys are present and you want Gemini, pass `--provider gemini` or set `ANALYSIS_PROVIDER=gemini`.

Default models are `gpt-4o-mini` for OpenAI and `gemini-2.5-flash` for Gemini. Override with `--model`, `ANALYSIS_MODEL`, `OPENAI_ANALYSIS_MODEL`, or `GEMINI_ANALYSIS_MODEL`.

## Notes On API-First Ingestion

Ingestion prefers the YouTube Data API when `YOUTUBE_API_KEY` is configured. This gives cleaner metadata and avoids some scraping fragility.

If the API key is missing or the API request fails for a channel, ingestion falls back to `yt-dlp`. The fallback first tries the configured channel `/videos` URL, then retries the derived uploads playlist URL because YouTube channel pages can return empty playlist data in some environments.

Failed channels do not stop the whole run. Each channel is handled independently, and the final result is recorded in `job_runs`.

## Notes On Transcript Reliability

Transcript fetching starts with YouTube captions through `youtube-transcript-api`. This can fail for normal operational reasons:

- Captions are disabled.
- Captions are unavailable in preferred languages.
- YouTube rate-limits requests with HTTP 429.
- YouTube blocks unauthenticated/bot-like requests.
- Network access is unavailable in the runtime environment.

When caption extraction fails, the system attempts an OpenAI Whisper fallback if `OPENAI_API_KEY` is configured. It downloads lowest-quality audio with `yt-dlp` into a temporary directory, submits the file to `whisper-1`, and stores successful output with `source='openai_whisper'`.

When extraction fails completely, or when `OPENAI_API_KEY` is missing, the system writes a terminal failed record into `transcripts` and updates `videos.transcript_status='failed'`. This is intentional: it prevents infinite retry loops and lets the dashboard show a clear pending/failed state.

Analysis only performs transcript-grounded LLM analysis when `transcripts.status='complete'`. Failed transcript placeholders are stored as schema-valid `Unavailable` analysis rows, so export and dashboard rendering stay reliable even when captions are unavailable.
