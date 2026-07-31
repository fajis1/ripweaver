"""Read-only media diagnostics using embedded bitmap-subtitle OCR.

The source MKV is never modified. OCR output must be written to an explicit,
collision-free diagnostic path, and durable reports omit paths and dialogue.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


class SubtitleOcrError(RuntimeError):
    """Raised when embedded-subtitle OCR cannot complete safely."""


@dataclass(frozen=True)
class OcrCandidate:
    reference_id: str
    query_coverage: float
    jaccard: float
    overlapping_shingles: int


@dataclass(frozen=True)
class SubtitleOcrDiagnostic:
    media_id: str
    caption_count: int
    text_characters: int
    text_words: int
    candidates: tuple[OcrCandidate, ...]

    def safe_report(self) -> dict[str, object]:
        """Return score-only evidence without media paths or dialogue."""

        return {
            "mode": "embedded-subtitle-ocr-diagnostic",
            "created_at": datetime.now(UTC).isoformat(),
            **asdict(self),
        }


_TIMECODE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+"
    r"\d{2}:\d{2}:\d{2}[,.]\d{3}"
)
_TAG = re.compile(r"<[^>]+>")
_TOKEN = re.compile(r"[a-z0-9]+")


def parse_srt_text(content: str) -> tuple[str, int]:
    """Return normalized dialogue and the number of numbered SRT blocks."""

    text_lines: list[str] = []
    caption_count = 0
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.isdigit():
            caption_count += 1
            continue
        if _TIMECODE.match(line):
            continue
        text_lines.append(_TAG.sub(" ", line))
    return " ".join(" ".join(text_lines).split()), caption_count


def _shingles(text: str, size: int) -> set[tuple[str, ...]]:
    tokens = _TOKEN.findall(text.lower())
    if len(tokens) < size:
        return set()
    return {
        tuple(tokens[index : index + size])
        for index in range(len(tokens) - size + 1)
    }


def rank_ocr_references(
    query: str,
    references: dict[str, str],
    *,
    top_k: int = 5,
    shingle_size: int = 3,
) -> tuple[OcrCandidate, ...]:
    """Rank reference transcripts using linear-time exact word shingles."""

    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if shingle_size <= 0:
        raise ValueError("shingle_size must be positive")

    query_shingles = _shingles(query, shingle_size)
    if not query_shingles:
        return ()

    candidates: list[OcrCandidate] = []
    for reference_id, reference in references.items():
        reference_shingles = _shingles(reference, shingle_size)
        overlap = len(query_shingles & reference_shingles)
        union = len(query_shingles | reference_shingles)
        candidates.append(
            OcrCandidate(
                reference_id=reference_id,
                query_coverage=overlap / len(query_shingles),
                jaccard=overlap / union if union else 0.0,
                overlapping_shingles=overlap,
            )
        )
    candidates.sort(
        key=lambda candidate: (
            candidate.query_coverage,
            candidate.jaccard,
            candidate.overlapping_shingles,
            candidate.reference_id,
        ),
        reverse=True,
    )
    return tuple(candidates[:top_k])


def build_seconv_ocr_command(
    media_path: Path,
    output_path: Path,
    seconv_path: Path,
    *,
    language: str = "eng",
    track_number: int | None = None,
) -> list[str]:
    """Build a non-overwriting SeConv/Tesseract OCR command."""

    if not media_path.is_file() or media_path.suffix.lower() != ".mkv":
        raise SubtitleOcrError("OCR input must be an explicit MKV")
    if not seconv_path.is_file():
        raise SubtitleOcrError("SeConv executable was not found")
    if output_path.suffix.lower() != ".srt":
        raise SubtitleOcrError("OCR output must use the .srt extension")
    if output_path.exists():
        raise SubtitleOcrError("OCR output exists; refusing overwrite")
    if not language.strip():
        raise SubtitleOcrError("OCR language is required")
    if track_number is not None and track_number < 0:
        raise SubtitleOcrError("OCR track number must not be negative")

    command = [
        str(seconv_path),
        str(media_path),
        "subrip",
        "--ocr-engine:tesseract",
        f"--ocr-language:{language.strip()}",
        f"--output-folder:{output_path.parent}",
        f"--output-filename:{output_path.name}",
        "--json",
    ]
    if track_number is not None:
        command.insert(-1, f"--track-number:{track_number}")
    return command


def run_seconv_ocr(
    media_path: Path,
    output_path: Path,
    seconv_path: Path,
    tesseract_path: Path,
    *,
    language: str = "eng",
    track_number: int | None = None,
    timeout_seconds: float = 180.0,
) -> tuple[str, int]:
    """OCR one MKV sequentially and return its normalized subtitle text."""

    if not tesseract_path.is_file():
        raise SubtitleOcrError("Tesseract executable was not found")
    if timeout_seconds <= 0:
        raise SubtitleOcrError("OCR timeout must be positive")
    command = build_seconv_ocr_command(
        media_path,
        output_path,
        seconv_path,
        language=language,
        track_number=track_number,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PATH"] = (
        f"{tesseract_path.parent}{os.pathsep}{environment.get('PATH', '')}"
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise SubtitleOcrError("Embedded subtitle OCR timed out") from exc
    except OSError as exc:
        raise SubtitleOcrError(
            f"Embedded subtitle OCR failed to start: {type(exc).__name__}"
        ) from exc

    if result.returncode != 0:
        raise SubtitleOcrError(
            f"Embedded subtitle OCR failed with exit code {result.returncode}"
        )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise SubtitleOcrError("Embedded subtitle OCR produced no SRT output")
    return parse_srt_text(output_path.read_text(encoding="utf-8-sig"))


def write_safe_ocr_report(
    path: Path,
    diagnostic: SubtitleOcrDiagnostic,
) -> Path:
    if path.exists():
        raise SubtitleOcrError("OCR diagnostic report exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(diagnostic.safe_report(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
