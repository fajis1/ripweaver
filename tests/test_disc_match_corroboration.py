from types import SimpleNamespace

from mkv_episode_matcher.backend.disc_match_corroboration import (
    corroborate_single_window_local_match,
)
from mkv_episode_matcher.core.tv_identification_policy import (
    AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION,
    LOCAL_DIALOGUE_TWO_WINDOW_SOURCE,
)

FINGERPRINT = "0123456789abcdef"


def _history(episode_id: str):
    return {
        "episode_id": episode_id,
        "series_name": "The Office",
        "match_source": "local_evidence",
        "assignment_evidence_source": LOCAL_DIALOGUE_TWO_WINDOW_SOURCE,
        "identification_policy_version": AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION,
    }


def _store(outcomes):
    return SimpleNamespace(
        expected_title_indexes_for_disc=lambda _fingerprint: (1, 2, 3, 4),
        catalogue_title_history=lambda _fingerprint: outcomes,
    )


def _payload():
    return {
        "disc_fingerprint": FINGERPRINT,
        "title_index": 4,
        "media_context": {"series_name": "The Office"},
    }


def _trace(episode_id="S06E11"):
    return {
        "selected_episode_id": episode_id,
        "selected_score": 0.890611,
        "selected_vote_count": 1,
        "runner_up_score": 0.0,
        "runner_up_vote_count": 0,
        "engine_threshold": 0.7,
        "subtitle_release_match": "generic",
    }


def test_single_window_match_is_corroborated_by_settled_strong_siblings():
    result = corroborate_single_window_local_match(
        _store({1: _history("S06E08"), 2: _history("S06E09"), 3: _history("S06E10")}),
        _payload(),
        _trace(),
        candidate_episode_id="S06E11",
    )

    assert result is not None
    assert result["reason"] == "single_window_disc_context_corroborated"
    assert result["anchor_episode_ids"] == ["S06E08", "S06E09", "S06E10"]
    assert result["candidate_scope"] == "S06E07-E11"
    assert result["settled_sibling_count"] == 3
    assert result["title_order_used"] is False


def test_single_window_match_is_not_corroborated_outside_anchor_range():
    result = corroborate_single_window_local_match(
        _store({1: _history("S06E08"), 2: _history("S06E09"), 3: _history("S06E10")}),
        _payload(),
        _trace("S06E20"),
        candidate_episode_id="S06E20",
    )

    assert result is None


def test_single_window_match_requires_every_sibling_to_be_settled():
    result = corroborate_single_window_local_match(
        _store({1: _history("S06E08"), 2: _history("S06E09")}),
        _payload(),
        _trace(),
        candidate_episode_id="S06E11",
    )

    assert result is None


def test_single_window_match_rejects_legacy_unproven_anchors():
    legacy = {
        "episode_id": "S06E08",
        "series_name": "The Office",
        "match_source": "local_evidence",
        "assignment_evidence_source": None,
        "identification_policy_version": None,
    }
    result = corroborate_single_window_local_match(
        _store({1: legacy, 2: _history("S06E09"), 3: _history("S06E10")}),
        _payload(),
        _trace(),
        candidate_episode_id="S06E11",
    )

    assert result is not None
    assert result["anchor_count"] == 2
