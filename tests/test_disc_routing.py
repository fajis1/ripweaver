import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from mkv_episode_matcher.disc.routing import (
    DiscRoutingAssessment,
    RoutingError,
    TitleRoutingEvidence,
)
from mkv_episode_matcher.disc.routing_store import DiscRoutingStore

FINGERPRINT = "0123456789abcdef"


def assessment(*roles, hint=None, source="database"):
    return DiscRoutingAssessment(
        FINGERPRINT,
        tuple(range(len(roles))),
        user_hint=hint,
        evidence=tuple(
            TitleRoutingEvidence(i, role, source)
            for i, role in enumerate(roles)
            if role != "unknown"
        ),
    )


@pytest.mark.parametrize(
    "roles,composition",
    [
        (("tv", "tv"), "tv"),
        (("tv", "extras"), "tv_with_extras"),
        (("movie", "extras"), "movies_with_extras"),
        (("movie", "movie"), "movies"),
        (("tv", "movie", "extras"), "mixed"),
        (("extras",), "extras"),
        (("unknown",), "unknown"),
    ],
)
def test_disc_composition_keeps_individual_title_roles(roles, composition):
    result = assessment(*roles)
    assert result.composition == composition
    assert tuple(route.role for route in result.title_routes()) == roles


@pytest.mark.parametrize("hint", [None, "tv", "movie", "extras", "mixed"])
def test_hint_prioritizes_search_but_never_establishes_content(hint):
    result = assessment("unknown", hint=hint)
    route = result.title_routes()[0]
    assert route.role == "unknown"
    assert result.composition == "unknown"
    assert result.user_hint == hint
    assert route.investigation_order[0] == (
        "mixed-classifier" if hint in (None, "mixed") else hint
    )


@pytest.mark.parametrize("role,hint", [("movie", "tv"), ("tv", "movie")])
def test_title_evidence_overrides_wrong_hint_without_erasing_it(role, hint):
    result = assessment(role, hint=hint)
    assert result.user_hint == hint
    assert result.title_routes()[0].investigation_order[0] == role


def test_content_evidence_can_correct_initial_structural_route():
    initial = assessment("tv", source="label", hint="tv")
    revised = replace(
        initial,
        revision=2,
        evidence=initial.evidence + (TitleRoutingEvidence(0, "movie", "content"),),
    )
    assert initial.title_routes()[0].role == "tv"
    assert revised.title_routes()[0].role == "movie"
    assert revised.user_hint == "tv"


def test_conflicting_equal_strength_evidence_remains_unknown():
    initial = assessment("tv", hint="movie")
    result = replace(
        initial,
        evidence=initial.evidence + (TitleRoutingEvidence(0, "movie", "database"),),
    )
    route = result.title_routes()[0]
    assert route.role == "unknown"
    assert route.reason == "conflicting_evidence"
    assert route.investigation_order[0] == "mixed-classifier"


def test_serialization_is_deterministic_and_rejects_extra_private_fields():
    result = assessment("movie", "extras")
    assert (
        result.digest
        == replace(result, evidence=tuple(reversed(result.evidence))).digest
    )
    assert DiscRoutingAssessment.from_dict(result.to_dict()).digest == result.digest
    for key in ("source_path", "dialogue", "execution_authorized"):
        with pytest.raises(RoutingError):
            DiscRoutingAssessment.from_dict({**result.to_dict(), key: "not allowed"})


@pytest.mark.parametrize(
    "changes",
    [
        {"inventory_fingerprint": "invalid"},
        {"title_indexes": (0, 0)},
        {"title_indexes": (True,)},
        {"revision": True},
        {"schema_version": 2},
        {"user_hint": []},
        {"evidence": (TitleRoutingEvidence(2, "tv", "content"),)},
    ],
)
def test_malformed_or_substituted_inventory_is_refused(changes):
    with pytest.raises(RoutingError):
        replace(assessment("tv"), **changes)


def test_routing_revisions_survive_restart_and_exact_retries(tmp_path):
    path = tmp_path / "routing.sqlite3"
    original = assessment("unknown", hint="tv")
    store = DiscRoutingStore(path)
    store.append(original, expected_revision=0)
    revised = replace(
        original, revision=2, evidence=(TitleRoutingEvidence(0, "movie", "content"),)
    )
    store.append(revised, expected_revision=1)
    restarted = DiscRoutingStore(path)
    assert restarted.latest(FINGERPRINT) == revised
    assert restarted.append(original, expected_revision=0) == original
    assert restarted.latest(FINGERPRINT) == revised


def test_stale_worker_cannot_replace_another_workers_decision(tmp_path):
    path = tmp_path / "routing.sqlite3"
    first, second = DiscRoutingStore(path), DiscRoutingStore(path)
    original = assessment("unknown")
    first.append(original, expected_revision=0)
    first.append(replace(original, revision=2, user_hint="movie"), expected_revision=1)
    with pytest.raises(RoutingError, match="stale"):
        second.append(
            replace(original, revision=2, user_hint="tv"), expected_revision=1
        )
    assert second.latest(FINGERPRINT).user_hint == "movie"


def test_revision_cannot_expand_same_fingerprint_inventory(tmp_path):
    store = DiscRoutingStore(tmp_path / "routing.sqlite3")
    original = assessment("tv")
    store.append(original, expected_revision=0)
    with pytest.raises(RoutingError, match="inventory"):
        store.append(
            replace(original, revision=2, title_indexes=(0, 1)), expected_revision=1
        )
    assert store.latest(FINGERPRINT) == original


def test_other_disc_has_independent_revision_history(tmp_path):
    store = DiscRoutingStore(tmp_path / "routing.sqlite3")
    first = assessment("tv")
    second = replace(assessment("movie"), inventory_fingerprint="fedcba9876543210")
    store.append(first, expected_revision=0)
    store.append(second, expected_revision=0)
    assert store.latest(first.inventory_fingerprint) == first
    assert store.latest(second.inventory_fingerprint) == second


def test_concurrent_revisions_have_exactly_one_winner(tmp_path):
    path = tmp_path / "routing.sqlite3"
    store = DiscRoutingStore(path)
    initial = assessment("unknown")
    store.append(initial, expected_revision=0)
    barrier = threading.Barrier(2)

    def publish(hint):
        worker_store = DiscRoutingStore(path)
        barrier.wait(timeout=5)
        try:
            worker_store.append(
                replace(initial, revision=2, user_hint=hint), expected_revision=1
            )
            return "saved"
        except RoutingError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(publish, ("tv", "movie"))) == ["saved", "stale"]
    assert store.latest(FINGERPRINT).revision == 2


def test_changed_saved_payload_is_refused(tmp_path):
    path = tmp_path / "routing.sqlite3"
    store = DiscRoutingStore(path)
    store.append(assessment("movie"), expected_revision=0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE disc_routing_revisions SET assessment_sha256=?", ("0" * 64,)
        )
    with pytest.raises(RoutingError, match="identity"):
        store.latest(FINGERPRINT)
