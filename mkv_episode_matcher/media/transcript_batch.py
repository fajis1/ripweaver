"""Confirmation-gated, explicit-file CPU transcript collection.

The collector samples only caller-supplied MKVs using saved FFprobe metadata.
It reuses one ASR provider, processes files sequentially, and keeps paths and
dialogue out of its durable metrics report.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import wave
from array import array
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.core.providers.asr import ASRProvider
from mkv_episode_matcher.core.utils import clean_text
from mkv_episode_matcher.media.audio_diagnostics import (
    SampleWindow,
    build_audio_diagnostic_plan,
)
from mkv_episode_matcher.media.probe import ProbedMedia

_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class TranscriptBatchError(RuntimeError):
    """Raised when batch collection cannot proceed safely."""


class AudioExtractor(Protocol):
    def extract(
        self,
        media_path: Path,
        *,
        start_seconds: float,
        duration_seconds: float,
        audio_stream_index: int,
        output_path: Path,
    ) -> None: ...


@dataclass(frozen=True)
class TranscriptBatchItem:
    file_id: str
    media_path: Path
    media: ProbedMedia


@dataclass(frozen=True)
class CollectedWindow:
    start_seconds: float
    text: str
    word_count: int
    mean_dbfs: float | None
    peak_dbfs: float | None

    def private_dict(self) -> dict[str, object]:
        return {
            "start_seconds": self.start_seconds,
            "text": self.text,
        }

    def safe_dict(self) -> dict[str, object]:
        report = asdict(self)
        report.pop("text")
        return report


@dataclass(frozen=True)
class CollectedFile:
    file_id: str
    duration_seconds: float
    audio_stream_index: int | None
    status: str
    windows: tuple[CollectedWindow, ...]
    attempted_streams: tuple[int, ...]
    failure_code: str | None = None

    def private_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "duration_seconds": self.duration_seconds,
            "audio_stream_index": self.audio_stream_index,
            "status": self.status,
            "windows": [
                window.private_dict() for window in self.windows if window.text
            ],
        }

    def safe_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "duration_seconds": self.duration_seconds,
            "audio_stream_index": self.audio_stream_index,
            "status": self.status,
            "attempted_streams": list(self.attempted_streams),
            "failure_code": self.failure_code,
            "windows": [window.safe_dict() for window in self.windows],
        }


@dataclass(frozen=True)
class TranscriptBatchResult:
    mode: str
    model_name: str
    device: str
    files: tuple[CollectedFile, ...]

    @property
    def succeeded(self) -> bool:
        return all(item.status == "collected" for item in self.files)

    def private_report(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "model_name": self.model_name,
            "device": self.device,
            "files": [item.private_dict() for item in self.files],
        }

    def safe_report(self) -> dict[str, object]:
        return {
            "mode": "transcript-batch-metrics",
            "model_name": self.model_name,
            "device": self.device,
            "file_count": len(self.files),
            "collected_count": sum(item.status == "collected" for item in self.files),
            "files": [item.safe_dict() for item in self.files],
        }


def resolve_ffmpeg_path(explicit_path: Path | None = None) -> Path:
    candidate = explicit_path or load_environment_settings().ffmpeg_path
    if candidate is None:
        discovered = shutil.which("ffmpeg")
        if discovered is None:
            raise TranscriptBatchError(
                "FFmpeg was not found; configure FFMPEG_PATH or pass --ffmpeg-path"
            )
        candidate = Path(discovered)
    candidate = candidate.expanduser()
    if not candidate.is_file():
        raise TranscriptBatchError("Configured FFmpeg executable was not found")
    return candidate.resolve()


def validate_explicit_mkv(path: Path) -> Path:
    if path.suffix.lower() != ".mkv" or not path.is_file():
        raise TranscriptBatchError("Batch inputs must be explicit existing MKV files")
    return path.resolve()


def build_ffmpeg_sample_command(
    executable: Path,
    media_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    audio_stream_index: int,
    output_path: Path,
) -> tuple[str, ...]:
    """Build a fixed read-only-media FFmpeg command with a new WAV target."""

    source = validate_explicit_mkv(media_path)
    if not executable.is_file():
        raise TranscriptBatchError("Configured FFmpeg executable was not found")
    if start_seconds < 0 or not 5 <= duration_seconds <= 60:
        raise TranscriptBatchError("Sample window is outside safe bounds")
    if audio_stream_index < 0:
        raise TranscriptBatchError("Audio stream index must not be negative")
    if output_path.exists() or output_path.suffix.lower() != ".wav":
        raise TranscriptBatchError("Temporary WAV target must be a new .wav file")
    return (
        str(executable.resolve()),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start_seconds),
        "-t",
        str(duration_seconds),
        "-i",
        str(source),
        "-map",
        f"0:{audio_stream_index}",
        "-vn",
        "-sn",
        "-dn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-n",
        str(output_path),
    )


class FFmpegSampleExtractor:
    """Constrained FFmpeg runner whose errors never include source paths."""

    def __init__(self, executable: Path, *, timeout_seconds: int = 90):
        if timeout_seconds <= 0:
            raise ValueError("FFmpeg timeout must be positive")
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def extract(
        self,
        media_path: Path,
        *,
        start_seconds: float,
        duration_seconds: float,
        audio_stream_index: int,
        output_path: Path,
    ) -> None:
        command = build_ffmpeg_sample_command(
            self.executable,
            media_path,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            audio_stream_index=audio_stream_index,
            output_path=output_path,
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TranscriptBatchError("FFmpeg sample extraction timed out") from exc
        except OSError as exc:
            raise TranscriptBatchError(
                f"FFmpeg could not be started: {type(exc).__name__}"
            ) from exc
        if completed.returncode != 0:
            raise TranscriptBatchError(
                f"FFmpeg sample extraction failed with exit code {completed.returncode}"
            )
        if not output_path.is_file() or output_path.stat().st_size < 1024:
            raise TranscriptBatchError("FFmpeg sample output was missing or too small")


def _dbfs(value: float) -> float | None:
    return 20 * math.log10(value / 32768.0) if value > 0 else None


def _wav_levels(path: Path) -> tuple[float | None, float | None]:
    try:
        with wave.open(str(path), "rb") as stream:
            if stream.getsampwidth() != 2:
                raise TranscriptBatchError("Sample WAV was not 16-bit PCM")
            samples = array("h", stream.readframes(stream.getnframes()))
    except (OSError, wave.Error) as exc:
        raise TranscriptBatchError("Sample WAV could not be measured") from exc
    if not samples:
        return None, None
    root_mean_square = math.sqrt(
        sum(sample * sample for sample in samples) / len(samples)
    )
    return _dbfs(root_mean_square), _dbfs(max(abs(sample) for sample in samples))


def _usable(windows: tuple[CollectedWindow, ...], minimum_words: int) -> bool:
    total_words = sum(window.word_count for window in windows)
    audible = any(
        window.mean_dbfs is not None and window.mean_dbfs >= -65 for window in windows
    )
    return total_words >= minimum_words and audible


def _attempt_stream(
    item: TranscriptBatchItem,
    asr: ASRProvider,
    extractor: AudioExtractor,
    *,
    stream_index: int,
    temporary_root: Path,
    sample_windows: tuple[SampleWindow, ...],
) -> tuple[CollectedWindow, ...]:
    collected: list[CollectedWindow] = []
    for ordinal, window in enumerate(sample_windows, start=1):
        output = temporary_root / (
            f"{item.file_id}-stream-{stream_index}-window-{ordinal}.wav"
        )
        extractor.extract(
            item.media_path,
            start_seconds=window.start_seconds,
            duration_seconds=window.duration_seconds,
            audio_stream_index=stream_index,
            output_path=output,
        )
        mean_dbfs, peak_dbfs = _wav_levels(output)
        text = clean_text(asr.transcribe(output))
        collected.append(
            CollectedWindow(
                start_seconds=window.start_seconds,
                text=text,
                word_count=len(text.split()),
                mean_dbfs=mean_dbfs,
                peak_dbfs=peak_dbfs,
            )
        )
    return tuple(collected)


def _collection_windows(
    item: TranscriptBatchItem,
    sampling_mode: Literal["standard", "expanded", "intro"],
    *,
    intro_start_seconds: float,
) -> tuple[SampleWindow, ...]:
    if sampling_mode == "standard":
        return build_audio_diagnostic_plan(
            item.media,
            media_id=item.file_id,
        ).sample_windows
    if sampling_mode == "expanded":
        duration = item.media.duration_seconds
        sample_duration = min(30, max(5, round(duration)))
        latest_start = max(0.0, duration - sample_duration)
        starts = {
            round(
                min(
                    latest_start,
                    max(0.0, duration * position / 7 - sample_duration / 2),
                ),
                3,
            )
            for position in range(1, 7)
        }
        return tuple(
            SampleWindow(start_seconds=start, duration_seconds=sample_duration)
            for start in sorted(starts)
        )
    if sampling_mode == "intro":
        duration = min(30, max(5, round(item.media.duration_seconds)))
        latest_start = max(0.0, item.media.duration_seconds - duration)
        return (
            SampleWindow(
                start_seconds=round(min(intro_start_seconds, latest_start), 3),
                duration_seconds=duration,
            ),
        )
    raise TranscriptBatchError("Transcript sampling mode is invalid")


def _collector_stream_indices(
    item: TranscriptBatchItem,
    *,
    maximum_streams: int,
    preferred_stream_index: int | None,
) -> tuple[int, ...]:
    """Honor an explicit stream override, then retain diagnostic-plan order."""

    plan = build_audio_diagnostic_plan(item.media, media_id=item.file_id)
    metadata = {stream.index: stream for stream in item.media.audio_streams}
    if preferred_stream_index is not None and preferred_stream_index not in metadata:
        raise TranscriptBatchError(
            f"Preferred audio stream is absent for media ID {item.file_id}"
        )
    ordered = sorted(
        plan.streams,
        key=lambda stream: (
            stream.stream_index != preferred_stream_index
            if preferred_stream_index is not None
            else False,
            metadata[stream.stream_index].is_commentary,
            not metadata[stream.stream_index].is_default,
            stream.rank,
        ),
    )
    return tuple(stream.stream_index for stream in ordered[:maximum_streams])


def _validate_items(items: tuple[TranscriptBatchItem, ...]) -> None:
    if not items or len(items) > 50:
        raise TranscriptBatchError("Batch must contain 1-50 explicit MKV files")
    if len({item.file_id for item in items}) != len(items):
        raise TranscriptBatchError("Batch file IDs must be unique")
    resolved_paths = [validate_explicit_mkv(item.media_path) for item in items]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise TranscriptBatchError("Each explicit MKV may appear only once")
    for item in items:
        if _FILE_ID.fullmatch(item.file_id) is None:
            raise TranscriptBatchError("Batch contains an invalid file ID")
        if not item.media.audio_streams:
            raise TranscriptBatchError("Saved FFprobe report contains no audio streams")


def collect_transcript_batch(
    items: tuple[TranscriptBatchItem, ...],
    asr: ASRProvider,
    extractor: AudioExtractor,
    *,
    model_name: str,
    minimum_words: int = 8,
    maximum_streams: int = 3,
    sampling_mode: Literal["standard", "expanded", "intro"] = "standard",
    intro_start_seconds: float = 60.0,
    preferred_stream_index: int | None = None,
    temporary_directory: Callable[..., tempfile.TemporaryDirectory] = (
        tempfile.TemporaryDirectory
    ),
) -> TranscriptBatchResult:
    """Collect windows sequentially while loading the CPU ASR provider once."""

    _validate_items(items)
    if (
        minimum_words <= 0
        or not 1 <= maximum_streams <= 3
        or not math.isfinite(intro_start_seconds)
        or intro_start_seconds < 0
        or preferred_stream_index is not None
        and preferred_stream_index < 0
    ):
        raise TranscriptBatchError("Batch stream and word limits are invalid")
    try:
        asr.load()
    except Exception as exc:
        raise TranscriptBatchError(
            f"CPU ASR model could not be loaded: {type(exc).__name__}"
        ) from exc

    results: list[CollectedFile] = []
    with temporary_directory(prefix="mkv-transcript-batch-") as folder:
        temporary_root = Path(folder)
        for item in items:
            sample_windows = _collection_windows(
                item,
                sampling_mode,
                intro_start_seconds=intro_start_seconds,
            )
            stream_indices = _collector_stream_indices(
                item,
                maximum_streams=maximum_streams,
                preferred_stream_index=preferred_stream_index,
            )
            best_windows: tuple[CollectedWindow, ...] = ()
            best_stream: int | None = None
            failure_code: str | None = None
            for stream_index in stream_indices:
                try:
                    windows = _attempt_stream(
                        item,
                        asr,
                        extractor,
                        stream_index=stream_index,
                        temporary_root=temporary_root,
                        sample_windows=sample_windows,
                    )
                except Exception as exc:
                    failure_code = f"sample-failed:{type(exc).__name__}"
                    continue
                if sum(window.word_count for window in windows) > sum(
                    window.word_count for window in best_windows
                ):
                    best_windows = windows
                    best_stream = stream_index
                if _usable(windows, minimum_words):
                    best_windows = windows
                    best_stream = stream_index
                    failure_code = None
                    break
                failure_code = "weak-or-silent-audio"

            status = (
                "collected"
                if best_stream is not None and _usable(best_windows, minimum_words)
                else "review-audio"
            )
            results.append(
                CollectedFile(
                    file_id=item.file_id,
                    duration_seconds=item.media.duration_seconds,
                    audio_stream_index=best_stream,
                    status=status,
                    windows=best_windows,
                    attempted_streams=stream_indices,
                    failure_code=None if status == "collected" else failure_code,
                )
            )
    return TranscriptBatchResult(
        mode="saved-transcript-evidence",
        model_name=model_name,
        device="cpu",
        files=tuple(results),
    )


def validate_new_report_paths(private_path: Path, safe_path: Path) -> None:
    if private_path.resolve() == safe_path.resolve():
        raise TranscriptBatchError("Private and safe report paths must be different")
    if private_path.exists():
        raise TranscriptBatchError(
            "Private transcript report exists; refusing overwrite"
        )
    if safe_path.exists():
        raise TranscriptBatchError("Safe metrics report exists; refusing overwrite")


def _write_new_json(path: Path, payload: dict[str, object]) -> Path:
    if path.exists():
        raise TranscriptBatchError("Transcript batch output exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_private_transcript_report(
    path: Path,
    result: TranscriptBatchResult,
) -> Path:
    return _write_new_json(path, result.private_report())


def write_safe_metrics_report(path: Path, result: TranscriptBatchResult) -> Path:
    return _write_new_json(path, result.safe_report())
