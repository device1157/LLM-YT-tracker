# System Instruction: Final Project Polish

You are an expert Python developer finalizing the "LLM YouTube Landscape Tracker". The current codebase is functionally excellent, but it requires three critical improvements before it is ready for final submission. 

Please execute the following 3 tasks step-by-step. Do not move to the next task until the current one is completed.

## Task 1: Implement the Actual OpenAI Whisper Fallback
Currently, `src/transcription.py` uses a mock placeholder function (`fetch_whisper_placeholder`) when YouTube captions fail. The project requirements state we must actually download the audio and use the OpenAI Whisper API as a fallback.

**Action 1:** Open `src/transcription.py`.
**Action 2:** Delete the `fetch_whisper_placeholder` function completely.
**Action 3:** Replace it with the following `fetch_whisper_fallback` function:

```python
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