"""Saved-data-only disc routing tests; no disc or provider access."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from mkv_episode_matcher.disc.routing import (
    DiscAssessment,
    RoutingError,
    TitleEvidence,
    assessment_from_contract,
)
from mkv_episode_matcher.disc.routing_preparation import build_preparation_assessment
from mkv_episode_matcher.pipeline_queue import PipelineQueueError, PipelineQueueStore

FINGERPRINT = "0123456789abcdef"


def test_hint_does_not_establish_role():
    assessment = DiscAssessment(FINGERPRINT, (0, 1), user_hint="tv")
    assert [item.role for item in assessment.title_roles()] == ["unknown", "unknown"]
    assert assessment.composition == "unknown"


def test_movie_with_extras_and_conflicting_hint():
    assessment = DiscAssessment(
        FINGERPRINT,
        (0, 1, 2),
        user_hint="tv",
        evidence=(
            TitleEvidence(0, "database", "supported", "movie"),
            TitleEvidence(1, "content", "supported", "extra"),
            TitleEvidence(2, "database", "unavailable"),
        ),
    )
    assert [item.role for item in assessment.title_roles()] == [
        "movie",
        "extra",
        "unknown",
    ]
    assert assessment.composition == "movie_with_extras"
    assert DiscAssessment.from_dict(assessment.to_dict()).digest == assessment.digest


def test_stronger_content_evidence_outweighs_structural_tv_guess():
    assessment = DiscAssessment(
        FINGERPRINT,
        (0,),
        evidence=(
            TitleEvidence(0, "inventory", "supported", "tv"),
            TitleEvidence(0, "content", "supported", "movie"),
        ),
    )
    assert assessment.title_roles()[0].role == "movie"


def test_equal_strength_conflict_remains_unresolved():
    assessment = DiscAssessment(
        FINGERPRINT,
        (0, 1),
        evidence=(
            TitleEvidence(0, "database", "supported", "tv"),
            TitleEvidence(0, "database", "supported", "movie"),
            TitleEvidence(1, "content", "supported", "extra"),
        ),
    )
    assert assessment.title_roles()[0].role == "conflicting"
    assert assessment.composition == "extras"


def test_tv_and_mixed_composition_from_distinct_title_evidence():
    tv = DiscAssessment(
        FINGERPRINT,
        (0, 1),
        evidence=(TitleEvidence(0, "database", "supported", "tv"),),
    )
    assert tv.composition == "tv"
    mixed = replace(
        tv,
        evidence=tv.evidence + (TitleEvidence(1, "content", "supported", "movie"),),
    )
    assert mixed.composition == "mixed"


def test_eleven_title_movie_with_extras_settles_reviewed_skips(tmp_path):
    assessment = DiscAssessment(
        FINGERPRINT,
        tuple(range(11)),
        user_hint="tv",
        evidence=(
            TitleEvidence(0, "content", "supported", "movie"),
            *(
                TitleEvidence(index, "content", "supported", "extra")
                for index in range(2, 8)
            ),
            *(
                TitleEvidence(index, "review", "supported", "skip")
                for index in (1, 8, 9, 10)
            ),
        ),
    )

    assert assessment.composition == "movie_with_extras"
    assert [item.role for item in assessment.title_roles()] == [
        "movie",
        "skip",
        "extra",
        "extra",
        "extra",
        "extra",
        "extra",
        "extra",
        "skip",
        "skip",
        "skip",
    ]
    restored = DiscAssessment.from_dict(assessment.to_dict())
    assert restored.digest == assessment.digest
    assert restored.composition == assessment.composition
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    saved = store.routing_append(restored, expected_revision=0)
    restarted = PipelineQueueStore(store.database_path)
    assert restarted.routing_latest(FINGERPRINT) == saved
    assert restarted.routing_latest(FINGERPRINT).composition == "movie_with_extras"


def test_legacy_contract_has_no_assessment_and_new_binding_is_exact():
    legacy = {
        "disc_fingerprint": FINGERPRINT,
        "title_index": 0,
        "media_context": {"content_hint": "tv"},
    }
    assert assessment_from_contract(legacy) is None
    assessment = DiscAssessment(FINGERPRINT, (0, 1))
    context = {
        "routing_assessment": assessment.to_dict(),
        "routing_assessment_digest": assessment.digest,
        "routing_assessment_revision": assessment.revision,
    }
    payload = {**legacy, "media_context": context}
    assert assessment_from_contract(payload) == assessment
    with pytest.raises(RoutingError, match="inconsistent"):
        assessment_from_contract({**payload, "title_index": 3})
    with pytest.raises(RoutingError, match="inconsistent"):
        assessment_from_contract({
            **payload,
            "media_context": {**context, "routing_assessment_digest": "bad"},
        })
    with pytest.raises(RoutingError, match="incomplete"):
        assessment_from_contract({
            **payload,
            "media_context": {"routing_assessment": assessment.to_dict()},
        })


def test_preparation_hint_does_not_promote_runtime_cluster_to_tv():
    observed = build_preparation_assessment(
        fingerprint=FINGERPRINT,
        title_indexes=(0, 1, 2),
        user_hint="tv",
        title_classifications={0: "episode", 1: "extra", 2: "review"},
        explicit_tv_context=False,
        database_status="unavailable",
    )
    assert observed.user_hint == "tv"
    assert [item.role for item in observed.title_roles()] == ["unknown"] * 3
    assert observed.composition == "unknown"
    assert all(item.status == "unavailable" for item in observed.evidence)


def test_preparation_label_plus_title_shape_keeps_tv_extras_separate():
    observed = build_preparation_assessment(
        fingerprint=FINGERPRINT,
        title_indexes=(0, 1, 2),
        user_hint="movie",
        title_classifications={0: "episode", 1: "extra", 2: "review"},
        explicit_tv_context=True,
    )
    assert [item.role for item in observed.title_roles()] == ["tv", "extra", "unknown"]
    assert observed.composition == "tv_with_extras"


def test_preparation_conflicting_trusted_assignments_are_not_forced():
    observed = build_preparation_assessment(
        fingerprint=FINGERPRINT,
        title_indexes=(0,),
        user_hint="movie",
        title_classifications={0: "review"},
        explicit_tv_context=False,
        episode_assignment_indexes=(0,),
        feature_assignment_indexes=(0,),
    )
    assert observed.title_roles()[0].role == "conflicting"


@pytest.mark.parametrize(
    "change",
    [
        {"inventory_fingerprint": "not-a-fingerprint"},
        {"title_indexes": (1, 0)},
        {"title_indexes": (0, 0)},
        {"title_indexes": (True,)},
        {"user_hint": ["tv"]},
        {"revision": 0},
        {"schema_version": 2},
        {"evidence": (TitleEvidence(3, "content", "supported", "movie"),)},
    ],
)
def test_invalid_assessment_is_rejected(change):
    with pytest.raises(RoutingError):
        replace(DiscAssessment(FINGERPRINT, (0, 1)), **change)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": []},
        {"status": []},
        {"status": "unavailable", "role": "tv"},
        {"status": "supported", "role": None},
        {"title_index": True},
    ],
)
def test_invalid_evidence_is_rejected(kwargs):
    with pytest.raises(RoutingError):
        TitleEvidence(**{
            "title_index": 0,
            "source": "content",
            "status": "supported",
            "role": "tv",
            **kwargs,
        })


def test_schema_rejects_extra_fields_and_oversized_evidence():
    assessment = DiscAssessment(FINGERPRINT, (0,))
    with pytest.raises(RoutingError):
        DiscAssessment.from_dict({**assessment.to_dict(), "source_path": "private"})
    with pytest.raises(RoutingError):
        DiscAssessment.from_dict({**assessment.to_dict(), "evidence": [{}] * 40_001})


def test_queue_routing_observation_idempotent_and_revision_content_based(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    base = DiscAssessment(FINGERPRINT, (0, 1), user_hint="tv")
    first = store.routing_save_observation(base)
    assert first.revision == 1
    assert store.routing_save_observation(base) == first
    second = store.routing_save_observation(replace(base, user_hint="movie"))
    assert second.revision == 2
    assert store.routing_save_observation(replace(base, user_hint="movie")) == second
    third = store.routing_save_observation(
        replace(
            base,
            user_hint="movie",
            evidence=(TitleEvidence(0, "database", "supported", "movie"),),
        )
    )
    assert third.revision == 3
    assert PipelineQueueStore(store.database_path).routing_latest(FINGERPRINT) == third


def test_queue_routing_refresh_preserves_content_and_rejects_changed_inventory(
    tmp_path,
):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    base = store.routing_save_observation(DiscAssessment(FINGERPRINT, (0, 1)))
    content = store.routing_append(
        replace(
            base,
            revision=2,
            evidence=(TitleEvidence(0, "content", "supported", "movie"),),
        ),
        expected_revision=1,
    )
    refreshed = store.routing_save_observation(
        DiscAssessment(FINGERPRINT, (0, 1), user_hint="tv")
    )
    assert refreshed.revision == 3
    assert TitleEvidence(0, "content", "supported", "movie") in refreshed.evidence
    assert refreshed.title_roles()[0].role == "movie"
    with pytest.raises(RoutingError, match="inventory changed"):
        store.routing_save_observation(DiscAssessment(FINGERPRINT, (0,)))
    assert store.routing_latest(FINGERPRINT) == refreshed
    assert content.revision == 2


def test_queue_routing_revision_cannot_revoke_accepted_content(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    first = store.routing_append(
        DiscAssessment(
            FINGERPRINT,
            (0,),
            evidence=(TitleEvidence(0, "content", "supported", "movie"),),
        ),
        expected_revision=0,
    )
    with pytest.raises(RoutingError, match="cannot be revoked"):
        store.routing_append(
            replace(first, revision=2, evidence=()), expected_revision=1
        )


def test_queue_routing_append_stale_write_and_exact_retry(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    first = store.routing_append(DiscAssessment(FINGERPRINT, (0,)), expected_revision=0)
    assert store.routing_append(first, expected_revision=0) == first
    second = store.routing_append(
        replace(first, revision=2, user_hint="movie"), expected_revision=1
    )
    with pytest.raises(RoutingError, match="stale"):
        store.routing_append(
            replace(first, revision=2, user_hint="tv"), expected_revision=1
        )
    with pytest.raises(RoutingError, match="invalid"):
        store.routing_append(replace(first, revision=3), expected_revision=1)
    assert store.routing_latest(FINGERPRINT) == second


def test_queue_routing_concurrent_revision_claims_only_one_wins(tmp_path):
    path = tmp_path / "queue.sqlite3"
    store = PipelineQueueStore(path)
    first = store.routing_append(DiscAssessment(FINGERPRINT, (0,)), expected_revision=0)
    candidates = [
        replace(first, revision=2, user_hint=hint) for hint in ("movie", "tv")
    ]

    def append(candidate):
        try:
            return PipelineQueueStore(path).routing_append(
                candidate, expected_revision=1
            )
        except RoutingError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, candidates))
    assert sum(result is not None for result in results) == 1
    assert store.routing_latest(FINGERPRINT).revision == 2


def test_queue_routing_attempt_claim_settle_restart_and_forget(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(FINGERPRINT, (0, 1)), expected_revision=0
    )
    assert store.routing_claim(assessment, 0, "classify")
    assert not store.routing_claim(assessment, 0, "classify")
    assert store.routing_reconcile_interrupted() == 1
    assert store.routing_reconcile_interrupted() == 0
    assert store.routing_attempts(FINGERPRINT, 0)[0].outcome == "interrupted"
    assert not store.routing_claim(assessment, 0, "classify")
    assert store.routing_claim(assessment, 0, "movie")
    matched = store.routing_settle(assessment, 0, "movie", "matched")
    assert store.routing_settle(assessment, 0, "movie", "matched") == matched
    with pytest.raises(RoutingError, match="inconsistent"):
        store.routing_settle(assessment, 0, "movie", "no_match")
    assert not store.routing_claim(assessment, 0, "tv")
    assert not PipelineQueueStore(store.database_path).routing_claim(
        assessment, 0, "tv"
    )
    store.remember_disc_matching_scope(FINGERPRINT, (0,))
    store.forget_disc_records(FINGERPRINT)
    assert store.routing_latest(FINGERPRINT) is None
    assert store.routing_attempts(FINGERPRINT, 0) == ()
    assert store.disc_matching_scope(FINGERPRINT) is None


def test_queue_routing_service_failure_holds_but_no_match_allows_alternate(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(FINGERPRINT, (0, 1)), expected_revision=0
    )
    assert store.routing_claim(assessment, 0, "tv")
    store.routing_settle(assessment, 0, "tv", "no_match")
    assert store.routing_claim(assessment, 0, "movie")
    assert store.routing_claim(assessment, 1, "tv")
    store.routing_settle(assessment, 1, "tv", "service_failed")
    assert not store.routing_claim(assessment, 1, "movie")


def test_queue_routing_stale_worker_cannot_settle_after_new_revision(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    first = store.routing_append(DiscAssessment(FINGERPRINT, (0,)), expected_revision=0)
    assert store.routing_claim(first, 0, "classify")
    second = store.routing_append(
        replace(first, revision=2, user_hint="movie"), expected_revision=1
    )
    with pytest.raises(RoutingError, match="stale"):
        store.routing_settle(first, 0, "classify", "matched")
    assert store.routing_latest(FINGERPRINT) == second


def test_forget_refuses_a_running_route(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(FINGERPRINT, (0,)), expected_revision=0
    )
    assert store.routing_claim(assessment, 0, "classify")
    with pytest.raises(PipelineQueueError, match="currently running"):
        store.forget_disc_records(FINGERPRINT)
    assert store.routing_latest(FINGERPRINT) == assessment


def test_queue_routing_migrates_existing_database_without_changing_scope(tmp_path):
    path = tmp_path / "old-queue.sqlite3"
    store = PipelineQueueStore(path)
    store.remember_disc_matching_scope(FINGERPRINT, (0, 2))
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE disc_route_attempts")
        connection.execute("DROP TABLE disc_routing_revisions")
    upgraded = PipelineQueueStore(path)
    assert upgraded.disc_matching_scope(FINGERPRINT) == (0, 2)
    assert upgraded.routing_latest(FINGERPRINT) is None
    assert upgraded.routing_append(
        DiscAssessment(FINGERPRINT, (0, 2)), expected_revision=0
    )
