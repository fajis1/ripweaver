"""Normalize saved FFprobe JSON without executing FFprobe or reading media."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProbeDataError(RuntimeError):
    """Raised when saved FFprobe JSON is missing required metadata."""


@dataclass(frozen=True)
class ProbedAudioStream:
    index: int
    codec: str | None
    language: str | None
    title: str | None
    channels: int | None
    channel_layout: str | None
    is_default: bool
    is_commentary: bool


@dataclass(frozen=True)
class ProbedMedia:
    duration_seconds: float
    size_bytes: int | None
    container: str | None
    audio_streams: tuple[ProbedAudioStream, ...]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_ffprobe_payload(payload: dict[str, Any]) -> ProbedMedia:
    """Normalize the subset of FFprobe JSON required for audio planning."""

    format_data = payload.get("format", {})
    if not isinstance(format_data, dict):
        format_data = {}
    duration = _optional_float(format_data.get("duration"))
    if duration is None or duration <= 0:
        raise ProbeDataError("Saved FFprobe data has no positive media duration")

    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise ProbeDataError("Saved FFprobe data has no stream list")

    audio_streams: list[ProbedAudioStream] = []
    for raw_stream in streams:
        if not isinstance(raw_stream, dict):
            continue
        if raw_stream.get("codec_type") != "audio":
            continue
        tags = raw_stream.get("tags", {})
        if not isinstance(tags, dict):
            tags = {}
        disposition = raw_stream.get("disposition", {})
        if not isinstance(disposition, dict):
            disposition = {}
        title = tags.get("title")
        descriptive_text = " ".join(
            str(value or "")
            for value in (
                title,
                tags.get("handler_name"),
                tags.get("comment"),
            )
        ).lower()
        audio_streams.append(
            ProbedAudioStream(
                index=int(raw_stream.get("index", -1)),
                codec=raw_stream.get("codec_name"),
                language=tags.get("language"),
                title=title,
                channels=_optional_int(raw_stream.get("channels")),
                channel_layout=raw_stream.get("channel_layout"),
                is_default=bool(disposition.get("default", 0)),
                is_commentary=any(
                    marker in descriptive_text
                    for marker in ("commentary", "director", "descriptive")
                ),
            )
        )

    return ProbedMedia(
        duration_seconds=duration,
        size_bytes=_optional_int(format_data.get("size")),
        container=format_data.get("format_name"),
        audio_streams=tuple(sorted(audio_streams, key=lambda stream: stream.index)),
    )


def load_ffprobe_payload(path: Path) -> ProbedMedia:
    """Load saved FFprobe JSON without including its source path in errors."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeDataError(
            f"Could not read saved FFprobe data: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProbeDataError("Saved FFprobe data must contain a JSON object")
    return parse_ffprobe_payload(payload)
