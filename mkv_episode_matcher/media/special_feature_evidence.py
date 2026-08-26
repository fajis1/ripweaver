"""Explicit-file visual and audio evidence collection for special features.

This module does not discover media.  Callers must supply every MKV, a
path-safe identifier, its reviewed duration, and the exact audio streams to
sample.  Derived evidence is private; the returned report contains neither
source paths nor OCR text.
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_MEDIA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class SpecialFeatureEvidenceError(RuntimeError):
    """Raised when evidence cannot be collected without unsafe behavior."""


@dataclass(frozen=True)
class SpecialFeatureEvidenceItem:
    media_id: str
    media_path: Path
    duration_seconds: float
    audio_stream_indexes: tuple[int, ...] = ()
    contact_sheet: bool = True
    ocr_contact_sheet: bool = True


@dataclass(frozen=True)
class SpecialFeatureEvidencePlan:
    items: tuple[SpecialFeatureEvidenceItem, ...]
    output_root: Path
    ffmpeg_path: Path
    tesseract_path: Path | None
    sample_start_seconds: float = 15.0
    sample_duration_seconds: float = 30.0


@dataclass(frozen=True)
class EvidenceItemResult:
    media_id: str
    status: str
    contact_sheet_created: bool
    ocr_text_characters: int
    sampled_audio_streams: tuple[int, ...]
    failure_code: str | None = None


@dataclass(frozen=True)
class SpecialFeatureEvidenceResult:
    items: tuple[EvidenceItemResult, ...]

    def safe_report(self) -> dict[str, object]:
        return {
            "mode": "special-feature-evidence-metrics",
            "item_count": len(self.items),
            "succeeded_count": sum(item.status == "collected" for item in self.items),
            "items": [
                {
                    "media_id": item.media_id,
                    "status": item.status,
                    "contact_sheet_created": item.contact_sheet_created,
                    "ocr_text_characters": item.ocr_text_characters,
                    "sampled_audio_streams": list(item.sampled_audio_streams),
                    "failure_code": item.failure_code,
                }
                for item in self.items
            ],
        }


def write_safe_evidence_report(
    path: Path,
    result: SpecialFeatureEvidenceResult,
) -> Path:
    """Write path-free metrics to one new report without dialogue or OCR text."""

    if path.exists():
        raise SpecialFeatureEvidenceError(
            "Evidence metrics report exists; refusing overwrite"
        )
    if not path.parent.is_dir():
        raise SpecialFeatureEvidenceError(
            "Evidence metrics report parent must already exist"
        )
    with path.open("x", encoding="utf-8") as output:
        json.dump(result.safe_report(), output, indent=2, sort_keys=True)
        output.write("\n")
    return path


class CommandRunner(Protocol):
    def __call__(
        self, command: tuple[str, ...], *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]: ...


def _validate_executable(path: Path, label: str) -> Path:
    if not path.is_file():
        raise SpecialFeatureEvidenceError(f"{label} executable was not found")
    return path.resolve()


def _validate_item(item: SpecialFeatureEvidenceItem) -> None:
    if not _MEDIA_ID.fullmatch(item.media_id):
        raise SpecialFeatureEvidenceError("Media ID is not path-safe")
    if not item.media_path.is_file() or item.media_path.suffix.lower() != ".mkv":
        raise SpecialFeatureEvidenceError("Input must be an explicit existing MKV")
    if not 10 <= item.duration_seconds <= 12 * 60 * 60:
        raise SpecialFeatureEvidenceError("Reviewed duration is outside safe bounds")
    if len(set(item.audio_stream_indexes)) != len(item.audio_stream_indexes):
        raise SpecialFeatureEvidenceError("Audio stream indexes must be unique")
    if any(index < 0 for index in item.audio_stream_indexes):
        raise SpecialFeatureEvidenceError("Audio stream index must not be negative")
    if item.ocr_contact_sheet and not item.contact_sheet:
        raise SpecialFeatureEvidenceError("Contact-sheet OCR requires a contact sheet")


def _validate_plan_settings(plan: SpecialFeatureEvidencePlan) -> None:
    if not plan.items:
        raise SpecialFeatureEvidenceError(
            "Evidence plan must contain at least one item"
        )
    if len({item.media_id.casefold() for item in plan.items}) != len(plan.items):
        raise SpecialFeatureEvidenceError("Media IDs must be unique")
    if not plan.output_root.is_dir():
        raise SpecialFeatureEvidenceError("Evidence output root must already exist")
    if plan.sample_start_seconds < 0:
        raise SpecialFeatureEvidenceError("Audio sample start must not be negative")
    if not 5 <= plan.sample_duration_seconds <= 60:
        raise SpecialFeatureEvidenceError(
            "Audio sample duration is outside safe bounds"
        )


def validate_evidence_plan(plan: SpecialFeatureEvidencePlan) -> None:
    """Preflight all inputs and collisions before creating any output."""

    _validate_executable(plan.ffmpeg_path, "FFmpeg")
    if plan.tesseract_path is not None:
        _validate_executable(plan.tesseract_path, "Tesseract")
    if (
        any(item.ocr_contact_sheet for item in plan.items)
        and plan.tesseract_path is None
    ):
        raise SpecialFeatureEvidenceError("Tesseract is required by the OCR plan")
    _validate_plan_settings(plan)
    for item in plan.items:
        _validate_item(item)
        if (plan.output_root / item.media_id).exists():
            raise SpecialFeatureEvidenceError(
                "Evidence target exists; refusing overwrite"
            )
        if (
            item.audio_stream_indexes
            and plan.sample_start_seconds + plan.sample_duration_seconds
            > item.duration_seconds
        ):
            raise SpecialFeatureEvidenceError("Audio sample exceeds reviewed duration")


def build_contact_sheet_command(
    ffmpeg_path: Path,
    item: SpecialFeatureEvidenceItem,
    output_path: Path,
) -> tuple[str, ...]:
    _validate_item(item)
    if output_path.exists() or output_path.suffix.lower() != ".png":
        raise SpecialFeatureEvidenceError("Contact-sheet target must be a new PNG")
    interval = item.duration_seconds / 6
    video_filter = (
        f"fps=1/{interval:.6f},scale=480:-2:flags=lanczos,tile=3x2:padding=8:margin=8"
    )
    return (
        str(_validate_executable(ffmpeg_path, "FFmpeg")),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(item.media_path.resolve()),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        video_filter,
        "-frames:v",
        "1",
        "-n",
        str(output_path),
    )


def build_audio_sample_command(
    ffmpeg_path: Path,
    item: SpecialFeatureEvidenceItem,
    *,
    stream_index: int,
    start_seconds: float,
    duration_seconds: float,
    output_path: Path,
) -> tuple[str, ...]:
    _validate_item(item)
    if stream_index not in item.audio_stream_indexes:
        raise SpecialFeatureEvidenceError("Audio stream was not authorized by the plan")
    if output_path.exists() or output_path.suffix.lower() != ".wav":
        raise SpecialFeatureEvidenceError("Audio target must be a new WAV")
    if start_seconds < 0 or not 5 <= duration_seconds <= 60:
        raise SpecialFeatureEvidenceError("Audio sample window is outside safe bounds")
    if start_seconds + duration_seconds > item.duration_seconds:
        raise SpecialFeatureEvidenceError("Audio sample exceeds reviewed duration")
    return (
        str(_validate_executable(ffmpeg_path, "FFmpeg")),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start_seconds),
        "-t",
        str(duration_seconds),
        "-i",
        str(item.media_path.resolve()),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-n",
        str(output_path),
    )


def build_tesseract_command(
    tesseract_path: Path, contact_sheet_path: Path
) -> tuple[str, ...]:
    if not contact_sheet_path.is_file():
        raise SpecialFeatureEvidenceError("Contact sheet is missing")
    return (
        str(_validate_executable(tesseract_path, "Tesseract")),
        str(contact_sheet_path),
        "stdout",
        "-l",
        "eng",
        "--psm",
        "11",
    )


def _default_runner(
    command: tuple[str, ...], *, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SpecialFeatureEvidenceError("Evidence command timed out") from exc
    except OSError as exc:
        raise SpecialFeatureEvidenceError(
            f"Evidence command failed to start: {type(exc).__name__}"
        ) from exc


def _run_checked(
    runner: CommandRunner,
    command: tuple[str, ...],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    completed = runner(command, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        raise SpecialFeatureEvidenceError(
            f"Evidence command failed with exit code {completed.returncode}"
        )
    return completed


def _collect_one(
    plan: SpecialFeatureEvidencePlan,
    item: SpecialFeatureEvidenceItem,
    runner: CommandRunner,
    timeout_seconds: float,
) -> EvidenceItemResult:
    item_dir = plan.output_root / item.media_id
    sheet_created = False
    ocr_characters = 0
    sampled: list[int] = []
    try:
        item_dir.mkdir()
        sheet_path = item_dir / "contact-sheet.png"
        if item.contact_sheet:
            command = build_contact_sheet_command(plan.ffmpeg_path, item, sheet_path)
            _run_checked(runner, command, timeout_seconds=timeout_seconds)
            if not sheet_path.is_file() or sheet_path.stat().st_size == 0:
                raise SpecialFeatureEvidenceError("Contact sheet was not created")
            sheet_created = True
        if item.ocr_contact_sheet:
            assert plan.tesseract_path is not None
            completed = _run_checked(
                runner,
                build_tesseract_command(plan.tesseract_path, sheet_path),
                timeout_seconds=timeout_seconds,
            )
            private_text = item_dir / "contact-sheet-ocr.txt"
            with private_text.open("x", encoding="utf-8") as output:
                output.write(completed.stdout)
            ocr_characters = len(completed.stdout.strip())
        for stream_index in item.audio_stream_indexes:
            audio_path = item_dir / f"audio-stream-{stream_index:02d}.wav"
            command = build_audio_sample_command(
                plan.ffmpeg_path,
                item,
                stream_index=stream_index,
                start_seconds=plan.sample_start_seconds,
                duration_seconds=plan.sample_duration_seconds,
                output_path=audio_path,
            )
            _run_checked(runner, command, timeout_seconds=timeout_seconds)
            if not audio_path.is_file() or audio_path.stat().st_size < 1024:
                raise SpecialFeatureEvidenceError("Audio sample was not created")
            sampled.append(stream_index)
        return EvidenceItemResult(
            media_id=item.media_id,
            status="collected",
            contact_sheet_created=sheet_created,
            ocr_text_characters=ocr_characters,
            sampled_audio_streams=tuple(sampled),
        )
    except Exception as exc:
        return EvidenceItemResult(
            media_id=item.media_id,
            status="failed",
            contact_sheet_created=sheet_created,
            ocr_text_characters=ocr_characters,
            sampled_audio_streams=tuple(sampled),
            failure_code=type(exc).__name__,
        )


def collect_special_feature_evidence(
    plan: SpecialFeatureEvidencePlan,
    *,
    max_workers: int = 3,
    timeout_seconds: float = 180,
    runner: CommandRunner = _default_runner,
) -> SpecialFeatureEvidenceResult:
    """Collect independent items concurrently after an all-item preflight."""

    if not 1 <= max_workers <= 3:
        raise SpecialFeatureEvidenceError("Worker count must be between one and three")
    if timeout_seconds <= 0:
        raise SpecialFeatureEvidenceError("Evidence timeout must be positive")
    validate_evidence_plan(plan)
    results: dict[str, EvidenceItemResult] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(plan.items))) as pool:
        futures = {
            pool.submit(
                _collect_one, plan, item, runner, timeout_seconds
            ): item.media_id
            for item in plan.items
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return SpecialFeatureEvidenceResult(
        items=tuple(results[item.media_id] for item in plan.items)
    )
