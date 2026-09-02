"""Bounded Gemini model selection and fallback classification."""

from __future__ import annotations

import json
from collections.abc import Sequence

from mkv_episode_matcher.core.credentials import ApiServiceError

MAX_GEMINI_FALLBACK_MODELS = 2

# These defaults mirror the capacity-oriented model families used by OpenReader.
# A user-supplied fallback list replaces the suggested chain for that primary.
DEFAULT_GEMINI_MODEL_FALLBACKS: dict[str, tuple[str, ...]] = {
    "gemini-3.7-flash": ("gemini-3.6-flash", "gemini-3.5-flash"),
    "gemini-3.6-flash": ("gemini-3.5-flash", "gemini-2.5-flash"),
    "gemini-3.5-flash": ("gemini-2.5-flash",),
    "gemini-3.5-flash-lite": (
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
    ),
    "gemini-3.1-flash-lite": ("gemini-2.5-flash-lite",),
}

_UNAVAILABLE_MARKERS = (
    "model_not_found",
    "model not found",
    "model does not exist",
    "model is not available",
    "model is not supported",
    "unsupported model",
)


class GeminiModelUnavailableError(ApiServiceError):
    """A definitive provider response allowing a configured model fallback."""


def ordered_gemini_models(
    primary: str,
    fallbacks: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Return one primary and at most two unique, validated fallback models."""

    primary = primary.strip()
    candidates = (
        DEFAULT_GEMINI_MODEL_FALLBACKS.get(primary, ())
        if fallbacks is None
        else tuple(fallbacks)
    )
    models = [primary]
    for value in candidates:
        model = value.strip()
        if model and model not in models:
            models.append(model)
        if len(models) >= MAX_GEMINI_FALLBACK_MODELS + 1:
            break
    return tuple(models)


def is_gemini_model_unavailable_response(
    status_code: int,
    payload: dict,
    raw_text: str | None,
) -> bool:
    """Recognize only explicit model-not-available 400/404 provider responses."""

    if status_code not in (400, 404):
        return False
    text = raw_text
    if text is None:
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    normalized = text.casefold()
    if status_code == 404 and not normalized.strip():
        return True
    return any(marker in normalized for marker in _UNAVAILABLE_MARKERS) or (
        "models/" in normalized
        and any(
            marker in normalized
            for marker in ("not found", "not supported", "not available")
        )
    )


def permits_gemini_model_fallback(error: Exception) -> bool:
    """Return whether another configured model may be tried for this failure."""

    return isinstance(error, GeminiModelUnavailableError) or (
        isinstance(error, ApiServiceError) and error.status_code in (429, 503)
    )
