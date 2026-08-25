"""Normalize saved FFprobe JSON without executing FFprobe or reading media."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from fractions import Fraction
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
    bit_rate: int | None = None
    sample_rate: int | None = None


@dataclass(frozen=True)
class ProbedMedia:
    duration_seconds: float
    size_bytes: int | None
    container: str | None
    audio_streams: tuple[ProbedAudioStream, ...]
    video_codec: str | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_field_order: str | None = None
    overall_bit_rate: int | None = None
    video_bit_rate: int | None = None
    video_frame_rate: float | None = None
    video_profile: str | None = None
    video_pixel_format: str | None = None
    video_bit_depth: int | None = None
    video_hdr_format: str | None = None
    video_color_primaries: str | None = None
    video_color_transfer: str | None = None
    video_color_space: str | None = None
    video_color_range: str | None = None
    video_encoder: str | None = None
    format_encoder: str | None = None


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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_frame_rate(value: Any) -> float | None:
    text = _optional_text(value)
    if text is None or text in {"0/0", "N/A"}:
        return None
    try:
        rate = float(Fraction(text)) if "/" in text else float(text)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def _video_bit_depth(stream: dict[str, Any]) -> int | None:
    explicit = _optional_int(
        stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
    )
    if explicit is not None and explicit > 0:
        return explicit
    pixel_format = _optional_text(stream.get("pix_fmt"))
    if pixel_format is None:
        return None
    p010 = re.search(r"p0(\d{2})(?:le|be)?$", pixel_format)
    if p010:
        return int(p010.group(1))
    planar = re.search(r"p(\d{2})(?:le|be)?$", pixel_format)
    if planar:
        return int(planar.group(1))
    if pixel_format in {
        "nv12",
        "nv21",
        "yuv420p",
        "yuv422p",
        "yuv444p",
        "yuvj420p",
        "yuvj422p",
        "yuvj444p",
        "rgb24",
        "bgr24",
    }:
        return 8
    return None


def _video_hdr_format(stream: dict[str, Any]) -> str | None:
    side_data = stream.get("side_data_list", [])
    if isinstance(side_data, list) and any(
        isinstance(entry, dict)
        and any(
            marker in str(entry.get("side_data_type") or "").casefold()
            for marker in ("dolby vision", "dovi")
        )
        for entry in side_data
    ):
        return "Dolby Vision"
    transfer = str(stream.get("color_transfer") or "").casefold()
    if transfer == "smpte2084":
        return "HDR (PQ / SMPTE ST 2084)"
    if transfer == "arib-std-b67":
        return "HDR (HLG)"
    return None


def _tag_text(tags: object, *names: str) -> str | None:
    if not isinstance(tags, dict):
        return None
    folded = {str(key).casefold(): value for key, value in tags.items()}
    for name in names:
        value = _optional_text(folded.get(name.casefold()))
        if value is not None:
            return value
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
    video_stream: dict[str, Any] | None = None
    for raw_stream in streams:
        if not isinstance(raw_stream, dict):
            continue
        if raw_stream.get("codec_type") == "video" and video_stream is None:
            video_stream = raw_stream
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
                bit_rate=_optional_int(raw_stream.get("bit_rate")),
                sample_rate=_optional_int(raw_stream.get("sample_rate")),
            )
        )

    format_tags = format_data.get("tags", {})
    video_tags = video_stream.get("tags", {}) if video_stream else {}
    frame_rate = (
        _optional_frame_rate(video_stream.get("avg_frame_rate"))
        or _optional_frame_rate(video_stream.get("r_frame_rate"))
        if video_stream
        else None
    )

    return ProbedMedia(
        duration_seconds=duration,
        size_bytes=_optional_int(format_data.get("size")),
        container=format_data.get("format_name"),
        audio_streams=tuple(sorted(audio_streams, key=lambda stream: stream.index)),
        video_codec=video_stream.get("codec_name") if video_stream else None,
        video_width=_optional_int(video_stream.get("width")) if video_stream else None,
        video_height=_optional_int(video_stream.get("height"))
        if video_stream
        else None,
        video_field_order=(
            str(video_stream.get("field_order") or "unknown").lower()
            if video_stream
            else None
        ),
        overall_bit_rate=_optional_int(format_data.get("bit_rate")),
        video_bit_rate=_optional_int(video_stream.get("bit_rate"))
        if video_stream
        else None,
        video_frame_rate=frame_rate,
        video_profile=_optional_text(video_stream.get("profile"))
        if video_stream
        else None,
        video_pixel_format=_optional_text(video_stream.get("pix_fmt"))
        if video_stream
        else None,
        video_bit_depth=_video_bit_depth(video_stream) if video_stream else None,
        video_hdr_format=_video_hdr_format(video_stream) if video_stream else None,
        video_color_primaries=_optional_text(video_stream.get("color_primaries"))
        if video_stream
        else None,
        video_color_transfer=_optional_text(video_stream.get("color_transfer"))
        if video_stream
        else None,
        video_color_space=_optional_text(video_stream.get("color_space"))
        if video_stream
        else None,
        video_color_range=_optional_text(video_stream.get("color_range"))
        if video_stream
        else None,
        video_encoder=_tag_text(video_tags, "encoder"),
        format_encoder=_tag_text(format_tags, "encoder", "writing_application"),
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
