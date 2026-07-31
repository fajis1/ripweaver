"""Plan audio diagnostics from normalized metadata without executing tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from mkv_episode_matcher.media.probe import ProbedAudioStream, ProbedMedia

SAMPLE_DURATION_SECONDS = 30
SAMPLE_POSITIONS = (0.25, 0.50, 0.75)


@dataclass(frozen=True)
class SampleWindow:
    start_seconds: float
    duration_seconds: int


@dataclass(frozen=True)
class StreamDiagnostic:
    stream_index: int
    rank: int
    role: Literal["primary", "alternate"]
    downmix: Literal["none", "dialogue-preserving-stereo"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AudioDiagnosticPlan:
    media_id: str
    mode: Literal["plan-only"]
    duration_seconds: float
    sample_windows: tuple[SampleWindow, ...]
    streams: tuple[StreamDiagnostic, ...]
    measurements: tuple[str, ...]
    fallback_policy: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stream_rank(stream: ProbedAudioStream) -> tuple[int, int, int, int, int]:
    language = (stream.language or "").lower()
    english = language in {"eng", "en", "english"}
    stereo = stream.channels == 2
    return (
        1 if stream.is_commentary else 0,
        0 if english else 1,
        0 if stereo else 1,
        0 if stream.is_default else 1,
        stream.index,
    )


def _sample_windows(duration_seconds: float) -> tuple[SampleWindow, ...]:
    if duration_seconds <= SAMPLE_DURATION_SECONDS:
        return (
            SampleWindow(
                start_seconds=0.0,
                duration_seconds=max(1, round(duration_seconds)),
            ),
        )

    latest_start = duration_seconds - SAMPLE_DURATION_SECONDS
    starts = {
        round(
            min(
                latest_start,
                max(
                    0.0,
                    duration_seconds * position - SAMPLE_DURATION_SECONDS / 2,
                ),
            ),
            3,
        )
        for position in SAMPLE_POSITIONS
    }
    return tuple(
        SampleWindow(
            start_seconds=start,
            duration_seconds=SAMPLE_DURATION_SECONDS,
        )
        for start in sorted(starts)
    )


def build_audio_diagnostic_plan(
    media: ProbedMedia,
    *,
    media_id: str,
) -> AudioDiagnosticPlan:
    """Rank streams and samples while producing no executable command."""

    ranked = sorted(media.audio_streams, key=_stream_rank)
    stream_plans: list[StreamDiagnostic] = []
    for rank, stream in enumerate(ranked, start=1):
        language = stream.language or "unknown language"
        channel_description = stream.channel_layout or (
            f"{stream.channels} channels"
            if stream.channels is not None
            else "unknown channel layout"
        )
        reasons = [
            f"{language}; {channel_description}.",
            "Commentary/descriptive track."
            if stream.is_commentary
            else "No commentary marker detected.",
        ]
        if stream.channels == 2:
            reasons.append("Stereo can be measured and transcribed directly.")
        elif stream.channels is not None and stream.channels > 2:
            reasons.append("Use a dialogue-preserving stereo downmix for diagnostics.")
        stream_plans.append(
            StreamDiagnostic(
                stream_index=stream.index,
                rank=rank,
                role="primary" if rank == 1 else "alternate",
                downmix=(
                    "dialogue-preserving-stereo"
                    if stream.channels is not None and stream.channels > 2
                    else "none"
                ),
                reasons=tuple(reasons),
            )
        )

    return AudioDiagnosticPlan(
        media_id=media_id,
        mode="plan-only",
        duration_seconds=media.duration_seconds,
        sample_windows=_sample_windows(media.duration_seconds),
        streams=tuple(stream_plans),
        measurements=(
            "integrated_loudness_lufs",
            "true_peak_dbfs",
            "mean_volume_db",
            "silence_ratio",
            "transcript_word_count",
            "transcript_information_score",
        ),
        fallback_policy=(
            "If the primary stream is silent, low-information, or fails "
            "transcription, evaluate alternates in rank order."
        ),
    )
