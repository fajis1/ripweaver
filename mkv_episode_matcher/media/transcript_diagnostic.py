"""Explicit, non-mutating Whisper diagnostics for one media sample."""

from __future__ import annotations

import json
import math
import tempfile
import wave
from array import array
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.core.providers.asr import ASRProvider
from mkv_episode_matcher.core.utils import clean_text, extract_audio_chunk


class TranscriptDiagnosticError(RuntimeError):
    """Raised when a transcript diagnostic cannot complete safely."""


@dataclass(frozen=True)
class TranscriptDiagnostic:
    media_id: str
    audio_stream_index: int | None
    start_seconds: float
    duration_seconds: float
    model_name: str
    transcript_characters: int
    transcript_words: int
    mean_dbfs: float | None
    peak_dbfs: float | None
    excerpt: str

    def safe_report(self) -> dict[str, object]:
        """Return durable metrics without dialogue or a source path."""

        report = asdict(self)
        report.pop("excerpt")
        return {
            "mode": "transcript-diagnostic",
            "created_at": datetime.now(UTC).isoformat(),
            **report,
        }


def _dbfs(value: float) -> float | None:
    if value <= 0:
        return None
    return 20 * math.log10(value / 32768.0)


def _wav_levels(path: Path) -> tuple[float | None, float | None]:
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2:
            raise TranscriptDiagnosticError("Diagnostic WAV is not 16-bit PCM")
        samples = array("h", stream.readframes(stream.getnframes()))
    if not samples:
        return None, None
    sum_squares = sum(sample * sample for sample in samples)
    root_mean_square = math.sqrt(sum_squares / len(samples))
    peak = max(abs(sample) for sample in samples)
    return _dbfs(root_mean_square), _dbfs(peak)


def _short_excerpt(text: str, maximum_characters: int = 240) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= maximum_characters:
        return normalized
    shortened = normalized[: maximum_characters + 1].rsplit(" ", 1)[0]
    return f"{shortened}…"


def diagnose_transcript(
    media_path: Path,
    asr: ASRProvider,
    *,
    media_id: str,
    start_seconds: float,
    duration_seconds: float,
    model_name: str,
    audio_stream_index: int | None = None,
) -> TranscriptDiagnostic:
    """Extract one temporary mono sample, transcribe it, then remove the WAV."""

    if not media_path.is_file() or media_path.suffix.lower() != ".mkv":
        raise TranscriptDiagnosticError("Diagnostic input must be an explicit MKV")
    if start_seconds < 0:
        raise TranscriptDiagnosticError("Diagnostic start must not be negative")
    if audio_stream_index is not None and audio_stream_index < 0:
        raise TranscriptDiagnosticError("Audio stream index must not be negative")
    if not 5 <= duration_seconds <= 60:
        raise TranscriptDiagnosticError(
            "Diagnostic duration must be between 5 and 60 seconds"
        )

    try:
        with tempfile.TemporaryDirectory(prefix="mkv-transcript-diagnostic-") as folder:
            chunk = Path(folder) / "sample.wav"
            extract_audio_chunk(
                media_path,
                start_seconds,
                duration_seconds,
                chunk,
                audio_stream_index=audio_stream_index,
            )
            mean_dbfs, peak_dbfs = _wav_levels(chunk)
            transcript = clean_text(asr.transcribe(chunk))
    except TranscriptDiagnosticError:
        raise
    except Exception as exc:
        raise TranscriptDiagnosticError(
            f"Transcript diagnostic failed: {type(exc).__name__}"
        ) from exc

    return TranscriptDiagnostic(
        media_id=media_id,
        audio_stream_index=audio_stream_index,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        model_name=model_name,
        transcript_characters=len(transcript),
        transcript_words=len(transcript.split()),
        mean_dbfs=mean_dbfs,
        peak_dbfs=peak_dbfs,
        excerpt=_short_excerpt(transcript),
    )


def write_safe_report(path: Path, diagnostic: TranscriptDiagnostic) -> Path:
    if path.exists():
        raise TranscriptDiagnosticError(
            "Transcript diagnostic report exists; refusing overwrite"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(diagnostic.safe_report(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
