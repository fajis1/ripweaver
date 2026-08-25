from mkv_episode_matcher.media.unmatched import (
    EpisodeRef,
    FileEvidence,
    SubtitleWindow,
    bm25_episode_scores,
    load_srt_reference_windows,
    plan_unmatched,
    rank_candidates,
    rank_reference_query,
    runtime_similarity,
)


def test_bm25_retrieves_relevant_episode_without_full_fuzzy_scan():
    first = EpisodeRef(1, 1)
    second = EpisodeRef(2, 3)
    scores = bm25_episode_scores(
        "the magic mirror belongs to the queen",
        [
            SubtitleWindow(first, "the spaceship is ready for launch"),
            SubtitleWindow(second, "the magic mirror belongs to the evil queen"),
        ],
    )

    assert scores[second.key] == 1.0
    assert first.key not in scores


def test_load_and_rank_srt_references_across_seasons(tmp_path):
    (tmp_path / "Show - S01E01.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nThe astronauts prepare their spaceship.\n",
        encoding="utf-8",
    )
    (tmp_path / "Show - S04E03.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n"
        "The evil goblin travels the universe creating mischief.\n",
        encoding="utf-8",
    )

    windows = load_srt_reference_windows(
        tmp_path,
        window_words=6,
        stride_words=3,
    )
    candidates = rank_reference_query(
        "an evil goblin roamed the universe and created mischief",
        windows,
        top_k=2,
    )

    assert candidates[0].episode_key == "S04E03"
    assert candidates[0].query_term_coverage > 0.5


def test_reference_loader_ignores_files_without_episode_key(tmp_path):
    (tmp_path / "unknown.srt").write_text("Dialogue", encoding="utf-8")

    assert load_srt_reference_windows(tmp_path) == []


def test_runtime_similarity_penalizes_large_difference():
    assert runtime_similarity(1500, 1510) > runtime_similarity(1500, 2400)


def test_candidate_pruning_excludes_implausible_runtime():
    evidence = FileEvidence(
        file_id="disc-01-title-000",
        duration_seconds=1500,
        text_scores={"S01E01": 0.8, "S01E02": 0.99},
    )
    candidates = rank_candidates(
        evidence,
        [
            EpisodeRef(1, 1, runtime_seconds=1510),
            EpisodeRef(1, 2, runtime_seconds=3600),
        ],
    )

    assert [candidate.episode.key for candidate in candidates] == ["S01E01"]


def test_disc_assignment_is_unique_and_order_preserving():
    episodes = [
        EpisodeRef(1, 1, runtime_seconds=1500),
        EpisodeRef(1, 2, runtime_seconds=1500),
        EpisodeRef(1, 3, runtime_seconds=1500),
    ]
    files = [
        FileEvidence(
            "disc-title-000",
            1500,
            {"S01E01": 0.90, "S01E02": 0.89},
        ),
        FileEvidence(
            "disc-title-001",
            1500,
            {"S01E01": 0.95, "S01E02": 0.92, "S01E03": 0.20},
        ),
    ]

    plan = plan_unmatched("Test Show", files, episodes)

    assert [item.proposed_episode for item in plan.items] == ["S01E01", "S01E02"]
    assert len({item.proposed_episode for item in plan.items}) == 2


def test_low_evidence_remains_unmatched():
    plan = plan_unmatched(
        "Test Show",
        [FileEvidence("disc-title-000", 1500, {})],
        [EpisodeRef(1, 1, runtime_seconds=1500)],
    )

    assert plan.items[0].proposed_episode is None
    assert plan.items[0].disposition == "review-unmatched"


def test_plan_contains_redacted_ids_not_paths():
    plan = plan_unmatched(
        "Faerie Tale Theatre",
        [
            FileEvidence(
                "disc-03-title-001",
                3000,
                {"S02E01": 0.95},
            )
        ],
        [EpisodeRef(2, 1, title="Story", runtime_seconds=3000)],
    )
    serialized = str(plan.to_dict())

    assert "disc-03-title-001" in serialized
    assert "G:\\" not in serialized
