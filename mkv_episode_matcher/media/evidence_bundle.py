"""Build transient unmatched bundles from explicit saved evidence.

This module performs no media discovery, extraction, transcription, provider
request, or media mutation. Transcript text exists only in the explicit input
and transient bundle; the companion plan is dialogue-free.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from mkv_episode_matcher.media.episode_catalog import (
    CatalogCandidateScore,
    EpisodeCatalogEntry,
    EpisodeCatalogError,
    rank_catalog_candidates,
    validate_catalog,
)

_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)")
_WORDS = re.compile(r"[a-z0-9']+", re.IGNORECASE)


class EvidenceBundleError(RuntimeError):
    """Raised when saved evidence cannot form a safe transient bundle."""


@dataclass(frozen=True)
class SavedTranscriptWindow:
    start_seconds: float
    text: str


@dataclass(frozen=True)
class SavedFileEvidence:
    file_id: str
    duration_seconds: float
    windows: tuple[SavedTranscriptWindow, ...]


@dataclass(frozen=True)
class LocalCandidatePlan:
    episode_id: str
    title_score: float
    overview_score: float
    runtime_score: float
    combined_score: float


@dataclass(frozen=True)
class EvidenceFilePlan:
    file_id: str
    duration_seconds: float
    input_window_count: int
    selected_excerpt_count: int
    candidates: tuple[LocalCandidatePlan, ...]


@dataclass(frozen=True)
class EvidenceBundlePlan:
    mode: str
    file_count: int
    catalog_episode_count: int
    shortlisted_episode_count: int
    files: tuple[EvidenceFilePlan, ...]

    def safe_report(self) -> dict[str, object]:
        return asdict(self)


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _parse_window(item: object) -> SavedTranscriptWindow:
    if not isinstance(item, dict):
        raise EvidenceBundleError("Transcript windows must be JSON objects")
    try:
        start_seconds = float(item["start_seconds"])
        text = _clean_text(str(item["text"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceBundleError("Transcript window fields are invalid") from exc
    if start_seconds < 0:
        raise EvidenceBundleError("Transcript window start must not be negative")
    if not text or len(text) > 4000:
        raise EvidenceBundleError(
            "Transcript window text must contain 1-4000 characters"
        )
    if _WINDOWS_PATH.search(text):
        raise EvidenceBundleError("Transcript text must not contain a local path")
    return SavedTranscriptWindow(start_seconds, text)


def load_saved_transcript_evidence(
    path: Path,
    *,
    skip_review_files: bool = False,
) -> tuple[SavedFileEvidence, ...]:
    """Load explicit multi-window transcript JSON without retaining its path."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_files = payload["files"]
        if not isinstance(raw_files, list):
            raise TypeError
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EvidenceBundleError("Saved transcript evidence is invalid") from exc

    files: list[SavedFileEvidence] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise EvidenceBundleError("Saved file evidence must be a JSON object")
        try:
            file_id = str(item["file_id"])
            duration_seconds = float(item["duration_seconds"])
            raw_windows = item["windows"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceBundleError("Saved file evidence fields are invalid") from exc
        if _FILE_ID.fullmatch(file_id) is None:
            raise EvidenceBundleError("Saved evidence contains an invalid file ID")
        if duration_seconds <= 0:
            raise EvidenceBundleError("Saved evidence duration must be positive")
        if item.get("status", "collected") != "collected":
            if skip_review_files:
                continue
            raise EvidenceBundleError(
                "Saved transcript evidence contains a file requiring audio review"
            )
        if not isinstance(raw_windows, list) or not raw_windows:
            raise EvidenceBundleError("Each file requires saved transcript windows")
        windows = tuple(_parse_window(window) for window in raw_windows)
        files.append(SavedFileEvidence(file_id, duration_seconds, windows))

    if not files and not skip_review_files:
        raise EvidenceBundleError("Saved transcript evidence contains no files")
    if len(files) > 50:
        raise EvidenceBundleError("Saved transcript evidence exceeds 50 files")
    if len({item.file_id for item in files}) != len(files):
        raise EvidenceBundleError("Saved transcript file IDs must be unique")
    return tuple(files)


def _apply_file_id_map(
    files: list[SavedFileEvidence],
    file_id_map: dict[str, str],
) -> list[SavedFileEvidence]:
    for source_id, target_id in file_id_map.items():
        if (
            _FILE_ID.fullmatch(source_id) is None
            or _FILE_ID.fullmatch(target_id) is None
        ):
            raise EvidenceBundleError("Transcript file-ID mapping is invalid")
    present_ids = {item.file_id for item in files}
    if set(file_id_map) - present_ids:
        raise EvidenceBundleError(
            "Transcript file-ID mapping contains an absent source ID"
        )
    return [
        SavedFileEvidence(
            file_id_map.get(item.file_id, item.file_id),
            item.duration_seconds,
            item.windows,
        )
        for item in files
    ]


def merge_saved_transcript_evidence(
    paths: tuple[Path, ...],
    *,
    file_id_prefix: str | None = None,
    enrich_duplicates: bool = False,
    skip_review_files: bool = False,
    file_id_map: dict[str, str] | None = None,
) -> tuple[SavedFileEvidence, ...]:
    """Merge explicit private reports without logging paths or dialogue."""

    if not paths:
        raise EvidenceBundleError("At least one saved transcript report is required")
    merged: list[SavedFileEvidence] = []
    for path in paths:
        merged.extend(
            load_saved_transcript_evidence(
                path,
                skip_review_files=skip_review_files,
            )
        )
    if file_id_map:
        merged = _apply_file_id_map(merged, file_id_map)
    if file_id_prefix is not None:
        if _FILE_ID.fullmatch(file_id_prefix.rstrip("-")) is None:
            raise EvidenceBundleError("Transcript file-ID prefix is invalid")
        merged = [item for item in merged if item.file_id.startswith(file_id_prefix)]
    if not merged:
        raise EvidenceBundleError("No saved transcript files matched the merge")
    if enrich_duplicates:
        enriched: dict[str, SavedFileEvidence] = {}
        for item in merged:
            previous = enriched.get(item.file_id)
            if previous is None:
                enriched[item.file_id] = item
                continue
            if abs(previous.duration_seconds - item.duration_seconds) > 1:
                raise EvidenceBundleError(
                    "Duplicate transcript IDs have conflicting durations"
                )
            windows = {
                (window.start_seconds, window.text): window
                for window in (*previous.windows, *item.windows)
            }
            enriched[item.file_id] = SavedFileEvidence(
                item.file_id,
                previous.duration_seconds,
                tuple(
                    sorted(
                        windows.values(),
                        key=lambda window: window.start_seconds,
                    )
                ),
            )
        merged = list(enriched.values())
    elif len({item.file_id for item in merged}) != len(merged):
        raise EvidenceBundleError("Merged transcript file IDs must be unique")
    if len(merged) > 50:
        raise EvidenceBundleError("Merged transcript evidence exceeds 50 files")
    return tuple(merged)


def write_merged_transcript_evidence(
    path: Path,
    files: tuple[SavedFileEvidence, ...],
) -> Path:
    """Write a new private report in the collector's saved-evidence schema."""

    payload = {
        "mode": "saved-transcript-evidence",
        "files": [
            {
                "file_id": item.file_id,
                "duration_seconds": item.duration_seconds,
                "status": "collected",
                "windows": [
                    {
                        "start_seconds": window.start_seconds,
                        "text": window.text,
                    }
                    for window in item.windows
                ],
            }
            for item in files
        ],
    }
    return _write_new_json(path, payload, label="Merged transcript report")


def _catalog_entry(item: object) -> EpisodeCatalogEntry:
    if not isinstance(item, dict):
        raise EvidenceBundleError("Episode catalogue entries must be JSON objects")
    try:
        runtime = item.get("runtime_seconds")
        return EpisodeCatalogEntry(
            episode_id=str(item["episode_id"]),
            season=int(item["season"]),
            episode=int(item["episode"]),
            title=str(item["title"]),
            overview=str(item.get("overview", "")),
            runtime_seconds=float(runtime) if runtime is not None else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceBundleError("Episode catalogue fields are invalid") from exc


def load_episode_catalog(path: Path) -> tuple[EpisodeCatalogEntry, ...]:
    """Load an explicit authoritative episode catalogue JSON report."""

    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        raw_episodes = payload["episodes"]
        if not isinstance(raw_episodes, list):
            raise TypeError
        entries = [_catalog_entry(item) for item in raw_episodes]
        catalog = validate_catalog(entries)
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        EpisodeCatalogError,
    ) as exc:
        raise EvidenceBundleError("Episode catalogue report is invalid") from exc
    if not catalog or len(catalog) > 500:
        raise EvidenceBundleError("Episode catalogue must contain 1-500 episodes")
    return catalog


def _excerpt_quality(text: str) -> float:
    words = _WORDS.findall(text.lower())
    if not words:
        return 0.0
    unique_ratio = len(set(words)) / len(words)
    length_score = min(len(words), 80) / 80
    return 0.65 * length_score + 0.35 * unique_ratio


def _trim_excerpt(text: str, maximum_characters: int = 600) -> str:
    if len(text) <= maximum_characters:
        return text
    shortened = text[: maximum_characters + 1]
    boundary = shortened.rfind(" ")
    if boundary >= maximum_characters // 2:
        shortened = shortened[:boundary]
    return shortened[:maximum_characters].rstrip()


def select_transcript_excerpts(
    windows: tuple[SavedTranscriptWindow, ...],
    *,
    maximum_excerpts: int = 3,
) -> tuple[str, ...]:
    """Select informative, non-duplicate excerpts and restore time order."""

    if not 1 <= maximum_excerpts <= 3:
        raise ValueError("maximum_excerpts must be between one and three")
    ranked = sorted(
        windows,
        key=lambda item: (_excerpt_quality(item.text), -item.start_seconds),
        reverse=True,
    )
    selected: list[SavedTranscriptWindow] = []
    for window in ranked:
        excerpt = _trim_excerpt(window.text)
        if not excerpt:
            continue
        if any(
            fuzz.ratio(excerpt.lower(), _trim_excerpt(item.text).lower()) >= 90
            for item in selected
        ):
            continue
        selected.append(SavedTranscriptWindow(window.start_seconds, excerpt))
        if len(selected) == maximum_excerpts:
            break
    if not selected:
        raise EvidenceBundleError("Saved windows contain no usable transcript text")
    selected.sort(key=lambda item: item.start_seconds)
    return tuple(item.text for item in selected)


def _safe_candidate(candidate: CatalogCandidateScore) -> LocalCandidatePlan:
    return LocalCandidatePlan(
        episode_id=candidate.episode_id,
        title_score=round(candidate.title_score, 6),
        overview_score=round(candidate.overview_score, 6),
        runtime_score=round(candidate.runtime_score, 6),
        combined_score=round(candidate.combined_score, 6),
    )


def build_transient_evidence_bundle(
    files: tuple[SavedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    *,
    top_k: int = 10,
) -> tuple[dict[str, object], EvidenceBundlePlan]:
    """Join saved evidence to a local shortlist and return bundle plus safe plan."""

    if not files or not catalog:
        raise EvidenceBundleError("Saved evidence and episode catalogue are required")
    if not 1 <= top_k <= 50:
        raise EvidenceBundleError("Local shortlist size must be between 1 and 50")

    ranking_depth = min(len(catalog), max(top_k, len(files)))
    transient_files: list[dict[str, object]] = []
    file_plans: list[EvidenceFilePlan] = []
    shortlisted_ids: set[str] = set()
    for item in files:
        excerpts = select_transcript_excerpts(item.windows)
        candidates = rank_catalog_candidates(
            excerpts,
            item.duration_seconds,
            catalog,
            top_k=ranking_depth,
        )
        if not candidates:
            raise EvidenceBundleError("Local catalogue ranking produced no candidates")
        local_top = candidates[:top_k]
        shortlisted_ids.update(candidate.episode_id for candidate in candidates)
        transient_files.append({
            "file_id": item.file_id,
            "duration_seconds": item.duration_seconds,
            "transcript_excerpts": list(excerpts),
            "local_candidate_episode_ids": [
                candidate.episode_id for candidate in local_top
            ],
        })
        file_plans.append(
            EvidenceFilePlan(
                file_id=item.file_id,
                duration_seconds=item.duration_seconds,
                input_window_count=len(item.windows),
                selected_excerpt_count=len(excerpts),
                candidates=tuple(_safe_candidate(value) for value in local_top),
            )
        )

    shortlisted_catalog = tuple(
        entry for entry in catalog if entry.episode_id in shortlisted_ids
    )
    bundle: dict[str, object] = {
        "mode": "transient-unmatched-evidence",
        "files": transient_files,
        "episodes": [entry.to_dict() for entry in shortlisted_catalog],
    }
    plan = EvidenceBundlePlan(
        mode="unmatched-evidence-bundle-plan",
        file_count=len(files),
        catalog_episode_count=len(catalog),
        shortlisted_episode_count=len(shortlisted_catalog),
        files=tuple(file_plans),
    )
    return bundle, plan


def _write_new_json(path: Path, payload: dict[str, object], *, label: str) -> Path:
    if path.exists():
        raise EvidenceBundleError(f"{label} exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def validate_new_output_paths(bundle_path: Path, report_path: Path) -> None:
    """Refuse ambiguous or colliding outputs before either file is written."""

    if bundle_path.resolve() == report_path.resolve():
        raise EvidenceBundleError("Bundle and safe report paths must be different")
    if bundle_path.exists():
        raise EvidenceBundleError(
            "Transient evidence bundle exists; refusing overwrite"
        )
    if report_path.exists():
        raise EvidenceBundleError("Evidence plan exists; refusing overwrite")


def write_transient_bundle(path: Path, bundle: dict[str, object]) -> Path:
    """Write the explicitly requested dialogue-bearing transient bundle."""

    return _write_new_json(path, bundle, label="Transient evidence bundle")


def write_safe_evidence_plan(path: Path, plan: EvidenceBundlePlan) -> Path:
    """Write a dialogue- and path-free local ranking report."""

    return _write_new_json(path, plan.safe_report(), label="Evidence plan")
