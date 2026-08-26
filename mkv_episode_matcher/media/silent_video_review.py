"""Bounded visual OCR review for an explicitly selected silent MKV."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from mkv_episode_matcher.media.special_feature_evidence import (
    SpecialFeatureEvidenceError,
    SpecialFeatureEvidenceItem,
    SpecialFeatureEvidencePlan,
    collect_special_feature_evidence,
)

_WARNING_TERMS = (
    "warning",
    "attention",
    "avertissement",
    "copyright",
    "fbi",
    "interpol",
    "piracy",
    "prohibited",
    "unauthorized",
    "all rights reserved",
    "disclaimer",
    "reproduction publique",
    "usage strictement familial",
    "utilisation strictement familiale",
    "advertencia",
    "derechos reservados",
    "reproduccion",
    "reproducción",
    "warnung",
    "urheberrecht",
    "attenzione",
    "riproduzione",
    "waarschuwing",
    "auteursrecht",
    "aviso legal",
)
_MENU_TERMS = (
    "play all",
    "episode selection",
    "scene selection",
    "special features",
    "language selection",
    "audio setup",
    "main menu",
)


@dataclass(frozen=True)
class SilentVideoReviewResult:
    category: str
    summary: str
    ocr_excerpt: str
    ocr_text_characters: int
    sampled_frame_count: int = 6


def resolve_tesseract_path(configured_path: Path | None) -> Path:
    """Resolve configured, PATH, or standard Windows Tesseract installs."""

    candidates: list[Path] = []
    if configured_path is not None:
        candidates.append(configured_path)
    discovered = shutil.which("tesseract.exe") or shutil.which("tesseract")
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(Path("C:/Program Files/Tesseract-OCR/tesseract.exe"))
    for candidate in candidates:
        if candidate.is_file() and candidate.name.casefold() in {
            "tesseract.exe",
            "tesseract",
        }:
            return candidate.resolve()
    raise SpecialFeatureEvidenceError(
        "Tesseract OCR was not found; detect installed tools in Settings"
    )


def classify_silent_video_text(text: str) -> tuple[str, str]:
    """Classify OCR text conservatively without making a deletion decision."""

    normalized = re.sub(r"\s+", " ", text).strip()
    folded = normalized.casefold()
    if any(term in folded for term in _WARNING_TERMS):
        return (
            "likely_warning_screen",
            "The sampled frames appear to contain a warning or rights notice. Review the video before deciding whether to delete it.",
        )
    if any(term in folded for term in _MENU_TERMS):
        return (
            "likely_disc_menu",
            "The sampled frames appear to contain disc-menu navigation. Review the video before deciding whether to delete it.",
        )
    if normalized:
        return (
            "text_detected",
            "Text was detected, but it was not confidently classified as a warning screen or disc menu.",
        )
    return (
        "no_text_detected",
        "No readable text was detected in the six sampled frames. Play the staged rip for visual review.",
    )


def collect_silent_video_review(
    *,
    media_id: str,
    media_path: Path,
    duration_seconds: float,
    output_root: Path,
    ffmpeg_path: Path,
    tesseract_path: Path,
) -> SilentVideoReviewResult:
    """Create six bounded frame samples and return a short private OCR review."""

    safe_id = f"silent-{hashlib.sha256(media_id.encode('utf-8')).hexdigest()[:16]}"
    plan = SpecialFeatureEvidencePlan(
        items=(
            SpecialFeatureEvidenceItem(
                media_id=safe_id,
                media_path=media_path,
                duration_seconds=duration_seconds,
                audio_stream_indexes=(),
            ),
        ),
        output_root=output_root,
        ffmpeg_path=ffmpeg_path,
        tesseract_path=tesseract_path,
    )
    result = collect_special_feature_evidence(plan, max_workers=1)
    item = result.items[0]
    if item.status != "collected":
        raise SpecialFeatureEvidenceError("Silent-video OCR collection failed safely")
    private_text_path = output_root / safe_id / "contact-sheet-ocr.txt"
    try:
        text = private_text_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecialFeatureEvidenceError(
            "Silent-video OCR text is unavailable"
        ) from exc
    excerpt = re.sub(r"\s+", " ", text).strip()[:600]
    category, summary = classify_silent_video_text(excerpt)
    return SilentVideoReviewResult(
        category=category,
        summary=summary,
        ocr_excerpt=excerpt,
        ocr_text_characters=item.ocr_text_characters,
    )
