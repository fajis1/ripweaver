from mkv_episode_matcher.media.play_all_detection import (
    MatchedEpisodeEvidence,
    detect_play_all,
)


def _episode(index: int, *, season: int = 1, duration: float = 1200):
    return MatchedEpisodeEvidence(
        file_id=f"title-{index:03d}",
        season=season,
        episode=index + 1,
        duration_seconds=duration,
        size_bytes=2_500_000_000,
    )


def test_detects_large_unmatched_play_all_after_contiguous_episode_matches():
    evidence = detect_play_all(
        candidate_file_id="title-004",
        candidate_duration_seconds=4_790,
        candidate_size_bytes=9_800_000_000,
        matched_episodes=tuple(_episode(index) for index in range(4)),
    )

    assert evidence is not None
    assert evidence.component_episode_ids == (
        "S01E01",
        "S01E02",
        "S01E03",
        "S01E04",
    )
    assert evidence.duration_ratio == 4_790 / 4_800
    assert evidence.size_ratio == 9_800_000_000 / 10_000_000_000


def test_does_not_classify_unmatched_file_when_runtime_or_episode_order_conflicts():
    assert (
        detect_play_all(
            candidate_file_id="aggregate",
            candidate_duration_seconds=3_200,
            candidate_size_bytes=9_800_000_000,
            matched_episodes=tuple(_episode(index) for index in range(4)),
        )
        is None
    )
    assert (
        detect_play_all(
            candidate_file_id="aggregate",
            candidate_duration_seconds=4_800,
            candidate_size_bytes=10_000_000_000,
            matched_episodes=(_episode(0), _episode(2)),
        )
        is None
    )
