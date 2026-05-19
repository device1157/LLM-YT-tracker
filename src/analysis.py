"""LLM analysis of fetched transcripts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field, ValidationError

from src.database import DEFAULT_DB_PATH, get_connection, initialize_database, insert_job_run

DEFAULT_PROMPT_PATH = Path("prompts/video_analysis.md")
DEFAULT_OPENAI_ANALYSIS_MODEL = "gpt-4o-mini"
DEFAULT_GEMINI_ANALYSIS_MODEL = "gemini-2.5-flash"
DEFAULT_ANALYSIS_MODEL = DEFAULT_OPENAI_ANALYSIS_MODEL
DEFAULT_ANALYSIS_PROVIDER = "auto"
SUPPORTED_ANALYSIS_PROVIDERS = ("auto", "openai", "gemini")
PROMPT_VERSION = "video-analysis-v1"
MAX_TRANSCRIPT_CHARS = 60000

AnalysisProvider = Literal["openai", "gemini"]

Theme = Literal[
    "Tutorial",
    "Model Release",
    "Hardware",
    "Research",
    "Safety",
    "Business",
    "Benchmark",
    "Tooling",
    "Policy",
    "Opinion",
    "Unavailable",
]


class VideoAnalysis(BaseModel):
    """Strict JSON contract for video analysis output."""

    summary: str = Field(min_length=1, max_length=1200)
    speakers: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    themes: list[Theme] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class TranscriptForAnalysis:
    """Joined transcript and video data used for analysis."""

    video_id: str
    title: str
    url: str
    published_at: str | None
    channel_title: str
    transcript_text: str
    transcript_status: str
    transcript_source: str
    transcript_error: str | None


@dataclass(frozen=True)
class AnalysisResult:
    """Normalized analysis result for database persistence."""

    video_id: str
    analysis: VideoAnalysis | None
    status: str
    model: str
    prompt_version: str
    error_message: str | None


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_prompt(prompt_path: Path | str = DEFAULT_PROMPT_PATH) -> str:
    """Load the analysis prompt from disk."""
    path = Path(prompt_path)
    logging.info("Loading analysis prompt from %s", path)
    prompt = path.read_text(encoding="utf-8")
    if "Prompt-Version:" not in prompt:
        logging.error("Prompt file is missing a Prompt-Version header")
        raise ValueError("Analysis prompt must include a Prompt-Version header")
    logging.info("Loaded analysis prompt with %d characters", len(prompt))
    return prompt


def get_unanalyzed_transcripts(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 10,
) -> list[TranscriptForAnalysis]:
    """Return transcript rows that need analysis or terminal analysis status."""
    logging.info("Querying up to %d transcripts pending analysis", limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                v.video_id,
                v.title,
                v.url,
                v.published_at,
                c.title AS channel_title,
                t.text AS transcript_text,
                t.status AS transcript_status,
                t.source AS transcript_source,
                t.error_message AS transcript_error
            FROM transcripts t
            JOIN videos v ON v.video_id = t.video_id
            JOIN channels c ON c.channel_id = v.channel_id
            LEFT JOIN analysis a ON a.video_id = t.video_id
            WHERE a.video_id IS NULL
               OR a.status IN ('pending', 'retryable_error')
            ORDER BY
                CASE WHEN t.status = 'complete' THEN 0 ELSE 1 END,
                v.published_at DESC,
                v.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    transcripts = [
        TranscriptForAnalysis(
            video_id=row["video_id"],
            title=row["title"],
            url=row["url"],
            published_at=row["published_at"],
            channel_title=row["channel_title"],
            transcript_text=row["transcript_text"],
            transcript_status=row["transcript_status"],
            transcript_source=row["transcript_source"],
            transcript_error=row["transcript_error"],
        )
        for row in rows
    ]
    logging.info("Found %d transcripts pending analysis", len(transcripts))
    return transcripts


def unavailable_analysis(message: str) -> VideoAnalysis:
    """Build a schema-valid placeholder for unavailable transcript content."""
    logging.info("Building unavailable analysis placeholder")
    return VideoAnalysis(
        summary=message,
        speakers=[],
        topics=[],
        keywords=[],
        themes=["Unavailable"],
        confidence=0.0,
    )


def build_user_payload(item: TranscriptForAnalysis) -> str:
    """Build the user message sent to the LLM."""
    logging.info("Building analysis payload for video %s", item.video_id)
    transcript = item.transcript_text.strip()
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        logging.info(
            "Truncating transcript for %s from %d to %d characters",
            item.video_id,
            len(transcript),
            MAX_TRANSCRIPT_CHARS,
        )
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]

    payload = {
        "video_id": item.video_id,
        "title": item.title,
        "channel": item.channel_title,
        "published_at": item.published_at,
        "url": item.url,
        "transcript_source": item.transcript_source,
        "transcript": transcript,
    }
    return json.dumps(payload, ensure_ascii=False)


def infer_provider_from_model(model: str | None) -> AnalysisProvider | None:
    """Infer provider from well-known model name prefixes."""
    if not model:
        return None
    model_name = model.strip().lower()
    if model_name.startswith("gemini-"):
        return "gemini"
    if model_name.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return None


def select_analysis_provider(provider: str | None, model: str | None = None) -> AnalysisProvider:
    """Choose the analysis provider from CLI/env configuration and available keys."""
    requested = (provider or os.getenv("ANALYSIS_PROVIDER") or DEFAULT_ANALYSIS_PROVIDER).strip().lower()
    if requested not in SUPPORTED_ANALYSIS_PROVIDERS:
        supported = ", ".join(SUPPORTED_ANALYSIS_PROVIDERS)
        raise ValueError(f"Unsupported analysis provider '{requested}'. Expected one of: {supported}")

    if requested != "auto":
        return requested  # type: ignore[return-value]

    inferred = infer_provider_from_model(model or os.getenv("ANALYSIS_MODEL"))
    if inferred:
        return inferred
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return "openai"


def select_analysis_model(provider: AnalysisProvider, model: str | None = None) -> str:
    """Choose the model for the selected provider."""
    if model:
        return model

    generic_model = os.getenv("ANALYSIS_MODEL")
    if generic_model:
        return generic_model

    if provider == "gemini":
        return os.getenv("GEMINI_ANALYSIS_MODEL") or DEFAULT_GEMINI_ANALYSIS_MODEL
    return os.getenv("OPENAI_ANALYSIS_MODEL") or DEFAULT_OPENAI_ANALYSIS_MODEL


def provider_api_key_name(provider: AnalysisProvider) -> str:
    """Return the environment variable required by a provider."""
    if provider == "gemini":
        return "GEMINI_API_KEY"
    return "OPENAI_API_KEY"


def analyze_with_openai(
    item: TranscriptForAnalysis,
    prompt: str,
    model: str,
    timeout_seconds: float,
) -> VideoAnalysis:
    """Run OpenAI structured analysis and return a validated Pydantic object."""
    logging.info("Calling OpenAI structured analysis for video %s with model %s", item.video_id, model)
    client = OpenAI(timeout=timeout_seconds)
    completion = client.beta.chat.completions.parse(
        model=model,
        temperature=0,
        response_format=VideoAnalysis,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": build_user_payload(item)},
        ],
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        logging.error("OpenAI returned no parsed analysis for %s", item.video_id)
        raise RuntimeError("OpenAI returned no parsed analysis")
    logging.info("OpenAI analysis parsed successfully for video %s", item.video_id)
    return parsed


def analyze_with_gemini(
    item: TranscriptForAnalysis,
    prompt: str,
    model: str,
    timeout_seconds: float,
) -> VideoAnalysis:
    """Run Gemini structured analysis and return a validated Pydantic object."""
    logging.info("Calling Gemini structured analysis for video %s with model %s", item.video_id, model)
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google-genai is not installed. Run `pip install -r requirements.txt`.") from exc

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    timeout_ms = int(timeout_seconds * 1000)
    config = types.GenerateContentConfig(
        system_instruction=prompt,
        temperature=0,
        response_mime_type="application/json",
        response_json_schema=VideoAnalysis.model_json_schema(),
    )

    with genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=timeout_ms),
    ) as client:
        response = client.models.generate_content(
            model=model,
            contents=build_user_payload(item),
            config=config,
        )

    parsed_payload = getattr(response, "parsed", None)
    if isinstance(parsed_payload, VideoAnalysis):
        logging.info("Gemini analysis parsed successfully for video %s", item.video_id)
        return parsed_payload
    if parsed_payload is not None:
        parsed = VideoAnalysis.model_validate(parsed_payload)
        logging.info("Gemini analysis parsed successfully for video %s", item.video_id)
        return parsed

    if not response.text:
        logging.error("Gemini returned no text analysis for %s", item.video_id)
        raise RuntimeError("Gemini returned no text analysis")

    parsed = VideoAnalysis.model_validate_json(response.text)
    logging.info("Gemini analysis parsed successfully for video %s", item.video_id)
    return parsed


def analyze_transcript(
    item: TranscriptForAnalysis,
    prompt: str,
    provider: AnalysisProvider,
    model: str,
    timeout_seconds: float,
) -> AnalysisResult:
    """Analyze one transcript and normalize terminal or retryable outcomes."""
    logging.info("Starting analysis for video %s", item.video_id)
    if item.transcript_status != "complete":
        message = "Transcript unavailable; no transcript-grounded analysis was performed."
        if item.transcript_error:
            message = f"{message} Transcript error: {item.transcript_error[:500]}"
        return AnalysisResult(
            video_id=item.video_id,
            analysis=unavailable_analysis(message),
            status="transcript_failed",
            model=model,
            prompt_version=PROMPT_VERSION,
            error_message=item.transcript_error,
        )

    api_key_name = provider_api_key_name(provider)
    if not os.getenv(api_key_name):
        logging.error("%s is missing; analysis for %s remains retryable", api_key_name, item.video_id)
        return AnalysisResult(
            video_id=item.video_id,
            analysis=unavailable_analysis(f"{provider.title()} analysis was not run because {api_key_name} is missing."),
            status="retryable_error",
            model=model,
            prompt_version=PROMPT_VERSION,
            error_message=f"{api_key_name} is missing.",
        )

    try:
        if provider == "gemini":
            parsed = analyze_with_gemini(item, prompt, model, timeout_seconds)
        else:
            parsed = analyze_with_openai(item, prompt, model, timeout_seconds)
        return AnalysisResult(
            video_id=item.video_id,
            analysis=parsed,
            status="complete",
            model=model,
            prompt_version=PROMPT_VERSION,
            error_message=None,
        )
    except (APITimeoutError, TimeoutError) as exc:
        logging.exception("%s analysis timed out for %s", provider.title(), item.video_id)
        return retryable_error(item.video_id, model, f"{provider.title()} timeout: {exc}")
    except (APIConnectionError, APIStatusError) as exc:
        logging.exception("%s API error for %s", provider.title(), item.video_id)
        return retryable_error(item.video_id, model, f"{provider.title()} API error: {exc}")
    except (ValidationError, ValueError, RuntimeError) as exc:
        logging.exception("%s analysis parsing failed for %s", provider.title(), item.video_id)
        return retryable_error(item.video_id, model, f"Analysis parsing failed: {exc}")
    except Exception as exc:
        logging.exception("%s analysis failed for %s", provider.title(), item.video_id)
        return retryable_error(item.video_id, model, f"{provider.title()} analysis failed: {exc}")


def retryable_error(video_id: str, model: str, error_message: str) -> AnalysisResult:
    """Create a retryable analysis error result."""
    logging.info("Building retryable analysis error for %s", video_id)
    return AnalysisResult(
        video_id=video_id,
        analysis=unavailable_analysis("Analysis failed and should be retried."),
        status="retryable_error",
        model=model,
        prompt_version=PROMPT_VERSION,
        error_message=error_message,
    )


def save_analysis(connection: Any, result: AnalysisResult, analyzed_at: str) -> None:
    """Upsert an analysis row and update the video analysis status."""
    logging.info("Saving analysis result for %s with status %s", result.video_id, result.status)
    analysis = result.analysis
    raw_json = analysis.model_dump_json() if analysis else None
    connection.execute(
        """
        INSERT INTO analysis (
            video_id,
            summary,
            speakers_json,
            topics_json,
            keywords_json,
            themes_json,
            confidence,
            model,
            prompt_version,
            raw_json,
            status,
            error_message,
            analyzed_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            summary = excluded.summary,
            speakers_json = excluded.speakers_json,
            topics_json = excluded.topics_json,
            keywords_json = excluded.keywords_json,
            themes_json = excluded.themes_json,
            confidence = excluded.confidence,
            model = excluded.model,
            prompt_version = excluded.prompt_version,
            raw_json = excluded.raw_json,
            status = excluded.status,
            error_message = excluded.error_message,
            analyzed_at = excluded.analyzed_at,
            updated_at = excluded.updated_at
        """,
        (
            result.video_id,
            analysis.summary if analysis else None,
            json.dumps(analysis.speakers if analysis else []),
            json.dumps(analysis.topics if analysis else []),
            json.dumps(analysis.keywords if analysis else []),
            json.dumps(analysis.themes if analysis else []),
            analysis.confidence if analysis else None,
            result.model,
            result.prompt_version,
            raw_json,
            result.status,
            result.error_message,
            analyzed_at,
            analyzed_at,
        ),
    )
    connection.execute(
        """
        UPDATE videos
        SET analysis_status = ?,
            updated_at = ?
        WHERE video_id = ?
        """,
        (result.status, analyzed_at, result.video_id),
    )


def analyze_pending_transcripts(
    db_path: Path | str = DEFAULT_DB_PATH,
    prompt_path: Path | str = DEFAULT_PROMPT_PATH,
    limit: int = 10,
    provider: str | None = None,
    model: str | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Run the LLM analysis job."""
    logging.info("Starting analysis job")
    load_dotenv()
    started_at = utc_now()
    initialize_database(db_path)
    prompt = load_prompt(prompt_path)
    selected_provider = select_analysis_provider(provider, model)
    selected_model = select_analysis_model(selected_provider, model)
    logging.info("Selected %s analysis provider with model %s", selected_provider, selected_model)
    transcripts = get_unanalyzed_transcripts(db_path=db_path, limit=limit)

    processed = 0
    succeeded = 0
    retryable = 0
    skipped = 0
    errors: list[str] = []
    status_counts: dict[str, int] = {}

    with get_connection(db_path) as connection:
        for transcript in transcripts:
            processed += 1
            result = analyze_transcript(
                transcript,
                prompt=prompt,
                provider=selected_provider,
                model=selected_model,
                timeout_seconds=timeout_seconds,
            )
            save_analysis(connection, result, utc_now())
            connection.commit()

            status_counts[result.status] = status_counts.get(result.status, 0) + 1
            if result.status == "complete":
                succeeded += 1
            elif result.status == "retryable_error":
                retryable += 1
                errors.append(f"{result.video_id}: {result.error_message}")
            elif result.status == "transcript_failed":
                skipped += 1
            logging.info("Finished analysis handling for %s", transcript.video_id)

    status = "success" if retryable == 0 else "partial" if succeeded or skipped else "failed"
    finished_at = utc_now()
    details = {
        "limit": limit,
        "provider": selected_provider,
        "model": selected_model,
        "prompt_version": PROMPT_VERSION,
        "status_counts": status_counts,
    }
    logging.info("Recording analysis job run with status %s", status)
    insert_job_run(
        job_name="analyse",
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        items_processed=processed,
        items_succeeded=succeeded,
        items_failed=retryable,
        error_message="\n".join(errors) if errors else None,
        details=details,
        db_path=db_path,
    )

    result_payload = {
        "status": status,
        "videos_processed": processed,
        "videos_succeeded": succeeded,
        "videos_retryable": retryable,
        "videos_skipped": skipped,
        "errors": errors,
    }
    logging.info("Analysis job completed: %s", result_payload)
    return result_payload
