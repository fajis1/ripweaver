import json

import pytest

from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.evidence_bundle import (
    SavedFileEvidence,
    SavedTranscriptWindow,
)
from mkv_episode_matcher.media.sequence_matcher import (
    SequenceGroup,
    SequenceMatchError,
    parse_sequence_group_specs,
    plan_disc_sequences,
    write_safe_sequence_plan,
)


def _episode(number, title):
    return EpisodeCatalogEntry(
        f"S01E{number:02d}",
        1,
        number,
        title,
        f"A story about {title}.",
        1800,
    )


def _file(file_id, text):
    return SavedFileEvidence(
        file_id,
        1800,
        (SavedTranscriptWindow(120, text),),
    )


def test_parser_preserves_explicit_group_and_title_order():
    groups = parse_sequence_group_specs((
        "earlier=disc05-title000,disc05-title001",
        "later=disc04-title000,disc04-title001",
    ))

    assert [group.group_id for group in groups] == ["earlier", "later"]
    assert groups[0].file_ids == ("disc05-title000", "disc05-title001")


def test_contiguous_sequence_overrides_one_misleading_independent_top():
    catalog = (
        _episode(1, "Apple"),
        _episode(2, "Banana"),
        _episode(3, "Castle"),
        _episode(4, "Dragon"),
        _episode(5, "Elephant"),
    )
    files = (
        _file("disc-title000", "A story about Castle."),
        _file("disc-title001", "A story about Banana."),
        _file("disc-title002", "A story about Elephant."),
    )

    plan = plan_disc_sequences(
        files,
        catalog,
        (SequenceGroup("disc", tuple(item.file_id for item in files)),),
        automatic_margin=0,
    )

    assert [item.proposed_episode for item in plan.groups[0].items] == [
        "S01E03",
        "S01E04",
        "S01E05",
    ]
    assert plan.groups[0].items[1].independent_top_episode != "S01E04"


def test_group_order_allows_gaps_but_prevents_episode_reuse():
    catalog = tuple(_episode(number, f"Story {number}") for number in range(1, 7))
    files = (
        _file("early-0", "Story 1"),
        _file("early-1", "Story 2"),
        _file("late-0", "Story 5"),
        _file("late-1", "Story 6"),
    )
    groups = (
        SequenceGroup("early", ("early-0", "early-1")),
        SequenceGroup("late", ("late-0", "late-1")),
    )

    plan = plan_disc_sequences(files, catalog, groups, automatic_margin=0)

    assignments = [
        item.proposed_episode for group in plan.groups for item in group.items
    ]
    assert assignments == ["S01E01", "S01E02", "S01E05", "S01E06"]
    assert len(set(assignments)) == 4


def test_weak_sequence_margin_routes_plan_to_review():
    catalog = tuple(_episode(number, "Same Story") for number in range(1, 5))
    files = (
        _file("disc-0", "Same Story"),
        _file("disc-1", "Same Story"),
    )

    plan = plan_disc_sequences(
        files,
        catalog,
        (SequenceGroup("disc", ("disc-0", "disc-1")),),
    )

    assert plan.disposition == "review-ambiguous"
    assert plan.global_margin == 0


def test_groups_must_cover_saved_evidence_exactly():
    with pytest.raises(SequenceMatchError, match="cover every"):
        plan_disc_sequences(
            (_file("disc-0", "Apple"), _file("disc-1", "Banana")),
            (_episode(1, "Apple"), _episode(2, "Banana")),
            (SequenceGroup("disc", ("disc-0", "missing")),),
        )


def test_safe_report_contains_no_dialogue_and_refuses_overwrite(tmp_path):
    plan = plan_disc_sequences(
        (_file("disc-0", "Private Apple dialogue"), _file("disc-1", "Banana")),
        (_episode(1, "Apple"), _episode(2, "Banana")),
        (SequenceGroup("disc", ("disc-0", "disc-1")),),
        automatic_margin=0,
    )
    report = tmp_path / "plan.json"

    write_safe_sequence_plan(report, plan)

    serialized = report.read_text(encoding="utf-8")
    assert "Private Apple dialogue" not in serialized
    assert json.loads(serialized)["mode"] == "saved-disc-sequence-plan"
    with pytest.raises(SequenceMatchError, match="refusing overwrite"):
        write_safe_sequence_plan(report, plan)


def test_theatre_disc_sequence_regression_resolves_ambiguous_files():
    episode_data = (
        ("S03E01", 3, 1, "Goldilocks and the Three Bears"),
        ("S03E02", 3, 2, "The Princess and the Pea"),
        ("S03E03", 3, 3, "Pinocchio"),
        ("S03E04", 3, 4, "Thumbelina"),
        ("S03E05", 3, 5, "Snow White and the Seven Dwarfs"),
        ("S03E06", 3, 6, "Beauty and the Beast"),
        (
            "S03E07",
            3,
            7,
            "The Boy Who Left Home to Find Out About the Shivers",
        ),
        ("S04E01", 4, 1, "The Three Little Pigs"),
        ("S04E02", 4, 2, "The Snow Queen"),
        ("S04E03", 4, 3, "The Pied Piper of Hamelin"),
        ("S04E04", 4, 4, "Cinderella"),
        ("S04E05", 4, 5, "Puss in Boots"),
    )
    catalog = tuple(
        EpisodeCatalogEntry(
            episode_id,
            season,
            episode,
            title,
            f"A story about {title}.",
            3000,
        )
        for episode_id, season, episode, title in episode_data
    )
    file_ids = (
        "theatre-disc05-title000",
        "theatre-disc05-title001",
        "theatre-disc05-title002",
        "theatre-disc05-title003",
        "theatre-disc04-title000",
        "theatre-disc04-title001",
        "theatre-disc04-title002",
        "theatre-disc04-title003",
        "theatre-disc03-title000",
        "theatre-disc03-title001",
        "theatre-disc03-title002",
        "theatre-disc03-title003",
    )
    files = tuple(
        SavedFileEvidence(
            file_id,
            3000,
            (
                SavedTranscriptWindow(
                    120,
                    (
                        "Evidence for Beauty and the Beast."
                        if file_id
                        in {
                            "theatre-disc04-title000",
                            "theatre-disc03-title001",
                        }
                        else f"Evidence for {episode_data[index][3]}."
                    ),
                ),
            ),
        )
        for index, file_id in enumerate(file_ids)
    )
    groups = (
        SequenceGroup("disc05", file_ids[0:4]),
        SequenceGroup("disc04", file_ids[4:8]),
        SequenceGroup("disc03", file_ids[8:12]),
    )

    plan = plan_disc_sequences(
        files,
        catalog,
        groups,
        automatic_margin=0,
    )

    assert [item.proposed_episode for group in plan.groups for item in group.items] == [
        item[0] for item in episode_data
    ]
    assert (
        plan.groups[1].items[0].independent_top_episode
        != plan.groups[1].items[0].proposed_episode
    )
    assert (
        plan.groups[2].items[1].independent_top_episode
        != plan.groups[2].items[1].proposed_episode
    )
