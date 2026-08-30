"""Schema-constrained Gemini fallback for canonical TV-series resolution."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mkv_episode_matcher.core.credentials import (
    ApiCredentialError,
    ApiServiceError,
    CredentialName,
)
from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.media.gemini_failover import (
    GeminiModelUnavailableError,
    is_gemini_model_unavailable_response,
    ordered_gemini_models,
    permits_gemini_model_fallback,
)
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiResponseError,
    GeminiTransport,
    RequestsGeminiTransport,
    _output_text,
)
from mkv_episode_matcher.tmdb_client import TvShowCandidate

_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)")


@dataclass(frozen=True)
class GeminiSeriesResolution:
    tmdb_id: int | None
    series_name: str
    confidence: float
    evidence: tuple[str, ...]
    alternative_series_names: tuple[str, ...] = ()


class _SeriesResolutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tmdb_id: int | None
    series_name: str = Field(min_length=1, max_length=160)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(min_length=1, max_length=3)
    alternative_series_names: list[str] = Field(max_length=4)

    @field_validator("series_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip(" .")
        if not cleaned or _WINDOWS_PATH.search(cleaned):
            raise ValueError("series name is invalid")
        return cleaned

    @field_validator("alternative_series_names")
    @classmethod
    def validate_alternative_names(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            name = " ".join(value.split()).strip(" .")
            if not name or len(name) > 160 or _WINDOWS_PATH.search(name):
                raise ValueError("alternative series name is invalid")
            if name.casefold() not in {item.casefold() for item in cleaned}:
                cleaned.append(name)
        return cleaned


def build_series_resolution_request(
    model: str,
    series_hint: str,
    candidates: tuple[TvShowCandidate, ...],
) -> dict:
    """Build a path-free request that cannot invent a supplied TMDb ID."""

    hint = " ".join(series_hint.split())[:200]
    if not hint or _WINDOWS_PATH.search(hint) or len(candidates) > 12:
        raise ValueError("Series-resolution input is invalid")
    allowed_ids = [item.tmdb_id for item in candidates]
    prompt = {
        "task": (
            "Resolve the optical-disc label to its canonical television series. "
            "Ignore packaging markers such as disc, volume, season, collection, "
            "CSR, DIM, and trailing set numbers. If supplied TMDb candidates "
            "contain the series, return exactly that candidate ID and name. If "
            "the candidate list is empty, propose only the canonical series name "
            "and return null for tmdb_id. Rank up to four distinct alternative "
            "canonical series names after the best answer. Do not invent a TMDb ID."
        ),
        "series_hint": hint,
        "tmdb_candidates": [
            {
                "tmdb_id": item.tmdb_id,
                "name": item.name,
                "original_name": item.original_name,
                "first_air_year": item.first_air_year,
                "overview": item.overview,
            }
            for item in candidates
        ],
    }
    id_schema: dict[str, object] = {"type": ["integer", "null"]}
    if allowed_ids:
        id_schema["enum"] = [*allowed_ids, None]
    schema = {
        "type": "object",
        "properties": {
            "tmdb_id": id_schema,
            "series_name": {"type": "string", "minLength": 1, "maxLength": 160},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string", "maxLength": 200},
            },
            "alternative_series_names": {
                "type": "array",
                "minItems": 0,
                "maxItems": 4,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
        },
        "required": [
            "tmdb_id",
            "series_name",
            "confidence",
            "evidence",
            "alternative_series_names",
        ],
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


class GeminiSeriesResolver:
    def __init__(
        self,
        *,
        model: str,
        fallback_models: Sequence[str] | None = None,
        transport: GeminiTransport | None = None,
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
    ):
        self.model = model
        self.models = ordered_gemini_models(model, fallback_models)
        self.transport = transport or RequestsGeminiTransport()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def resolve_with_key(  # noqa: C901 - bounded provider response handling
        self,
        series_hint: str,
        candidates: tuple[TvShowCandidate, ...],
        *,
        api_key: str,
        credential: CredentialName,
        model: str | None = None,
    ) -> GeminiSeriesResolution:
        if credential not in ("gemini-primary", "gemini-paid") or not api_key:
            raise ApiCredentialError(credential, "not configured")
        active_model = model or self.model
        body = build_series_resolution_request(active_model, series_hint, candidates)
        allowed = {item.tmdb_id: item for item in candidates}
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
                time.sleep(2**attempt)
                continue
            if response.status_code in (401, 403):
                raise ApiCredentialError(
                    credential,
                    "rejected by the provider",
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise ApiServiceError(
                    "Gemini",
                    response.status_code,
                    "provider is temporarily unavailable",
                )
            if is_gemini_model_unavailable_response(
                response.status_code, response.payload, response.raw_text
            ):
                raise GeminiModelUnavailableError(
                    "Gemini",
                    response.status_code,
                    "configured model is unavailable",
                )
            if response.status_code >= 400:
                raise ApiServiceError(
                    "Gemini", response.status_code, "request was not accepted"
                )
            try:
                parsed = _SeriesResolutionModel.model_validate_json(
                    _output_text(response.payload)
                )
            except (ValidationError, ValueError) as exc:
                raise GeminiResponseError(
                    "Gemini returned invalid series resolution"
                ) from exc
            if parsed.tmdb_id is not None and parsed.tmdb_id not in allowed:
                raise GeminiResponseError("Gemini returned an unsupplied TMDb ID")
            return GeminiSeriesResolution(
                parsed.tmdb_id,
                parsed.series_name,
                parsed.confidence,
                tuple(parsed.evidence),
                tuple(
                    name
                    for name in parsed.alternative_series_names
                    if name.casefold() != parsed.series_name.casefold()
                ),
            )
        raise ApiServiceError("Gemini", None, "retry loop ended unexpectedly")

    def resolve_with_configured_keys(
        self, series_hint: str, candidates: tuple[TvShowCandidate, ...]
    ) -> GeminiSeriesResolution:
        errors = []
        for model_index, active_model in enumerate(self.models):
            model_error: ApiServiceError | GeminiResponseError | None = None
            for credential, field in (
                ("gemini-primary", "gemini_primary_api_key"),
                ("gemini-paid", "gemini_paid_api_key"),
            ):
                api_key = getattr(load_environment_settings(), field)
                if not api_key:
                    continue
                try:
                    return self.resolve_with_key(
                        series_hint,
                        candidates,
                        api_key=api_key,
                        credential=credential,
                        model=active_model,
                    )
                except ApiCredentialError as exc:
                    errors.append(exc)
                except GeminiModelUnavailableError as exc:
                    errors.append(exc)
                    model_error = exc
                    break
                except (ApiServiceError, GeminiResponseError) as exc:
                    errors.append(exc)
                    model_error = exc
            next_model = (
                self.models[model_index + 1]
                if model_index + 1 < len(self.models)
                else None
            )
            if (
                model_error is not None
                and next_model is not None
                and permits_gemini_model_fallback(model_error)
            ):
                logger.warning(
                    "Gemini model {} exhausted or unavailable; trying configured fallback {}",
                    active_model,
                    next_model,
                )
                continue
            if model_error is not None:
                raise model_error
        if errors:
            raise errors[-1]
        raise ApiCredentialError("gemini-primary", "not configured or accepted")
