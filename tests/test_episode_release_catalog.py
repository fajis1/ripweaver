import json

from mkv_episode_matcher.backend.routers.rip import (
    ApplyEpisodeReleaseRequest,
    apply_episode_release,
)
from mkv_episode_matcher.disc.episode_release_catalog import (
    catalog_by_id,
    match_episode_release,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


def test_faerie_volume_four_matches_only_complete_reviewed_title_set():
    catalog = match_episode_release("FAERIE_TALE_THEATRE_4", (0, 1, 2, 3))

    assert catalog is not None
    assert catalog.series_name == "Faerie Tale Theatre"
    assert [(item.season, item.episode) for item in catalog.assignments] == [
        (3, 5),
        (3, 6),
        (3, 7),
        (4, 1),
    ]
    assert match_episode_release("FAERIE_TALE_THEATRE_4", (0, 1, 2)) is None
    assert catalog_by_id(catalog.catalog_id) == catalog


def test_reviewed_release_repairs_exact_held_fingerprint_without_media_read(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "0123456789abcdef"
    for title_index in range(4):
        media_id = f"disc-04-{fingerprint}-title-{title_index:03d}"
        contract = contracts / f"{media_id}.verified-rip.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(tmp_path / f"source-{title_index}.mkv"),
                "source_size_bytes": 10,
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {"series_name": "Unmatched", "season": None},
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        claimed = store.claim_next()
        assert claimed is not None and claimed.media_id == media_id
        store.require_review(media_id, "missing_season_context")

    result = apply_episode_release(
        ApplyEpisodeReleaseRequest(
            disc_fingerprint=fingerprint,
            catalog_id="faerie-tale-theatre-volume-4-aired",
            confirm_apply=True,
        ),
        store,
        contracts,
    )

    assert result["queued_item_count"] == 4
    assert {item.state for item in store.list_items()} == {"queued"}
    for item in store.list_items():
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        assert payload["media_context"]["series_name"] == "Faerie Tale Theatre"
        assert len(payload["media_context"]["episode_assignments"]) == 4


def test_reviewed_release_repairs_only_newest_held_copy_per_title(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "0123456789abcdef"
    for generation in ("original", "recovery"):
        for title_index in range(4):
            media_id = f"disc-04-{fingerprint}-title-{title_index:03d}-{generation}"
            contract = contracts / f"{media_id}.verified-rip.json"
            contract.write_text(
                json.dumps({
                    "mode": "verified-rip-contract",
                    "media_id": media_id,
                    "source_path": str(tmp_path / f"{media_id}.mkv"),
                    "source_size_bytes": 10,
                    "disc_fingerprint": fingerprint,
                    "title_index": title_index,
                    "media_context": {"series_name": "Unmatched", "season": None},
                }),
                encoding="utf-8",
            )
            store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
            claimed = store.claim_next()
            assert claimed is not None and claimed.media_id == media_id
            store.require_review(media_id, "unmatched_disc_analysis_required")

    result = apply_episode_release(
        ApplyEpisodeReleaseRequest(
            disc_fingerprint=fingerprint,
            catalog_id="faerie-tale-theatre-volume-4-aired",
            confirm_apply=True,
        ),
        store,
        contracts,
    )

    assert result["queued_item_count"] == 4
    queued = [item for item in store.list_items() if item.state == "queued"]
    held = [item for item in store.list_items() if item.state == "review_required"]
    assert len(queued) == 4
    assert len(held) == 4
    assert all(item.media_id.endswith("-recovery") for item in queued)
