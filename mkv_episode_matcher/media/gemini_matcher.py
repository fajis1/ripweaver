"""Catalog-constrained Gemini fallback for unmatched episode planning.

The adapter sends no local paths and performs no rename, move, or media access.
It accepts already-collected, redacted evidence and returns review candidates.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mkv_episode_matcher.core.credentials import (
    ApiCredentialError,
    ApiServiceError,
    CredentialName,
    request_credential_recovery,
)
from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)")


class GeminiMatchError(RuntimeError):
    """Raised when a Gemini match cannot be trusted or completed safely."""


class GeminiResponseError(GeminiMatchError):
    """Raised when a provider response does not satisfy the match schema."""


@dataclass(frozen=True)
class UnmatchedFileEvidence:
    file_id: str
    duration_seconds: float
    transcript_excerpts: tuple[str, ...]


@dataclass(frozen=True)
class GeminiSubtitleComparisonEvidence:
    candidate_episode_id: str
    whisper_excerpt: str
    subtitle_excerpt: str
    local_score: float


@dataclass(frozen=True)
class GeminiMatchResult:
    file_id: str
    episode_id: str | None
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class GeminiReviewPlan:
    mode: str
    model: str
    matches: tuple[GeminiMatchResult, ...]

    def safe_report(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "model": self.model,
            "matches": [asdict(match) for match in self.matches],
        }


@dataclass(frozen=True)
class GeminiDescriptiveResult:
    file_id: str
    content_kind: str
    suggested_title: str
    year: int | None
    confidence: float
    evidence: tuple[str, ...]
    summary: str = ""


@dataclass(frozen=True)
class GeminiDescriptivePlan:
    mode: str
    model: str
    matches: tuple[GeminiDescriptiveResult, ...]


@dataclass(frozen=True)
class GeminiRequestPlan:
    mode: str
    model: str
    file_ids: tuple[str, ...]
    candidate_episode_ids: tuple[str, ...]
    excerpt_counts: dict[str, int]

    def safe_report(self) -> dict[str, object]:
        return asdict(self)


class _MatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    episode_id: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(min_length=1, max_length=4)

    @field_validator("file_id")
    @classmethod
    def validate_file_id(cls, value: str) -> str:
        if _FILE_ID.fullmatch(value) is None:
            raise ValueError("invalid file ID")
        return value

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            normalized = " ".join(value.split())
            if not normalized or len(normalized) > 200:
                raise ValueError("evidence must contain 1-200 characters")
            if _WINDOWS_PATH.search(normalized):
                raise ValueError("evidence must not contain a local path")
            cleaned.append(normalized)
        return cleaned


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[_MatchModel]


class _DescriptiveMatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    content_kind: str = Field(pattern=r"^(movie|tv_episode|extra|menu|unknown)$")
    suggested_title: str = Field(min_length=1, max_length=160)
    year: int | None = Field(default=None, ge=1888, le=2200)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(min_length=1, max_length=4)
    summary: str = Field(min_length=1, max_length=320)

    @field_validator("file_id")
    @classmethod
    def validate_file_id(cls, value: str) -> str:
        if _FILE_ID.fullmatch(value) is None:
            raise ValueError("invalid file ID")
        return value

    @field_validator("suggested_title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip(" .")
        if not cleaned or _WINDOWS_PATH.search(cleaned):
            raise ValueError("suggested title is invalid")
        if any(character in '<>:"/\\|?*' for character in cleaned):
            raise ValueError("suggested title contains unsafe filename characters")
        return cleaned

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, values: list[str]) -> list[str]:
        return _MatchModel.validate_evidence(values)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or _WINDOWS_PATH.search(cleaned):
            raise ValueError("descriptive summary is invalid")
        return cleaned


class _DescriptiveResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[_DescriptiveMatchModel]


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    payload: dict
    raw_text: str | None = None


class GeminiTransport(Protocol):
    def send(
        self,
        *,
        api_key: str,
        body: dict,
        timeout_seconds: float,
    ) -> TransportResponse: ...


GeminiTransactionRecorder = Callable[[dict[str, object]], None]


class RequestsGeminiTransport:
    """Small REST transport that keeps the API key out of URLs and logs."""

    def send(
        self,
        *,
        api_key: str,
        body: dict,
        timeout_seconds: float,
    ) -> TransportResponse:
        response = requests.post(
            INTERACTIONS_URL,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            json=body,
            timeout=timeout_seconds,
        )
        try:
            decoded = response.json()
        except ValueError:
            payload = {}
        else:
            payload = decoded if isinstance(decoded, dict) else {}
        raw_text = response.text if isinstance(response.text, str) else None
        if raw_text is None:
            raw_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return TransportResponse(response.status_code, payload, raw_text)


Sleep = Callable[[float], None]


def _record_provider_transaction(
    recorder: GeminiTransactionRecorder | None,
    *,
    model: str,
    phase: str,
    attempt: int,
    body: dict,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    status_code: int | None,
    elapsed_ms: int,
    outcome: str,
    response: TransportResponse | None = None,
    error_type: str | None = None,
    diagnostic: str | None = None,
) -> None:
    """Emit one private provider transaction without affecting matching."""

    if recorder is None:
        return
    encoded_request = json.dumps(
        body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    raw_text = None
    if response is not None:
        raw_text = response.raw_text
        if raw_text is None:
            raw_text = json.dumps(
                response.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
    event: dict[str, object] = {
        "schema_version": 1,
        "model": model,
        "phase": phase,
        "attempt": attempt,
        "request_sha256": hashlib.sha256(encoded_request).hexdigest(),
        "file_ids": [item.file_id for item in files],
        "candidate_episode_ids": [item.episode_id for item in catalog],
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
        "outcome": outcome,
        "error_type": error_type,
        "diagnostic": diagnostic,
        "raw_response_text": raw_text,
    }
    try:
        recorder(event)
    except Exception as exc:  # Private diagnostics must never change a decision.
        logger.warning(
            "Gemini provider transaction was not persisted safely: {}",
            type(exc).__name__,
        )


def _validate_file_evidence(item: UnmatchedFileEvidence) -> None:
    if _FILE_ID.fullmatch(item.file_id) is None:
        raise GeminiMatchError("Unmatched evidence contains an invalid file ID")
    if item.duration_seconds <= 0:
        raise GeminiMatchError("Unmatched evidence duration must be positive")
    if len(item.transcript_excerpts) > 6:
        raise GeminiMatchError("Each file permits up to six excerpts")
    for excerpt in item.transcript_excerpts:
        if not excerpt.strip() or len(excerpt) > 600:
            raise GeminiMatchError("Transcript excerpts must contain 1-600 characters")
        if _WINDOWS_PATH.search(excerpt):
            raise GeminiMatchError("Transcript excerpts must not contain local paths")


def _safe_subtitle_comparisons(
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    comparisons: Mapping[str, tuple[GeminiSubtitleComparisonEvidence, ...]] | None,
) -> dict[str, list[dict[str, object]]]:
    if comparisons is None:
        return {}
    file_ids = {item.file_id for item in files}
    episode_ids = {item.episode_id for item in catalog}
    if set(comparisons) - file_ids:
        raise GeminiMatchError("Subtitle comparisons contain an unknown file ID")
    safe: dict[str, list[dict[str, object]]] = {}
    for file_id, pairs in comparisons.items():
        if len(pairs) > 6:
            raise GeminiMatchError("Each file permits up to six subtitle comparisons")
        serialized = []
        for pair in pairs:
            if pair.candidate_episode_id not in episode_ids:
                raise GeminiMatchError(
                    "Subtitle comparison episode is outside the catalogue"
                )
            whisper = " ".join(pair.whisper_excerpt.split())
            subtitle = " ".join(pair.subtitle_excerpt.split())
            if (
                not whisper
                or not subtitle
                or len(whisper) > 400
                or len(subtitle) > 400
                or _WINDOWS_PATH.search(whisper)
                or _WINDOWS_PATH.search(subtitle)
                or not 0 <= pair.local_score <= 1
            ):
                raise GeminiMatchError("Subtitle comparison evidence is unsafe")
            serialized.append({
                "candidate_episode_id": pair.candidate_episode_id,
                "whisper_excerpt": whisper,
                "subtitle_excerpt": subtitle,
                "local_score": round(pair.local_score, 6),
            })
        safe[file_id] = serialized
    return safe


def _validate_inputs(
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> None:
    if not files or not catalog:
        raise GeminiMatchError("Files and episode candidates are required")
    if len(files) > 50 or len(catalog) > 500:
        raise GeminiMatchError("Gemini candidate bundle exceeds safe limits")
    if len({item.file_id for item in files}) != len(files):
        raise GeminiMatchError("Unmatched file IDs must be unique")
    if len({item.episode_id for item in catalog}) != len(catalog):
        raise GeminiMatchError("Episode candidate IDs must be unique")
    for item in files:
        _validate_file_evidence(item)


def _safe_prior_attempts(  # noqa: C901 - strict bounded schema validation
    files: tuple[UnmatchedFileEvidence, ...],
    prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None,
) -> dict[str, list[dict[str, object]]]:
    """Validate dialogue-free classifier history before provider transmission."""

    if prior_attempts is None:
        return {}
    allowed_ids = {item.file_id for item in files}
    if set(prior_attempts) - allowed_ids:
        raise GeminiMatchError("Prior attempts contain an unknown file ID")
    safe: dict[str, list[dict[str, object]]] = {}
    for file_id, attempts in prior_attempts.items():
        if len(attempts) > 12:
            raise GeminiMatchError("Prior attempt history exceeds safe limits")
        cleaned = []
        for attempt in attempts:
            branch = attempt.get("branch")
            disposition = attempt.get("disposition")
            summary = attempt.get("summary")
            if (
                not isinstance(branch, str)
                or len(branch) > 40
                or not isinstance(disposition, str)
                or len(disposition) > 40
                or not isinstance(summary, dict)
            ):
                raise GeminiMatchError("Prior attempt history is invalid")
            safe_summary: dict[str, object] = {}
            for key, value in summary.items():
                if (
                    not isinstance(key, str)
                    or len(key) > 64
                    or isinstance(value, dict | list | tuple)
                    or not isinstance(value, str | int | float | bool | type(None))
                ):
                    raise GeminiMatchError("Prior attempt summary is invalid")
                if isinstance(value, str):
                    normalized = " ".join(value.split())
                    if len(normalized) > 240 or _WINDOWS_PATH.search(normalized):
                        raise GeminiMatchError("Prior attempt summary is unsafe")
                    value = normalized
                safe_summary[key] = value
            cleaned.append({
                "branch": branch,
                "disposition": disposition,
                "summary": safe_summary,
            })
        safe[file_id] = cleaned
    return safe


def _safe_reviewer_scene_descriptions(
    files: tuple[UnmatchedFileEvidence, ...],
    descriptions: Mapping[str, str] | None,
) -> dict[str, str]:
    """Validate explicit per-title review notes before provider transmission."""

    if descriptions is None:
        return {}
    allowed_ids = {item.file_id for item in files}
    if set(descriptions) - allowed_ids:
        raise GeminiMatchError("Reviewer scene descriptions contain an unknown file ID")
    safe: dict[str, str] = {}
    for file_id, description in descriptions.items():
        normalized = " ".join(description.split())
        if not 3 <= len(normalized) <= 1200 or _WINDOWS_PATH.search(normalized):
            raise GeminiMatchError("Reviewer scene description is unsafe")
        safe[file_id] = normalized
    return safe


def build_gemini_request(  # noqa: C901 - strict request validation
    model: str,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    *,
    prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
    existing_episode_ids: frozenset[str] | None = None,
    reviewer_scene_descriptions: Mapping[str, str] | None = None,
    subtitle_comparisons: Mapping[str, tuple[GeminiSubtitleComparisonEvidence, ...]]
    | None = None,
    review_phase: str = "initial",
    proposed_assignments: Mapping[str, str | None] | None = None,
) -> dict:
    """Build a path-free Interactions API request with a strict JSON schema."""

    _validate_inputs(files, catalog)
    if not model.strip() or len(model) > 100:
        raise GeminiMatchError("Gemini model name is invalid")
    safe_attempts = _safe_prior_attempts(files, prior_attempts)
    safe_scene_descriptions = _safe_reviewer_scene_descriptions(
        files, reviewer_scene_descriptions
    )
    safe_comparisons = _safe_subtitle_comparisons(files, catalog, subtitle_comparisons)
    if review_phase not in {"initial", "confirmation", "outside-season-review"}:
        raise GeminiMatchError("Gemini review phase is invalid")
    known_episode_ids = {item.episode_id for item in catalog}
    existing_episode_ids = existing_episode_ids or frozenset()
    if not existing_episode_ids.issubset(known_episode_ids):
        raise GeminiMatchError("Existing-library episodes are outside the catalogue")
    proposed_assignments = dict(proposed_assignments or {})
    if proposed_assignments:
        if review_phase != "confirmation" or set(proposed_assignments) != {
            item.file_id for item in files
        }:
            raise GeminiMatchError("Gemini proposed assignments are invalid")
        if any(
            episode_id is not None and episode_id not in known_episode_ids
            for episode_id in proposed_assignments.values()
        ):
            raise GeminiMatchError("Gemini proposal is outside the catalogue")
    evidence_payload = []
    for item in files:
        evidence_item = {
            "file_id": item.file_id,
            "duration_seconds": round(item.duration_seconds, 3),
            "transcript_excerpts": list(item.transcript_excerpts),
            "prior_attempts": safe_attempts.get(item.file_id, []),
        }
        if item.file_id in safe_scene_descriptions:
            evidence_item["reviewer_scene_description"] = safe_scene_descriptions[
                item.file_id
            ]
        if item.file_id in safe_comparisons:
            evidence_item["subtitle_comparisons"] = safe_comparisons[item.file_id]
        evidence_payload.append(evidence_item)
    catalog_payload = [
        {
            "episode_id": item.episode_id,
            "title": item.title,
            "overview": item.overview,
            "runtime_seconds": item.runtime_seconds,
            "library_status": (
                "present" if item.episode_id in existing_episode_ids else "missing"
            ),
        }
        for item in catalog
    ]
    runtime_only = any(not item.transcript_excerpts for item in files)
    prompt = {
        "task": (
            "Rank each file against only the supplied aired-order episode "
            "candidates. Use dialogue, names, plot, runtime, and disc-wide "
            "one-to-one consistency. Review the supplied prior-attempt summaries "
            "and correct, rather than repeat, their recorded failure modes. "
            "A reviewer_scene_description, when present, is an explicit human "
            "observation about that file. Use it as strong plot evidence while "
            "still checking it against the allowed episode titles and overviews. "
            "Library status is a tie-break hint only: never choose a missing "
            "episode over a present episode when dialogue, plot, or runtime "
            "supports the present episode. "
            "When subtitle_comparisons are supplied, decide whether each paired "
            "Whisper and subtitle passage describes the same scene despite ASR "
            "errors, inserted extended-cut dialogue, omissions, or shifted timing. "
            "Require corroboration from two independent scenes for a confident "
            "dialogue match. A weak or contradictory pair is not positive evidence. "
            + (
                "Some files have no usable dialogue. For those files make the best "
                "provisional one-to-one choice from runtime and the remaining names; "
                "do not use null merely because dialogue is absent. "
                if runtime_only
                else "Use null when evidence is insufficient. "
            )
            + "Never invent an episode ID or filename."
        ),
        "files": evidence_payload,
        "allowed_episodes": catalog_payload,
        "review_phase": review_phase,
    }
    if proposed_assignments:
        prompt["proposed_assignments"] = proposed_assignments
        prompt["confirmation_instruction"] = (
            "Audit the complete proposed one-to-one assignment map. Return the "
            "proposal only where the paired dialogue and disc-wide constraints "
            "still support it; otherwise return a corrected candidate or null."
        )
    schema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "minItems": len(files),
                "maxItems": len(files),
                "items": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "string",
                            "enum": [item.file_id for item in files],
                        },
                        "episode_id": {
                            "type": "string" if runtime_only else ["string", "null"],
                            "enum": [item.episode_id for item in catalog]
                            + ([] if runtime_only else [None]),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "file_id",
                        "episode_id",
                        "confidence",
                        "evidence",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["matches"],
        "additionalProperties": False,
    }
    return {
        "model": model.strip(),
        "input": json.dumps(prompt, ensure_ascii=True, separators=(",", ":")),
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": schema,
        },
    }


def gemini_request_digest(
    model: str,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    **kwargs,
) -> str:
    """Hash the exact path-free request so identical provider work is reusable."""

    body = build_gemini_request(model, files, catalog, **kwargs)
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_descriptive_gemini_request(
    model: str,
    files: tuple[UnmatchedFileEvidence, ...],
    *,
    release_hint: str,
    prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
) -> dict:
    """Build a path-free request for provisional catalogue-free classification."""

    if not files or len(files) > 20:
        raise GeminiMatchError("Descriptive Gemini evidence count is invalid")
    for item in files:
        _validate_file_evidence(item)
    hint = " ".join(release_hint.split())[:200]
    if not hint or _WINDOWS_PATH.search(hint):
        raise GeminiMatchError("Descriptive release hint is invalid")
    safe_attempts = _safe_prior_attempts(files, prior_attempts)
    prompt = {
        "task": (
            "Classify each optical-disc title as movie, tv_episode, extra, menu, "
            "or unknown. Produce a short filesystem-safe descriptive title using "
            "only the supplied release hint, runtime, dialogue evidence, bounded "
            "on-screen OCR text, and safe "
            "prior-attempt summaries. Use those summaries to arbitrate between "
            "content families instead of repeating a failed classification. "
            "Feature-length titles must not be labeled as extras merely because "
            "the disc also contains bonus material. Results are provisional; do "
            "not invent an exact episode number or claim certainty unsupported by "
            "the evidence. For extras, suggested_title must describe the actual "
            "subject and must never be a generic label such as Bonus Feature, "
            "Featurette, Extra, Special Feature, or Making Of Documentary. Provide "
            "a concise one- or two-sentence summary of what the evidence indicates "
            "the title contains. Titles for different files must be distinct."
        ),
        "release_hint": hint,
        "files": [
            {
                "file_id": item.file_id,
                "duration_seconds": round(item.duration_seconds, 3),
                "transcript_excerpts": list(item.transcript_excerpts),
                "prior_attempts": safe_attempts.get(item.file_id, []),
            }
            for item in files
        ],
    }
    schema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "minItems": len(files),
                "maxItems": len(files),
                "items": {
                    "type": "object",
                    "properties": {
                        "file_id": {
                            "type": "string",
                            "enum": [item.file_id for item in files],
                        },
                        "content_kind": {
                            "type": "string",
                            "enum": ["movie", "tv_episode", "extra", "menu", "unknown"],
                        },
                        "suggested_title": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                        },
                        "year": {"type": ["integer", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {"type": "string", "maxLength": 200},
                        },
                        "summary": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 320,
                        },
                    },
                    "required": [
                        "file_id",
                        "content_kind",
                        "suggested_title",
                        "year",
                        "confidence",
                        "evidence",
                        "summary",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["matches"],
        "additionalProperties": False,
    }
    return {
        "model": model.strip(),
        "input": json.dumps(prompt, ensure_ascii=True, separators=(",", ":")),
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": schema,
        },
    }


def plan_gemini_request(
    model: str,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> GeminiRequestPlan:
    """Validate a future request and return a dialogue-free preview."""

    build_gemini_request(model, files, catalog)
    return GeminiRequestPlan(
        mode="gemini-unmatched-request-plan",
        model=model.strip(),
        file_ids=tuple(sorted(item.file_id for item in files)),
        candidate_episode_ids=tuple(sorted(item.episode_id for item in catalog)),
        excerpt_counts={
            item.file_id: len(item.transcript_excerpts)
            for item in sorted(files, key=lambda value: value.file_id)
        },
    )


def load_gemini_bundle(
    path: Path,
) -> tuple[
    tuple[UnmatchedFileEvidence, ...],
    tuple[EpisodeCatalogEntry, ...],
]:
    """Load a transient evidence/catalogue bundle without retaining its path."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_files = data["files"]
        raw_catalog = data["episodes"]
        if not isinstance(raw_files, list) or not isinstance(raw_catalog, list):
            raise TypeError
        files = tuple(
            UnmatchedFileEvidence(
                file_id=str(item["file_id"]),
                duration_seconds=float(item["duration_seconds"]),
                transcript_excerpts=tuple(
                    str(excerpt) for excerpt in item["transcript_excerpts"]
                ),
            )
            for item in raw_files
        )
        catalog = tuple(
            EpisodeCatalogEntry(
                episode_id=str(item["episode_id"]),
                season=int(item["season"]),
                episode=int(item["episode"]),
                title=str(item["title"]),
                overview=str(item.get("overview", "")),
                runtime_seconds=(
                    float(item["runtime_seconds"])
                    if item.get("runtime_seconds") is not None
                    else None
                ),
            )
            for item in raw_catalog
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise GeminiMatchError("Gemini evidence bundle is invalid") from exc
    _validate_inputs(files, catalog)
    return files, catalog


def write_safe_request_plan(path: Path, plan: GeminiRequestPlan) -> Path:
    """Write a path- and dialogue-free request preview without overwriting."""

    if path.exists():
        raise GeminiMatchError("Gemini request plan exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.safe_report(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _output_text(payload: dict) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            content = step.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                ):
                    return item["text"]
    raise GeminiResponseError("Gemini response contained no structured text")


def _parse_and_validate_response(
    payload: dict,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> tuple[GeminiMatchResult, ...]:
    try:
        parsed = _ResponseModel.model_validate_json(_output_text(payload))
    except (ValidationError, ValueError) as exc:
        raise GeminiResponseError("Gemini returned invalid structured output") from exc

    allowed_files = {item.file_id for item in files}
    allowed_episodes = {item.episode_id for item in catalog}
    returned_files = [item.file_id for item in parsed.matches]
    if set(returned_files) != allowed_files or len(returned_files) != len(
        allowed_files
    ):
        raise GeminiResponseError(
            "Gemini response did not cover each supplied file once"
        )

    assigned: set[str] = set()
    results: list[GeminiMatchResult] = []
    for item in parsed.matches:
        if item.episode_id is not None:
            if item.episode_id not in allowed_episodes:
                raise GeminiResponseError(
                    "Gemini returned an episode outside the catalogue"
                )
            if item.episode_id in assigned:
                raise GeminiResponseError("Gemini assigned one episode more than once")
            assigned.add(item.episode_id)
        results.append(
            GeminiMatchResult(
                file_id=item.file_id,
                episode_id=item.episode_id,
                confidence=item.confidence,
                evidence=tuple(item.evidence),
            )
        )
    results.sort(key=lambda item: item.file_id)
    return tuple(results)


def _parse_provider_response(
    payload: dict,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> tuple[GeminiMatchResult, ...]:
    try:
        return _parse_and_validate_response(payload, files, catalog)
    except GeminiMatchError as exc:
        raise GeminiResponseError(str(exc)) from exc


def _validation_retry_body(body: dict, exc: GeminiResponseError, attempt: int) -> dict:
    prompt = json.loads(body["input"])
    prompt["validation_retry"] = {
        "attempt": attempt + 1,
        "instruction": (
            f"The previous response was rejected: {exc}. Return every supplied "
            "file exactly once, use only allowed episode IDs, and assign each "
            "episode at most once."
        ),
    }
    revised = dict(body)
    revised["input"] = json.dumps(prompt, ensure_ascii=True, separators=(",", ":"))
    return revised


def _parse_descriptive_response(
    payload: dict,
    files: tuple[UnmatchedFileEvidence, ...],
) -> tuple[GeminiDescriptiveResult, ...]:
    try:
        parsed = _DescriptiveResponseModel.model_validate_json(_output_text(payload))
    except (ValidationError, ValueError) as exc:
        raise GeminiResponseError("Gemini returned invalid descriptive output") from exc
    expected = {item.file_id for item in files}
    returned = [item.file_id for item in parsed.matches]
    if set(returned) != expected or len(returned) != len(expected):
        raise GeminiResponseError(
            "Gemini descriptive response did not cover each supplied file once"
        )
    generic_extra_titles = {
        "bonus content",
        "bonus feature",
        "extra",
        "featurette",
        "making of documentary",
        "special feature",
    }
    descriptive_titles = [
        item.suggested_title.casefold()
        for item in parsed.matches
        if item.content_kind == "extra"
    ]
    if any(title in generic_extra_titles for title in descriptive_titles):
        raise GeminiResponseError("Gemini returned a generic extra title")
    if len(descriptive_titles) != len(set(descriptive_titles)):
        raise GeminiResponseError("Gemini returned duplicate extra titles")
    return tuple(
        sorted(
            (
                GeminiDescriptiveResult(
                    file_id=item.file_id,
                    content_kind=item.content_kind,
                    suggested_title=item.suggested_title,
                    year=item.year,
                    confidence=item.confidence,
                    evidence=tuple(item.evidence),
                    summary=item.summary,
                )
                for item in parsed.matches
            ),
            key=lambda item: item.file_id,
        )
    )


class GeminiEpisodeRanker:
    """Bounded Gemini client returning a review plan only."""

    def __init__(
        self,
        *,
        model: str,
        transport: GeminiTransport | None = None,
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
        sleep: Sleep = time.sleep,
    ):
        if timeout_seconds <= 0 or max_retries < 0:
            raise ValueError("Gemini timeout/retry settings are invalid")
        self.model = model
        self.transport = transport or RequestsGeminiTransport()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.sleep = sleep

    def rank_with_key(  # noqa: C901 - bounded provider and validation retries
        self,
        files: tuple[UnmatchedFileEvidence, ...],
        catalog: tuple[EpisodeCatalogEntry, ...],
        *,
        api_key: str,
        credential: CredentialName,
        prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
        existing_episode_ids: frozenset[str] | None = None,
        reviewer_scene_descriptions: Mapping[str, str] | None = None,
        subtitle_comparisons: Mapping[str, tuple[GeminiSubtitleComparisonEvidence, ...]]
        | None = None,
        review_phase: str = "initial",
        proposed_assignments: Mapping[str, str | None] | None = None,
        transaction_recorder: GeminiTransactionRecorder | None = None,
    ) -> GeminiReviewPlan:
        if credential not in ("gemini-primary", "gemini-paid"):
            raise ValueError("Gemini credential name is invalid")
        if not api_key:
            raise ApiCredentialError(credential, "not configured")
        body = build_gemini_request(
            self.model,
            files,
            catalog,
            prior_attempts=prior_attempts,
            existing_episode_ids=existing_episode_ids,
            reviewer_scene_descriptions=reviewer_scene_descriptions,
            subtitle_comparisons=subtitle_comparisons,
            review_phase=review_phase,
            proposed_assignments=proposed_assignments,
        )
        for attempt in range(self.max_retries + 1):
            request_started = time.perf_counter()
            try:
                response = self.transport.send(
                    api_key=api_key,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                )
            except (requests.RequestException, ConnectionError, TimeoutError) as exc:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase=review_phase,
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=catalog,
                    status_code=None,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="network_failure",
                    error_type=type(exc).__name__,
                    diagnostic="provider request did not return a response",
                )
                if attempt >= self.max_retries:
                    raise ApiServiceError(
                        "Gemini", None, f"network failure: {type(exc).__name__}"
                    ) from exc
                self.sleep(2**attempt)
                continue

            if response.status_code in (401, 403):
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase=review_phase,
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=catalog,
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="credential_rejected",
                    response=response,
                    diagnostic="provider rejected the configured credential",
                )
                raise ApiCredentialError(
                    credential,
                    "rejected by the provider",
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase=review_phase,
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=catalog,
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome=(
                        "rate_limited"
                        if response.status_code == 429
                        else "provider_unavailable"
                    ),
                    response=response,
                    diagnostic="provider requested a bounded retry",
                )
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                reason = (
                    "rate or quota limit reached; retry later"
                    if response.status_code == 429
                    else "provider is temporarily unavailable"
                )
                raise ApiServiceError("Gemini", response.status_code, reason)
            if response.status_code >= 400:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase=review_phase,
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=catalog,
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="request_rejected",
                    response=response,
                    diagnostic="provider did not accept the request",
                )
                raise ApiServiceError(
                    "Gemini",
                    response.status_code,
                    "request was not accepted",
                )
            try:
                matches = _parse_provider_response(response.payload, files, catalog)
            except GeminiResponseError as exc:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase=review_phase,
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=catalog,
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="response_invalid",
                    response=response,
                    error_type=type(exc).__name__,
                    diagnostic=str(exc),
                )
                if attempt >= self.max_retries:
                    raise
                body = _validation_retry_body(body, exc, attempt)
                continue
            _record_provider_transaction(
                transaction_recorder,
                model=self.model,
                phase=review_phase,
                attempt=attempt + 1,
                body=body,
                files=files,
                catalog=catalog,
                status_code=response.status_code,
                elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                outcome="accepted",
                response=response,
            )
            return GeminiReviewPlan(
                mode="gemini-unmatched-review-plan",
                model=self.model,
                matches=matches,
            )
        raise ApiServiceError("Gemini", None, "retry loop ended unexpectedly")

    def rank_with_configured_keys(
        self,
        files: tuple[UnmatchedFileEvidence, ...],
        catalog: tuple[EpisodeCatalogEntry, ...],
        *,
        prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
        existing_episode_ids: frozenset[str] | None = None,
        reviewer_scene_descriptions: Mapping[str, str] | None = None,
        subtitle_comparisons: Mapping[str, tuple[GeminiSubtitleComparisonEvidence, ...]]
        | None = None,
        review_phase: str = "initial",
        proposed_assignments: Mapping[str, str | None] | None = None,
        transaction_recorder: GeminiTransactionRecorder | None = None,
    ) -> GeminiReviewPlan:
        """Try primary then paid key, with one interactive auth recovery each."""

        provider_errors: list[ApiServiceError | GeminiResponseError] = []
        for credential, field in (
            ("gemini-primary", "gemini_primary_api_key"),
            ("gemini-paid", "gemini_paid_api_key"),
        ):
            settings = load_environment_settings()
            api_key = getattr(settings, field)
            if not api_key:
                continue
            for auth_attempt in range(2):
                try:
                    return self.rank_with_key(
                        files,
                        catalog,
                        api_key=api_key,
                        credential=credential,
                        prior_attempts=prior_attempts,
                        existing_episode_ids=existing_episode_ids,
                        reviewer_scene_descriptions=reviewer_scene_descriptions,
                        subtitle_comparisons=subtitle_comparisons,
                        review_phase=review_phase,
                        proposed_assignments=proposed_assignments,
                        transaction_recorder=transaction_recorder,
                    )
                except ApiCredentialError as exc:
                    if auth_attempt == 0 and request_credential_recovery(exc):
                        settings = load_environment_settings()
                        api_key = getattr(settings, field)
                        if api_key:
                            continue
                    break
                except ApiServiceError as exc:
                    provider_errors.append(exc)
                    break
                except GeminiResponseError as exc:
                    provider_errors.append(exc)
                    break
        if provider_errors:
            raise provider_errors[-1]
        raise ApiCredentialError("gemini-primary", "not configured or accepted")


class GeminiDescriptiveRanker(GeminiEpisodeRanker):
    """Bounded catalogue-free classifier producing provisional safe names."""

    def describe_with_key(  # noqa: C901 - bounded provider status handling
        self,
        files: tuple[UnmatchedFileEvidence, ...],
        *,
        release_hint: str,
        api_key: str,
        credential: CredentialName,
        prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
        transaction_recorder: GeminiTransactionRecorder | None = None,
    ) -> GeminiDescriptivePlan:
        if credential not in ("gemini-primary", "gemini-paid"):
            raise ValueError("Gemini credential name is invalid")
        if not api_key:
            raise ApiCredentialError(credential, "not configured")
        body = build_descriptive_gemini_request(
            self.model,
            files,
            release_hint=release_hint,
            prior_attempts=prior_attempts,
        )
        for attempt in range(self.max_retries + 1):
            request_started = time.perf_counter()
            try:
                response = self.transport.send(
                    api_key=api_key,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                )
            except (requests.RequestException, ConnectionError, TimeoutError) as exc:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase="descriptive",
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=(),
                    status_code=None,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="network_failure",
                    error_type=type(exc).__name__,
                    diagnostic="provider request did not return a response",
                )
                if attempt >= self.max_retries:
                    raise ApiServiceError(
                        "Gemini", None, f"network failure: {type(exc).__name__}"
                    ) from exc
                self.sleep(2**attempt)
                continue
            if response.status_code in (401, 403):
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase="descriptive",
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=(),
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="credential_rejected",
                    response=response,
                    diagnostic="provider rejected the configured credential",
                )
                raise ApiCredentialError(
                    credential,
                    "rejected by the provider",
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase="descriptive",
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=(),
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome=(
                        "rate_limited"
                        if response.status_code == 429
                        else "provider_unavailable"
                    ),
                    response=response,
                    diagnostic="provider requested a bounded retry",
                )
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                reason = (
                    "rate or quota limit reached; retry later"
                    if response.status_code == 429
                    else "provider is temporarily unavailable"
                )
                raise ApiServiceError("Gemini", response.status_code, reason)
            if response.status_code >= 400:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase="descriptive",
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=(),
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="request_rejected",
                    response=response,
                    diagnostic="provider did not accept the request",
                )
                raise ApiServiceError(
                    "Gemini", response.status_code, "request was not accepted"
                )
            try:
                matches = _parse_descriptive_response(response.payload, files)
            except GeminiResponseError as exc:
                _record_provider_transaction(
                    transaction_recorder,
                    model=self.model,
                    phase="descriptive",
                    attempt=attempt + 1,
                    body=body,
                    files=files,
                    catalog=(),
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                    outcome="response_invalid",
                    response=response,
                    error_type=type(exc).__name__,
                    diagnostic=str(exc),
                )
                raise
            _record_provider_transaction(
                transaction_recorder,
                model=self.model,
                phase="descriptive",
                attempt=attempt + 1,
                body=body,
                files=files,
                catalog=(),
                status_code=response.status_code,
                elapsed_ms=round((time.perf_counter() - request_started) * 1000),
                outcome="accepted",
                response=response,
            )
            return GeminiDescriptivePlan(
                mode="gemini-descriptive-review-plan",
                model=self.model,
                matches=matches,
            )
        raise ApiServiceError("Gemini", None, "retry loop ended unexpectedly")

    def describe_with_configured_keys(
        self,
        files: tuple[UnmatchedFileEvidence, ...],
        *,
        release_hint: str,
        prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
        transaction_recorder: GeminiTransactionRecorder | None = None,
    ) -> GeminiDescriptivePlan:
        provider_errors: list[ApiServiceError | GeminiResponseError] = []
        for credential, field in (
            ("gemini-primary", "gemini_primary_api_key"),
            ("gemini-paid", "gemini_paid_api_key"),
        ):
            settings = load_environment_settings()
            api_key = getattr(settings, field)
            if not api_key:
                continue
            try:
                return self.describe_with_key(
                    files,
                    release_hint=release_hint,
                    api_key=api_key,
                    credential=credential,
                    prior_attempts=prior_attempts,
                    transaction_recorder=transaction_recorder,
                )
            except ApiCredentialError:
                continue
            except (ApiServiceError, GeminiResponseError) as exc:
                provider_errors.append(exc)
        if provider_errors:
            raise provider_errors[-1]
        raise ApiCredentialError("gemini-primary", "not configured or accepted")
