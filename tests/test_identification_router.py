from mkv_episode_matcher.backend.identification_router import (
    remaining_routes,
    route_order,
)


def test_tv_hint_cycles_each_content_family_once():
    assert [route.branch for route in route_order("tv")] == [
        "tv",
        "movie-bonus",
        "gemini-synthesis",
    ]


def test_movie_hint_starts_descriptive_and_never_repeats_attempted_branch():
    assert [route.branch for route in remaining_routes("movie", {"movie-bonus"})] == [
        "tv",
        "gemini-synthesis",
    ]
