"""Constrained, read-only FFprobe inspection for explicit MKV files."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.media.probe import (
    ProbeDataError,
    ProbedMedia,
    parse_ffprobe_payload,
)


class FFprobeError(RuntimeError):
    """Raised when a constrained FFprobe inspection cannot complete safely."""


@dataclass(frozen=True)
class FFprobeInspection:
    """Captured FFprobe result and its normalized, path-free metadata."""

    return_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    media: ProbedMedia


def resolve_ffprobe_path(explicit_path: Path | None = None) -> Path:
    """Resolve FFprobe from an option, environment setting, or ``PATH``."""

    candidate = explicit_path or load_environment_settings().ffprobe_path
    if candidate is None:
        discovered = shutil.which("ffprobe")
        if discovered is None:
            raise FFprobeError(
                "FFprobe executable was not found; configure FFPROBE_PATH or "
                "pass --ffprobe-path"
            )
        candidate = Path(discovered)

    candidate = candidate.expanduser()
    if not candidate.is_file():
        raise FFprobeError("Configured FFprobe executable was not found")
    return candidate.resolve()


def validate_mkv_path(media_path: Path) -> Path:
    """Require one explicit, existing MKV file and return its absolute path."""

    if media_path.suffix.lower() != ".mkv":
        raise FFprobeError("FFprobe inspection accepts explicit .mkv files only")
    if not media_path.is_file():
        raise FFprobeError("The requested MKV input is not an existing file")
    return media_path.resolve()


def build_ffprobe_command(
    executable: Path,
    media_path: Path,
) -> tuple[str, ...]:
    """Build a fixed FFprobe metadata command with no mutation capability."""

    source = validate_mkv_path(media_path)
    return (
        str(executable),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        str(source),
    )


def _redact_source(text: str, media_path: Path) -> str:
    """Remove common renderings of a source path from captured diagnostics."""

    redacted = text
    candidates = {
        str(media_path),
        str(media_path.resolve()),
        str(media_path).replace("\\", "/"),
        str(media_path.resolve()).replace("\\", "/"),
        media_path.name,
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            redacted = redacted.replace(candidate, "<media>")
    return redacted


def inspect_mkv(
    executable: Path,
    media_path: Path,
    *,
    timeout_seconds: int = 60,
) -> FFprobeInspection:
    """Run fixed FFprobe inspection flags and normalize the captured JSON."""

    if timeout_seconds <= 0:
        raise FFprobeError("FFprobe timeout must be positive")

    source = validate_mkv_path(media_path)
    command = build_ffprobe_command(executable, source)
    started = datetime.now(UTC)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFprobeError(
            f"FFprobe inspection timed out after {timeout_seconds}s"
        ) from exc
    except OSError as exc:
        raise FFprobeError(
            f"FFprobe could not be started: {type(exc).__name__}"
        ) from exc

    finished = datetime.now(UTC)
    stderr = _redact_source(completed.stderr, source)
    if completed.returncode != 0:
        detail = f" ({stderr.strip()})" if stderr.strip() else ""
        raise FFprobeError(
            f"FFprobe inspection failed with exit code {completed.returncode}{detail}"
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FFprobeError("FFprobe returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise FFprobeError("FFprobe returned JSON that was not an object")

    try:
        media = parse_ffprobe_payload(payload)
    except ProbeDataError as exc:
        raise FFprobeError(f"FFprobe metadata was incomplete: {exc}") from exc

    stdout = json.dumps(sanitized_ffprobe_payload(media), sort_keys=True)
    return FFprobeInspection(
        return_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        media=media,
    )


def sanitized_ffprobe_payload(media: ProbedMedia) -> dict[str, Any]:
    """Return replayable FFprobe-shaped JSON without source-identifying fields."""

    streams: list[dict[str, Any]] = []
    if media.video_codec or media.video_width or media.video_height:
        streams.append({
            "index": 0,
            "codec_type": "video",
            "codec_name": media.video_codec,
            "width": media.video_width,
            "height": media.video_height,
            "field_order": media.video_field_order,
            "bit_rate": media.video_bit_rate,
            "avg_frame_rate": media.video_frame_rate,
            "profile": media.video_profile,
            "pix_fmt": media.video_pixel_format,
            "bits_per_raw_sample": media.video_bit_depth,
            "color_primaries": media.video_color_primaries,
            "color_transfer": media.video_color_transfer,
            "color_space": media.video_color_space,
            "color_range": media.video_color_range,
            "side_data_list": (
                [{"side_data_type": "Dolby Vision configuration record"}]
                if media.video_hdr_format == "Dolby Vision"
                else []
            ),
            "tags": ({"encoder": media.video_encoder} if media.video_encoder else {}),
        })
    for stream in media.audio_streams:
        tags = {
            key: value
            for key, value in {
                "language": stream.language,
                "title": stream.title,
            }.items()
            if value is not None
        }
        streams.append({
            "index": stream.index,
            "codec_type": "audio",
            "codec_name": stream.codec,
            "channels": stream.channels,
            "channel_layout": stream.channel_layout,
            "bit_rate": stream.bit_rate,
            "sample_rate": stream.sample_rate,
            "tags": tags,
            "disposition": {"default": int(stream.is_default)},
        })

    return {
        "format": {
            "duration": media.duration_seconds,
            "size": media.size_bytes,
            "format_name": media.container,
            "bit_rate": media.overall_bit_rate,
            "tags": ({"encoder": media.format_encoder} if media.format_encoder else {}),
        },
        "streams": streams,
    }


def write_sanitized_probe_report(
    output_dir: Path,
    media: ProbedMedia,
    *,
    media_id: str,
) -> Path:
    """Save normalized probe metadata under a non-source-derived identifier."""

    if re.fullmatch(r"[A-Za-z0-9_-]+", media_id) is None:
        raise FFprobeError("Media report ID contains unsupported characters")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{media_id}.ffprobe.json"
    report_path.write_text(
        json.dumps(sanitized_ffprobe_payload(media), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report_path
