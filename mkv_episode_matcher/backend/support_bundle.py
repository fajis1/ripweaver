"""Create bounded, privacy-redacted support bundles for user-initiated export."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import sys
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_LOG_FILES = 5
MAX_LOG_BYTES_PER_FILE = 512 * 1024
MAX_TOTAL_LOG_BYTES = 2 * 1024 * 1024
MAX_EVENT_COUNT = 1000
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_STRING_LENGTH = 1024
MAX_COLLECTION_ITEMS = 100
MAX_MAPPING_ITEMS = 150

_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\r\n\t\"'<>|]*"
)
_POSIX_PATH_RE = re.compile(r"(?<![:/\w])/(?:[^/\s]+/)*[^/\s,;:)\]}\"']+")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_IPV4_RE = re.compile(
    r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_VALUE_RE = re.compile(
    r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|"
    r"password|passwd|secret|authorization|credential)[\"']?\s*[:=]\s*)"
    r"(?:[\"'][^\"'\r\n]*[\"']|[^\s,;}\]]+)"
)
_QUOTED_MEDIA_RE = re.compile(
    r"(?i)([\"'])[^\"'\r\n]{0,300}\.(?:mkv|mp4|avi|mov|m4v|ts|iso)\1"
)
_MEDIA_TOKEN_RE = re.compile(
    r"(?i)(?<!\w)[^\s\"'<>|]+\.(?:mkv|mp4|avi|mov|m4v|ts|iso)\b"
)

_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "authorization",
    "command",
    "credential",
    "credentials",
    "destination",
    "destination_relative",
    "dialogue",
    "disc_label",
    "display_name",
    "drive_label",
    "env",
    "environment",
    "episode_title",
    "file",
    "filename",
    "local_path",
    "matched_file",
    "original_file",
    "outcome_name",
    "password",
    "path",
    "request_headers",
    "secret",
    "series_name",
    "source_name",
    "source_path",
    "title",
    "token",
    "transcript",
    "username",
}
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "credential",
    "dialogue",
    "password",
    "secret",
    "token",
    "transcript",
)
_SENSITIVE_KEY_SUFFIXES = (
    "_dir",
    "_directory",
    "_file",
    "_filename",
    "_path",
    "_root",
)


@dataclass(frozen=True)
class SupportBundle:
    """An in-memory support archive and its public download metadata."""

    content: bytes
    filename: str
    sha256: str
    support_id: str


def redact_text(value: str) -> str:
    """Remove common secret, path, address, and media-name forms from text."""

    redacted = _BEARER_RE.sub("Bearer [redacted]", value)
    redacted = _SECRET_VALUE_RE.sub(r"\1[redacted]", redacted)
    redacted = _WINDOWS_PATH_RE.sub("[path redacted]", redacted)
    redacted = _POSIX_PATH_RE.sub("[path redacted]", redacted)
    redacted = _QUOTED_MEDIA_RE.sub("[media name redacted]", redacted)
    redacted = _MEDIA_TOKEN_RE.sub("[media name redacted]", redacted)
    redacted = _EMAIL_RE.sub("[email redacted]", redacted)
    return _IPV4_RE.sub("[network address redacted]", redacted)


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return (
        normalized in _SENSITIVE_EXACT_KEYS
        or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)
        or any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)
    )


def _sanitize_mapping(value: Mapping[Any, Any], *, depth: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, (item_key, item_value) in enumerate(value.items()):
        if index >= MAX_MAPPING_ITEMS:
            result["_truncated"] = True
            break
        public_key = str(item_key)[:128]
        result[public_key] = sanitize_value(item_value, key=public_key, depth=depth + 1)
    return result


def _sanitize_iterable(value: Iterable[Any], *, depth: int) -> list[Any]:
    items = []
    for index, item in enumerate(value):
        if index >= MAX_COLLECTION_ITEMS:
            items.append("[additional items omitted]")
            break
        items.append(sanitize_value(item, depth=depth + 1))
    return items


def sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a bounded JSON-safe value with private fields redacted."""

    if key and _sensitive_key(key):
        return "[redacted]"
    if depth >= 8:
        return "[nested data omitted]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Path):
        return "[path redacted]"
    if isinstance(value, str):
        cleaned = redact_text(value)
        if len(cleaned) > MAX_STRING_LENGTH:
            return cleaned[:MAX_STRING_LENGTH] + "...[truncated]"
        return cleaned
    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_value(asdict(value), key=key, depth=depth + 1)
    if isinstance(value, Mapping):
        return _sanitize_mapping(value, depth=depth)
    if isinstance(value, Iterable) and not isinstance(value, bytes | bytearray):
        return _sanitize_iterable(value, depth=depth)
    return redact_text(str(value))[:MAX_STRING_LENGTH]


def summarize_config(config: Any) -> dict[str, object]:
    """Expose only explicitly enumerated, non-secret configuration status."""

    from mkv_episode_matcher.media.gemini_failover import ordered_gemini_models

    path_fields = (
        "rip_output_root",
        "disc_image_root",
        "transcode_output_root",
        "deletion_staging_root",
        "jellyfin_tv_root",
        "jellyfin_movie_root",
        "makemkv_path",
        "disc_image_creator_path",
        "handbrake_path",
        "ffmpeg_path",
        "ffprobe_path",
        "tesseract_path",
    )
    feature_fields = (
        "automatic_processing_enabled",
        "automatic_eject_after_rip",
        "automatic_gemini_ambiguity_fallback",
        "automatic_organization_enabled",
        "thediscdb_lookup_enabled",
        "ripweaver_catalogue_enabled",
        "ripweaver_catalogue_contributions_enabled",
    )
    return {
        "providers": {
            "audio_recognition": str(getattr(config, "asr_provider", "unknown")),
            "subtitle": str(getattr(config, "sub_provider", "unknown")),
            "gemini_models": list(
                ordered_gemini_models(
                    str(getattr(config, "gemini_model", "unknown")),
                    getattr(config, "gemini_fallback_models", ()),
                )
            ),
        },
        "configured_locations_and_tools": {
            field: getattr(config, field, None) is not None for field in path_fields
        },
        "features": {
            field: bool(getattr(config, field, False)) for field in feature_fields
        },
    }


def _log_candidates(log_dir: Path) -> tuple[Path, ...]:
    try:
        if not log_dir.is_dir() or log_dir.is_symlink():
            return ()
        candidates = []
        for item in log_dir.iterdir():
            name = item.name.casefold()
            if (
                name.startswith("mkv-match")
                and ".log" in name
                and item.is_file()
                and not item.is_symlink()
            ):
                candidates.append(item)
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return tuple(candidates[:MAX_LOG_FILES])
    except OSError:
        return ()


def _read_file_tail(path: Path, limit: int) -> tuple[str, bool]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(-limit, io.SEEK_END)
        data = handle.read(limit)
    return data.decode("utf-8", errors="replace"), size > limit


def collect_redacted_logs(log_dir: Path) -> tuple[str, dict[str, object]]:
    """Read only bounded RipWeaver log tails and remove private values."""

    segments = []
    errors = []
    total_bytes = 0
    truncated_files = 0
    for index, path in enumerate(_log_candidates(log_dir), start=1):
        remaining = MAX_TOTAL_LOG_BYTES - total_bytes
        if remaining <= 0:
            break
        try:
            text, truncated = _read_file_tail(
                path, min(MAX_LOG_BYTES_PER_FILE, remaining)
            )
            encoded_size = len(text.encode("utf-8", errors="replace"))
            total_bytes += encoded_size
            truncated_files += int(truncated)
            segments.append(
                f"===== application log segment {index} =====\n{redact_text(text)}"
            )
        except OSError as error:
            errors.append(type(error).__name__)
    metadata: dict[str, object] = {
        "segments_included": len(segments),
        "source_bytes_read": total_bytes,
        "truncated_segments": truncated_files,
        "read_error_types": sorted(set(errors)),
    }
    return "\n\n".join(segments), metadata


def _public_event(event: Any) -> dict[str, Any]:
    source = (
        asdict(event) if is_dataclass(event) and not isinstance(event, type) else event
    )
    if not isinstance(source, Mapping):
        return {"event": sanitize_value(source)}
    media_id = str(source.get("media_id", ""))
    public_media_id = (
        f"media-{hashlib.sha256(media_id.encode('utf-8')).hexdigest()[:12]}"
        if media_id
        else None
    )
    return {
        "sequence": sanitize_value(source.get("sequence")),
        "media_id": public_media_id,
        "event_type": sanitize_value(source.get("event_type")),
        "stage": sanitize_value(source.get("stage")),
        "state": sanitize_value(source.get("state")),
        "created_at": sanitize_value(source.get("created_at")),
        "details": sanitize_value(source.get("details", {}), key="details"),
    }


def bounded_public_events(events: Iterable[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Keep the newest sanitized events within count and serialized-size limits."""

    source = list(events)
    count_truncated = len(source) > MAX_EVENT_COUNT
    selected = []
    encoded_size = 2
    for event in reversed(source[-MAX_EVENT_COUNT:]):
        public = _public_event(event)
        event_size = len(
            json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if encoded_size + event_size + 1 > MAX_EVENT_BYTES:
            count_truncated = True
            break
        selected.append(public)
        encoded_size += event_size + 1
    selected.reverse()
    return selected, count_truncated


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def build_support_bundle(
    *,
    app_version: str,
    config: Any,
    system_health: Mapping[str, Any],
    pipeline_events: Iterable[Any] = (),
    log_dir: Path,
    collection_errors: Iterable[Mapping[str, str]] = (),
    generated_at: datetime | None = None,
    support_id: str | None = None,
) -> SupportBundle:
    """Build one ZIP without retaining it on disk or reading unrelated files."""

    generated = generated_at or datetime.now(timezone.utc)
    generated = generated.astimezone(timezone.utc)
    identifier = support_id or uuid4().hex[:12]
    public_events, events_truncated = bounded_public_events(pipeline_events)
    redacted_logs, log_metadata = collect_redacted_logs(log_dir)
    errors = [sanitize_value(error) for error in collection_errors]
    summary = {
        "schema_version": 1,
        "support_id": identifier,
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "application": {
            "name": "RipWeaver",
            "version": redact_text(app_version),
            "packaged_build": bool(getattr(sys, "frozen", False)),
        },
        "runtime": {
            "operating_system": platform.system(),
            "operating_system_release": redact_text(platform.release()),
            "architecture": redact_text(platform.machine()),
            "python_version": platform.python_version(),
        },
        "configuration": summarize_config(config),
        "collection": {
            "pipeline_events_included": len(public_events),
            "pipeline_events_truncated": events_truncated,
            "application_logs": log_metadata,
            "error_types": errors,
        },
        "privacy": {
            "paths_redacted": True,
            "credentials_redacted": True,
            "media_names_redacted": True,
            "dialogue_excluded": True,
            "private_provider_transactions_excluded": True,
            "environment_excluded": True,
        },
    }
    safe_health = sanitize_value(system_health)
    readme = f"""RipWeaver support bundle
Support ID: {identifier}
Generated: {summary["generated_at"]}

This archive was created locally after you selected Export Support Bundle.
It was not uploaded or emailed automatically.

Included:
- Application/runtime and non-secret configuration status
- Path-redacted Setup & Health results
- Recent path-redacted pipeline events
- Bounded tails of RipWeaver application logs, when available

Excluded:
- API keys, passwords, tokens, and environment values
- Configuration and media paths
- Media filenames and disc labels
- Transcript dialogue and private provider responses
- Media files and disc contents

Automated redaction is deliberately conservative but cannot guarantee that text
you typed into an error-producing external tool is unidentifiable. Review this
archive before sharing it. Attach the ZIP, not private media or .env files, to a
GitHub bug report or support email.
"""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        archive.writestr("support-summary.json", _json_bytes(summary))
        archive.writestr("system-health.json", _json_bytes(safe_health))
        archive.writestr(
            "pipeline-events.json",
            _json_bytes({
                "events": public_events,
                "truncated": events_truncated,
                "path_redacted": True,
                "dialogue_redacted": True,
            }),
        )
        if redacted_logs:
            archive.writestr("application.log", redacted_logs)

    content = output.getvalue()
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", app_version).strip("-") or "unknown"
    filename = f"RipWeaver-Support-{safe_version}-{stamp}-{identifier}.zip"
    return SupportBundle(
        content=content,
        filename=filename,
        sha256=hashlib.sha256(content).hexdigest(),
        support_id=identifier,
    )
