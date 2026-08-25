"""Conservative same-disc corroboration for a one-window local TV match."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from mkv_episode_matcher.core.tv_identification_policy import (
    AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION,
    LOCAL_DIALOGUE_TWO_WINDOW_SOURCE,
    OPENSUBTITLES_TWO_WINDOW_SOURCE,
)

_DISC_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
_EPISODE_ID = re.compile(r"(?i)S(\d{1,3})E(\d{1,4})")
_STRONG_HISTORY_EVIDENCE = frozenset({
    LOCAL_DIALOGUE_TWO_WINDOW_SOURCE,
    OPENSUBTITLES_TWO_WINDOW_SOURCE,
})
_REVIEWED_HISTORY_SOURCES = frozenset({
    "deterministic",
    "manual_playback",
    "server_assisted",
})
_SUPPORTED_SUBTITLE_RELEASES = frozenset({"exact", "compatible", "generic"})
_MINIMUM_SINGLE_WINDOW_SCORE = 0.85
_MINIMUM_SINGLE_WINDOW_MARGIN = 0.25


class DiscMatchHistory(Protocol):
    """Path-free durable history used by the corroboration boundary."""

    def expected_title_indexes_for_disc(
        self, disc_fingerprint: str
    ) -> tuple[int, ...]: ...

    def catalogue_title_history(
        self, disc_fingerprint: str
    ) -> dict[int, dict[str, str | int | None]]: ...


def _normalized_series(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return normalized or None


def _episode_parts(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = _EPISODE_ID.fullmatch(value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _strong_history_anchor(outcome: Mapping[str, object], *, series_name: str) -> bool:
    if _normalized_series(outcome.get("series_name")) != series_name:
        return False
    evidence_source = outcome.get("assignment_evidence_source")
    policy_version = outcome.get("identification_policy_version")
    if (
        evidence_source in _STRONG_HISTORY_EVIDENCE
        and policy_version == AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION
    ):
        return True
    return outcome.get("match_source") in _REVIEWED_HISTORY_SOURCES


def _single_window_scores(
    decision_trace: Mapping[str, object], candidate_episode_id: str
) -> tuple[float, float] | None:
    selected_episode_id = decision_trace.get("selected_episode_id")
    selected_score = _number(decision_trace.get("selected_score"))
    runner_up_score = _number(decision_trace.get("runner_up_score")) or 0.0
    engine_threshold = _number(decision_trace.get("engine_threshold"))
    minimum_score = max(
        _MINIMUM_SINGLE_WINDOW_SCORE,
        (engine_threshold + 0.15) if engine_threshold is not None else 0.0,
    )
    if (
        decision_trace.get("selected_vote_count") != 1
        or not isinstance(selected_episode_id, str)
        or selected_episode_id.upper() != candidate_episode_id.upper()
        or selected_score is None
        or selected_score < minimum_score
        or selected_score - runner_up_score < _MINIMUM_SINGLE_WINDOW_MARGIN
        or int(decision_trace.get("runner_up_vote_count", 0) or 0) > 0
        or decision_trace.get("subtitle_release_match")
        not in _SUPPORTED_SUBTITLE_RELEASES
    ):
        return None
    return selected_score, runner_up_score


def _disc_identity(
    rip_payload: Mapping[str, object], candidate_episode_id: str
) -> tuple[str, int, str, tuple[int, int]] | None:
    fingerprint = rip_payload.get("disc_fingerprint")
    title_index = rip_payload.get("title_index")
    context = rip_payload.get("media_context")
    series_name = (
        _normalized_series(context.get("series_name"))
        if isinstance(context, Mapping)
        else None
    )
    candidate_parts = _episode_parts(candidate_episode_id)
    if (
        not isinstance(fingerprint, str)
        or _DISC_FINGERPRINT.fullmatch(fingerprint) is None
        or not isinstance(title_index, int)
        or isinstance(title_index, bool)
        or title_index < 0
        or series_name is None
        or candidate_parts is None
    ):
        return None
    return fingerprint, title_index, series_name, candidate_parts


def _settled_strong_anchors(
    outcomes: Mapping[int, Mapping[str, object]],
    sibling_indexes: set[int],
    *,
    series_name: str,
) -> dict[str, tuple[int, int]]:
    anchors: dict[str, tuple[int, int]] = {}
    for sibling_index in sibling_indexes:
        outcome = outcomes.get(sibling_index)
        if outcome is None or not _strong_history_anchor(
            outcome, series_name=series_name
        ):
            continue
        episode_id = outcome.get("episode_id")
        episode_parts = _episode_parts(episode_id)
        if isinstance(episode_id, str) and episode_parts is not None:
            anchors[episode_id.upper()] = episode_parts
    return anchors


def corroborate_single_window_local_match(
    history_store: DiscMatchHistory,
    rip_payload: Mapping[str, object],
    decision_trace: Mapping[str, object],
    *,
    candidate_episode_id: str,
) -> dict[str, object] | None:
    """Confirm a strong one-window result against settled same-disc siblings.

    This does not use MakeMKV title order. The candidate still comes from its
    own dialogue evidence; sibling assignments only establish a plausible
    same-season range and prove that the rest of the disc has settled.
    """

    scores = _single_window_scores(decision_trace, candidate_episode_id)
    identity = _disc_identity(rip_payload, candidate_episode_id)
    if scores is None or identity is None:
        return None
    selected_score, runner_up_score = scores
    fingerprint, title_index, series_name, candidate_parts = identity

    expected = set(history_store.expected_title_indexes_for_disc(fingerprint))
    outcomes = history_store.catalogue_title_history(fingerprint)
    if (
        not expected
        or title_index not in expected
        or not (expected - {title_index}).issubset(outcomes)
    ):
        return None

    anchors = _settled_strong_anchors(
        outcomes, expected - {title_index}, series_name=series_name
    )

    candidate_season, candidate_episode = candidate_parts
    if len(anchors) < 2 or candidate_episode_id.upper() in anchors:
        return None
    anchor_seasons = {season for season, _episode in anchors.values()}
    if anchor_seasons != {candidate_season}:
        return None
    anchor_episodes = tuple(episode for _season, episode in anchors.values())
    disc_title_count = len(expected)
    if max(anchor_episodes) - min(anchor_episodes) + 1 > disc_title_count:
        return None
    minimum_episode = max(1, max(anchor_episodes) - disc_title_count + 1)
    maximum_episode = min(anchor_episodes) + disc_title_count - 1
    if not minimum_episode <= candidate_episode <= maximum_episode:
        return None

    anchor_episode_ids = sorted(
        anchors,
        key=lambda episode_id: anchors[episode_id],
    )
    return {
        "reason": "single_window_disc_context_corroborated",
        "candidate_episode_id": candidate_episode_id.upper(),
        "candidate_scope": (
            f"S{candidate_season:02d}E{minimum_episode:02d}-E{maximum_episode:02d}"
        ),
        "selected_score": round(selected_score, 6),
        "runner_up_score": round(runner_up_score, 6),
        "decision_margin": round(selected_score - runner_up_score, 6),
        "anchor_count": len(anchor_episode_ids),
        "anchor_episode_ids": anchor_episode_ids[:12],
        "disc_title_count": disc_title_count,
        "settled_sibling_count": len(expected) - 1,
        "title_order_used": False,
    }
