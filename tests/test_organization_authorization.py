import json

from mkv_episode_matcher.backend.organization_authorization import (
    build_organization_authorization_plan,
)
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


def _artifact(tmp_path, media_id, stage, payload):
    path = tmp_path / f"{media_id}.{stage}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return build_artifact(stage, path)


def _queued_organization(tmp_path, *, destination_exists=False):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    media_id = "disc-01-title-000"
    store.enqueue_verified_rip(
        media_id,
        _artifact(tmp_path, media_id, "rip", {"mode": "verified-rip-contract"}),
    )
    store.claim_next()
    store.complete_stage(
        media_id,
        "identify",
        _artifact(
            tmp_path,
            media_id,
            "identify",
            {
                "mode": "identified-episode-contract",
                "library_relative": "Movie/Extras/Feature.mkv",
            },
        ),
    )
    store.claim_next()
    store.complete_stage(
        media_id,
        "transcode",
        _artifact(
            tmp_path,
            media_id,
            "transcode",
            {
                "mode": "verified-transcode-contract",
                "library_relative": "Movie/Extras/Feature.mkv",
                "episode_id": None,
                "encoded_height": 1080,
                "encoded_field_order": "progressive",
            },
        ),
    )
    movie_root = tmp_path / "movies"
    tv_root = tmp_path / "tv"
    movie_root.mkdir()
    tv_root.mkdir()
    if destination_exists:
        destination = movie_root / "Movie" / "Extras" / "Feature - 1080p.mkv"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing")
    return store, Config(jellyfin_movie_root=movie_root, jellyfin_tv_root=tv_root)


def test_organization_preview_is_path_redacted_and_collision_free(tmp_path):
    store, config = _queued_organization(tmp_path)

    plan = build_organization_authorization_plan(store, config)
    public = plan.public_dict()

    assert public["item_count"] == 1
    assert public["movie_count"] == 1
    assert public["collision_count"] == 0
    assert public["items"] == [
        {
            "media_id": "disc-01-title-000",
            "destination_relative": "Movie/Extras/Feature - 1080p.mkv",
            "kind": "movie",
            "collision": False,
        }
    ]
    assert str(tmp_path) not in json.dumps(public)


def test_organization_preview_reports_existing_destination(tmp_path):
    store, config = _queued_organization(tmp_path, destination_exists=True)

    plan = build_organization_authorization_plan(store, config)

    assert plan.collision_media_ids == ("disc-01-title-000",)


def test_organization_preview_allows_other_episode_resolution_but_not_same_version(
    tmp_path,
):
    _unused, config = _queued_organization(tmp_path)
    payload = {
        "mode": "verified-transcode-contract",
        "episode_id": "S01E01",
        "library_relative": "Show/Season 01/Show - S01E01 - First.mkv",
        "encoded_height": 1080,
        "encoded_field_order": "progressive",
    }
    store = PipelineQueueStore(tmp_path / "tv-pipeline.sqlite3")
    store.enqueue_verified_rip(
        "disc-01-title-000",
        _artifact(tmp_path, "tv-media", "rip", {"mode": "verified-rip-contract"}),
    )
    store.claim_next()
    store.complete_stage(
        "disc-01-title-000",
        "identify",
        _artifact(
            tmp_path,
            "tv-media",
            "identify",
            {
                "mode": "identified-episode-contract",
                "library_relative": "Show/Season 01/Show - S01E01 - First.mkv",
            },
        ),
    )
    store.claim_next()
    store.complete_stage(
        "disc-01-title-000",
        "transcode",
        _artifact(tmp_path, "tv-media", "transcode", payload),
    )
    config.automatic_organization_enabled = True
    season = config.jellyfin_tv_root / "Show" / "Season 01"
    season.mkdir(parents=True)
    (season / "Show - S01E01 - Existing - 480p.mkv").write_bytes(b"480")

    assert (
        build_organization_authorization_plan(store, config).collision_media_ids == ()
    )

    (season / "Show - S01E01 - Renamed - 1080p.mkv").write_bytes(b"1080")
    assert build_organization_authorization_plan(store, config).collision_media_ids == (
        "disc-01-title-000",
    )
