import pytest

from mkv_episode_matcher.disc.content_policy import (
    identification_order,
    infer_release_name_from_disc_label,
    infer_tv_context_from_disc_label,
    parse_tv_disc_label_context,
)


@pytest.mark.parametrize(
    ("hint", "expected_first"),
    [
        (None, "mixed-classifier"),
        ("tv", "tv"),
        ("movie", "movie"),
        ("extras", "extras"),
        ("mixed", "mixed-classifier"),
    ],
)
def test_hint_changes_priority_without_removing_fallbacks(hint, expected_first):
    order = identification_order(hint)

    assert order[0] == expected_first
    assert {"tv", "movie", "extras"}.issubset(set(order))


def test_unknown_hint_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        identification_order("music")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (
            "Dragons Race to the Edge Season 1 DVD2",
            ("Dragons Race to the Edge", 1),
        ),
        ("FAERIE_TALE_THEATRE_SEASON_4_DISC_2", ("FAERIE TALE THEATRE", 4)),
        ("Show Name - Season 12", ("Show Name", 12)),
        (
            "THE OFFICE SUPERFAN EPISODES S4 D3",
            ("THE OFFICE SUPERFAN EPISODES", 4),
        ),
        ("Show Name S04D03", ("Show Name", 4)),
        ("Show Name DVD2", None),
        ("Show Name Volume 3", None),
        (None, None),
    ],
)
def test_explicit_season_label_context(label, expected):
    assert infer_tv_context_from_disc_label(label) == expected


@pytest.mark.parametrize(
    ("label", "season", "disc_number"),
    [
        ("Show S4 D3", 4, 3),
        ("Show S04 D03", 4, 3),
        ("Show S04D03", 4, 3),
        ("Show Season 4 Disc 3", 4, 3),
        ("Show Season 4 DVD3", 4, 3),
    ],
)
def test_structured_tv_disc_label_shorthand(label, season, disc_number):
    context = parse_tv_disc_label_context(label)

    assert context is not None
    assert context.series_hint == "Show"
    assert context.season == season
    assert context.disc_number == disc_number


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("FAERIE_TALE_THEATRE_5", "FAERIE TALE THEATRE"),
        ("The_Flintstones_CSR_DIM1", "The Flintstones"),
        (
            "Dragons Race to the Edge Season 1 DVD2",
            "Dragons Race to the Edge Season 1",
        ),
        (
            "PARENT_TRAP_1961_PARENT_TRAP_II",
            "PARENT TRAP 1961 PARENT TRAP II",
        ),
        (None, None),
    ],
)
def test_release_name_from_disc_label_preserves_content_identity(label, expected):
    assert infer_release_name_from_disc_label(label) == expected
