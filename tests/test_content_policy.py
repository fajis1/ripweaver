import pytest

from mkv_episode_matcher.disc.content_policy import (
    identification_order,
    infer_tv_context_from_disc_label,
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
        ("Show Name DVD2", None),
        ("Show Name Volume 3", None),
        (None, None),
    ],
)
def test_explicit_season_label_context(label, expected):
    assert infer_tv_context_from_disc_label(label) == expected
