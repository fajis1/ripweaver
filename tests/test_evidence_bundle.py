import json

import pytest

from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.evidence_bundle import (
    EvidenceBundleError,
    SavedFileEvidence,
    SavedTranscriptWindow,
    build_transient_evidence_bundle,
    load_episode_catalog,
    load_saved_transcript_evidence,
    merge_saved_transcript_evidence,
    select_transcript_excerpts,
    validate_new_output_paths,
    write_merged_transcript_evidence,
    write_safe_evidence_plan,
    write_transient_bundle,
)


def _catalog():
    return (
        EpisodeCatalogEntry(
            "S01E01",
            1,
            1,
            "The Snow Queen",
            "A goblin mirror separates two childhood friends.",
            2940,
        ),
        EpisodeCatalogEntry(
            "S01E02",
            1,
            2,
            "The Pied Piper",
            "A mysterious piper removes the rats from Hamelin.",
            2880,
        ),
        EpisodeCatalogEntry(
            "S01E03",
            1,
            3,
            "Cinderella",
            "A mistreated young woman attends a royal ball.",
            3000,
        ),
    )


def _files():
    return (
        SavedFileEvidence(
            "disc-01-title-000",
            2925,
            (
                SavedTranscriptWindow(60, "Hello there."),
                SavedTranscriptWindow(
                    300,
                    "An evil goblin made a mirror and two childhood friends "
                    "were separated by the Snow Queen.",
                ),
                SavedTranscriptWindow(
                    900,
                    "The frozen palace was far to the north beyond the river.",
                ),
            ),
        ),
    )


def test_load_explicit_saved_reports(tmp_path):
    transcript_path = tmp_path / "transcripts.json"
    catalog_path = tmp_path / "catalog.json"
    transcript_path.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-01-title-000",
                    "duration_seconds": 2925,
                    "windows": [
                        {
                            "start_seconds": 300,
                            "text": "The Snow Queen captured a childhood friend.",
                        }
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )
    catalog_path.write_text(
        json.dumps({"episodes": [item.to_dict() for item in _catalog()]}),
        encoding="utf-8",
    )

    files = load_saved_transcript_evidence(transcript_path)
    catalog = load_episode_catalog(catalog_path)

    assert files[0].file_id == "disc-01-title-000"
    assert catalog == _catalog()


def test_excerpt_selection_prefers_information_and_deduplicates():
    windows = (
        SavedTranscriptWindow(0, "Hello."),
        SavedTranscriptWindow(
            60,
            "A princess secretly crossed the forest to attend the royal ball.",
        ),
        SavedTranscriptWindow(
            120,
            "A princess secretly crossed the forest to attend the royal ball!",
        ),
        SavedTranscriptWindow(
            180,
            "The glass slipper was left behind when midnight arrived.",
        ),
    )

    excerpts = select_transcript_excerpts(windows, maximum_excerpts=2)

    assert len(excerpts) == 2
    assert any("royal ball" in excerpt for excerpt in excerpts)
    assert any("glass slipper" in excerpt for excerpt in excerpts)


def test_bundle_contains_dialogue_but_safe_plan_does_not(tmp_path):
    bundle, plan = build_transient_evidence_bundle(_files(), _catalog(), top_k=2)
    bundle_path = tmp_path / "private-bundle.json"
    report_path = tmp_path / "safe-plan.json"

    validate_new_output_paths(bundle_path, report_path)
    write_transient_bundle(bundle_path, bundle)
    write_safe_evidence_plan(report_path, plan)

    bundle_text = bundle_path.read_text(encoding="utf-8")
    report_text = report_path.read_text(encoding="utf-8")
    assert "evil goblin" in bundle_text
    assert "evil goblin" not in report_text
    assert "transcript" not in report_text.lower()
    assert plan.files[0].candidates[0].episode_id == "S01E01"
    assert plan.shortlisted_episode_count >= 1


def test_shortlist_keeps_enough_candidates_for_disc_wide_assignment():
    catalog = tuple(
        EpisodeCatalogEntry(
            f"S01E{number:02d}",
            1,
            number,
            f"Story {number}",
            f"An overview for story {number}.",
            3000,
        )
        for number in range(1, 13)
    )
    files = tuple(
        SavedFileEvidence(
            f"disc-01-title-{number:03d}",
            3000,
            (
                SavedTranscriptWindow(
                    60,
                    f"This is distinctive dialogue about story {number + 1}.",
                ),
            ),
        )
        for number in range(12)
    )

    _bundle, plan = build_transient_evidence_bundle(files, catalog, top_k=3)

    assert plan.shortlisted_episode_count == 12
    assert all(len(item.candidates) == 3 for item in plan.files)


def test_local_paths_in_transcript_are_rejected(tmp_path):
    path = tmp_path / "transcripts.json"
    path.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-01-title-000",
                    "duration_seconds": 100,
                    "windows": [
                        {
                            "start_seconds": 0,
                            "text": "Loaded from G:\\private\\episode.mkv",
                        }
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceBundleError, match="local path"):
        load_saved_transcript_evidence(path)


def test_audio_review_status_is_rejected_before_bundle_creation(tmp_path):
    path = tmp_path / "transcripts.json"
    path.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-01-title-000",
                    "duration_seconds": 100,
                    "status": "review-audio",
                    "windows": [
                        {
                            "start_seconds": 0,
                            "text": "Weak transcript",
                        }
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceBundleError, match="requiring audio review"):
        load_saved_transcript_evidence(path)
    assert load_saved_transcript_evidence(path, skip_review_files=True) == ()


def test_private_reports_merge_with_redacted_prefix_filter(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    merged_path = tmp_path / "merged.json"
    first.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "dragons-control",
                    "duration_seconds": 100,
                    "status": "collected",
                    "windows": [{"start_seconds": 0, "text": "Dragon dialogue"}],
                },
                {
                    "file_id": "theatre-disc03-title000",
                    "duration_seconds": 200,
                    "status": "collected",
                    "windows": [{"start_seconds": 0, "text": "Goblin dialogue"}],
                },
            ]
        }),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "theatre-disc04-title000",
                    "duration_seconds": 300,
                    "status": "collected",
                    "windows": [{"start_seconds": 0, "text": "Mirror dialogue"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    files = merge_saved_transcript_evidence(
        (first, second),
        file_id_prefix="theatre-",
    )
    write_merged_transcript_evidence(merged_path, files)

    assert [item.file_id for item in files] == [
        "theatre-disc03-title000",
        "theatre-disc04-title000",
    ]
    serialized = merged_path.read_text(encoding="utf-8")
    assert "Dragon dialogue" not in serialized
    assert "Goblin dialogue" in serialized


def test_private_report_enrichment_combines_duplicate_id_windows(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    base = {
        "file_id": "theatre-disc03-title000",
        "duration_seconds": 200,
        "status": "collected",
    }
    first.write_text(
        json.dumps({
            "files": [
                {
                    **base,
                    "windows": [{"start_seconds": 300, "text": "Middle dialogue"}],
                }
            ]
        }),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({
            "files": [
                {
                    **base,
                    "windows": [{"start_seconds": 60, "text": "Story introduction"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    files = merge_saved_transcript_evidence(
        (first, second),
        enrich_duplicates=True,
    )

    assert [window.start_seconds for window in files[0].windows] == [60, 300]


def test_private_report_enrichment_maps_reviewed_redacted_id(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "theatre-disc03-title001",
                    "duration_seconds": 200,
                    "status": "collected",
                    "windows": [{"start_seconds": 300, "text": "Middle dialogue"}],
                }
            ]
        }),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-03-title-001",
                    "duration_seconds": 200,
                    "status": "collected",
                    "windows": [{"start_seconds": 120, "text": "Story dialogue"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    files = merge_saved_transcript_evidence(
        (first, second),
        enrich_duplicates=True,
        file_id_map={"disc-03-title-001": "theatre-disc03-title001"},
    )

    assert len(files) == 1
    assert files[0].file_id == "theatre-disc03-title001"
    assert [window.start_seconds for window in files[0].windows] == [120, 300]


def test_private_report_mapping_rejects_absent_source(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "theatre-disc03-title001",
                    "duration_seconds": 200,
                    "status": "collected",
                    "windows": [{"start_seconds": 120, "text": "Story dialogue"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceBundleError, match="absent source"):
        merge_saved_transcript_evidence(
            (source,),
            file_id_map={"missing-id": "theatre-disc03-title001"},
        )


def test_output_preflight_refuses_collision_or_same_path(tmp_path):
    existing = tmp_path / "existing.json"
    existing.write_text("{}", encoding="utf-8")

    with pytest.raises(EvidenceBundleError, match="exists"):
        validate_new_output_paths(existing, tmp_path / "new.json")
    with pytest.raises(EvidenceBundleError, match="different"):
        validate_new_output_paths(tmp_path / "same.json", tmp_path / "same.json")
