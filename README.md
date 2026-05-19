# LLM YouTube Landscape Tracker

An automated tracker for LLM-focused YouTube channels. It ingests recent videos, fetches transcripts where available, analyzes transcript text into structured themes, stores everything in SQLite, and publishes both a static dashboard and a FastAPI backend.

## What This Ships

- A Python CLI pipeline for `init-db`, `ingest`, `transcript`, `analyse`, `export-dashboard`, and `serve-api`.
- A SQLite database at `tracker.db` with channels, videos, transcripts, analysis rows, and job run history.
- YouTube ingestion with API-first behavior and a `yt-dlp` fallback.
- Transcript extraction using YouTube captions first, with terminal failure placeholders to prevent retry loops.
- LLM analysis using OpenAI structured output plus a strict Pydantic contract.
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
- OpenAI Python SDK for structured transcript analysis
- Pydantic for output validation
- Vanilla HTML, CSS, and JavaScript for the static dashboard
- GitHub Pages plus GitHub Actions for public hosting and scheduled refresh

## Project Layout

```text
.
├── .github/workflows/tracker.yml  # Scheduled refresh workflow
├── config/channels.json           # Tracked YouTube channel config
├── docs/
│   ├── index.html                 # Static dashboard
│   ├── style.css                  # Dashboard styling
│   ├── app.js                     # Dashboard data loading and rendering
│   └── data/latest.json           # Exported static dashboard snapshot
├── prompts/video_analysis.md      # Strict JSON analysis prompt
├── src/
│   ├── api.py                     # FastAPI app and routes
│   ├── analysis.py                # OpenAI structured analysis
│   ├── database.py                # SQLite schema and helpers
│   ├── export.py                  # Dashboard JSON export
│   ├── ingestion.py               # YouTube API and yt-dlp ingestion
│   └── transcription.py           # Caption fetching and placeholders
├── main.py                        # CLI entry point
├── requirements.txt               # Pinned Python dependencies
├── .env.example                   # Required environment variable template
├── tracker.db                     # Local SQLite database after init/run
└── REPORT.md                      # Submission report
```

## Quick Start

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `python` is not on PATH, use the interpreter available in your environment. In this workspace, commands were verified with:

```powershell
.\.venv\Scripts\python.exe main.py init-db
.\.venv\Scripts\python.exe main.py ingest
.\.venv\Scripts\python.exe main.py transcript
.\.venv\Scripts\python.exe main.py analyse
.\.venv\Scripts\python.exe main.py export-dashboard
```

Optional API keys:

```powershell
Copy-Item .env.example .env
```

Then set `YOUTUBE_API_KEY` and `OPENAI_API_KEY` in `.env`.

Serve the API:

```powershell
.\.venv\Scripts\python.exe main.py serve-api
```

Serve the static dashboard locally:

```powershell
.\.venv\Scripts\python.exe -m http.server 8765 --directory docs
```

## CLI Commands

- `init-db`: Creates the SQLite schema idempotently.
- `ingest`: Reads `config/channels.json`, fetches channel/video metadata, and upserts rows.
- `transcript`: Finds videos missing terminal transcript rows, fetches captions, and stores text or a failed placeholder.
- `analyse`: Finds transcripts needing analysis, calls OpenAI for complete transcripts, and stores schema-valid analysis rows.
- `export-dashboard`: Writes `docs/data/latest.json`.
- `serve-api`: Starts the FastAPI backend on `127.0.0.1:8000` by default.

Useful flags:

```powershell
.\.venv\Scripts\python.exe main.py --db tracker.db init-db
.\.venv\Scripts\python.exe main.py ingest --max-videos 10
.\.venv\Scripts\python.exe main.py transcript --limit 50
.\.venv\Scripts\python.exe main.py analyse --limit 50 --model gpt-4o-mini
.\.venv\Scripts\python.exe main.py export-dashboard --output docs/data/latest.json
.\.venv\Scripts\python.exe main.py serve-api --host 127.0.0.1 --port 8000
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
3. Installs `requirements.txt`.
4. Runs `init-db`.
5. Runs `ingest` with `YOUTUBE_API_KEY` from repository secrets.
6. Runs `transcript` with `OPENAI_API_KEY` available for fallback logic.
7. Runs `analyse` with `OPENAI_API_KEY`.
8. Runs `export-dashboard`.
9. Commits `tracker.db` and `docs/data/latest.json` if they changed.

For GitHub Pages, configure Pages to serve from the `docs/` directory. The dashboard will load `./data/latest.json` when the API is not present.

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

When extraction fails completely, the system writes a terminal failed placeholder into `transcripts` and updates `videos.transcript_status='failed'`. This is intentional: it prevents infinite retry loops and lets the dashboard show a clear pending/failed state.

The OpenAI Whisper fallback is represented as explicit placeholder logic in this build. A production implementation would add audio download, file size controls, rate limits, and privacy/storage handling before calling Whisper.

Analysis only performs transcript-grounded OpenAI analysis when `transcripts.status='complete'`. Failed transcript placeholders are stored as schema-valid `Unavailable` analysis rows, so export and dashboard rendering stay reliable even when captions are unavailable.
