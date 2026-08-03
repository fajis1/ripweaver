"""Catalog-constrained Gemini fallback for unmatched episode planning.

The adapter sends no local paths and performs no rename, move, or media access.
It accepts already-collected, redacted evidence and returns review candidates.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import requests
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


class _DescriptiveResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[_DescriptiveMatchModel]


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    payload: dict


class GeminiTransport(Protocol):
    def send(
        self,
        *,
        api_key: str,
        body: dict,
        timeout_seconds: float,
    ) -> TransportResponse: ...


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
            payload = response.json()
        except ValueError:
            payload = {}
        return TransportResponse(response.status_code, payload)


Sleep = Callable[[float], None]


def _validate_file_evidence(item: UnmatchedFileEvidence) -> None:
    if _FILE_ID.fullmatch(item.file_id) is None:
        raise GeminiMatchError("Unmatched evidence contains an invalid file ID")
    if item.duration_seconds <= 0:
        raise GeminiMatchError("Unmatched evidence duration must be positive")
    if len(item.transcript_excerpts) > 3:
        raise GeminiMatchError("Each file permits up to three excerpts")
    for excerpt in item.transcript_excerpts:
        if not excerpt.strip() or len(excerpt) > 600:
            raise GeminiMatchError("Transcript excerpts must contain 1-600 characters")
        if _WINDOWS_PATH.search(excerpt):
            raise GeminiMatchError("Transcript excerpts must not contain local paths")


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


def build_gemini_request(
    model: str,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    *,
    prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
    existing_episode_ids: frozenset[str] | None = None,
) -> dict:
    """Build a path-free Interactions API request with a strict JSON schema."""

    _validate_inputs(files, catalog)
    if not model.strip() or len(model) > 100:
        raise GeminiMatchError("Gemini model name is invalid")
    safe_attempts = _safe_prior_attempts(files, prior_attempts)
    known_episode_ids = {item.episode_id for item in catalog}
    existing_episode_ids = existing_episode_ids or frozenset()
    if not existing_episode_ids.issubset(known_episode_ids):
        raise GeminiMatchError("Existing-library episodes are outside the catalogue")
    evidence_payload = [
        {
            "file_id": item.file_id,
            "duration_seconds": round(item.duration_seconds, 3),
            "transcript_excerpts": list(item.transcript_excerpts),
            "prior_attempts": safe_attempts.get(item.file_id, []),
        }
        for item in files
    ]
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
            "Library status is a tie-break hint only: never choose a missing "
            "episode over a present episode when dialogue, plot, or runtime "
            "supports the present episode. "
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
            "only the supplied release hint, runtime, dialogue evidence, and safe "
            "prior-attempt summaries. Use those summaries to arbitrate between "
            "content families instead of repeating a failed classification. "
            "Feature-length titles must not be labeled as extras merely because "
            "the disc also contains bonus material. Results are provisional; do "
            "not invent an exact episode number or claim certainty unsupported by "
            "the evidence."
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
                    },
                    "required": [
                        "file_id",
                        "content_kind",
                        "suggested_title",
                        "year",
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
    raise GeminiMatchError("Gemini response contained no structured text")


def _parse_and_validate_response(
    payload: dict,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> tuple[GeminiMatchResult, ...]:
    try:
        parsed = _ResponseModel.model_validate_json(_output_text(payload))
    except (ValidationError, ValueError) as exc:
        raise GeminiMatchError("Gemini returned invalid structured output") from exc

    allowed_files = {item.file_id for item in files}
    allowed_episodes = {item.episode_id for item in catalog}
    returned_files = [item.file_id for item in parsed.matches]
    if set(returned_files) != allowed_files or len(returned_files) != len(
        allowed_files
    ):
        raise GeminiMatchError("Gemini response did not cover each supplied file once")

    assigned: set[str] = set()
    results: list[GeminiMatchResult] = []
    for item in parsed.matches:
        if item.episode_id is not None:
            if item.episode_id not in allowed_episodes:
                raise GeminiMatchError(
                    "Gemini returned an episode outside the catalogue"
                )
            if item.episode_id in assigned:
                raise GeminiMatchError("Gemini assigned one episode more than once")
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
        )
        for attempt in range(self.max_retries + 1):
            try:
                response = self.transport.send(
                    api_key=api_key,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                )
            except (requests.RequestException, ConnectionError, TimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise ApiServiceError(
                        "Gemini", None, f"network failure: {type(exc).__name__}"
                    ) from exc
                self.sleep(2**attempt)
                continue

            if response.status_code in (401, 403):
                raise ApiCredentialError(
                    credential,
                    "rejected by the provider",
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
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
                raise ApiServiceError(
                    "Gemini",
                    response.status_code,
                    "request was not accepted",
                )
            try:
                matches = _parse_provider_response(response.payload, files, catalog)
            except GeminiResponseError as exc:
                if attempt >= self.max_retries:
                    raise
                body = _validation_retry_body(body, exc, attempt)
                continue
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

    def describe_with_key(
        self,
        files: tuple[UnmatchedFileEvidence, ...],
        *,
        release_hint: str,
        api_key: str,
        credential: CredentialName,
        prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
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
            try:
                response = self.transport.send(
                    api_key=api_key,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                )
            except (requests.RequestException, ConnectionError, TimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise ApiServiceError(
                        "Gemini", None, f"network failure: {type(exc).__name__}"
                    ) from exc
                self.sleep(2**attempt)
                continue
            if response.status_code in (401, 403):
                raise ApiCredentialError(
                    credential,
                    "rejected by the provider",
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
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
                raise ApiServiceError(
                    "Gemini", response.status_code, "request was not accepted"
                )
            return GeminiDescriptivePlan(
                mode="gemini-descriptive-review-plan",
                model=self.model,
                matches=_parse_descriptive_response(response.payload, files),
            )
        raise ApiServiceError("Gemini", None, "retry loop ended unexpectedly")

    def describe_with_configured_keys(
        self,
        files: tuple[UnmatchedFileEvidence, ...],
        *,
        release_hint: str,
        prior_attempts: Mapping[str, tuple[dict[str, object], ...]] | None = None,
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
                )
            except ApiCredentialError:
                continue
            except (ApiServiceError, GeminiResponseError) as exc:
                provider_errors.append(exc)
        if provider_errors:
            raise provider_errors[-1]
        raise ApiCredentialError("gemini-primary", "not configured or accepted")
