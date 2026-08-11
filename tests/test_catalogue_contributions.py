from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend import catalogue_worker
from mkv_episode_matcher.backend.catalogue_worker import CatalogueContributionWorker
from mkv_episode_matcher.disc.catalogue_contributions import (
    CatalogueContributionError,
    CatalogueContributionStore,
    PendingContribution,
    snapshot_from_inventory,
)
from mkv_episode_matcher.disc.preflight import (
    DiscInventory,
    MakeMKVDrive,
    MakeMKVTitle,
)
from mkv_episode_matcher.disc.ripweaver_catalogue import ContributionReceipt
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact

CONTENT_HASH = "8B6FCE0775F77E41B1EB2E293BA9BA80"
FINGERPRINT = "0123456789abcdef"


def inventory(*, unsafe_source: str | None = None) -> DiscInventory:
    return DiscInventory(
        drive=MakeMKVDrive(
            index=0,
            visible=2,
            enabled=999,
            flags=1,
            drive_name="<hardware-redacted>",
            disc_name="Synthetic",
            device_name="<device-redacted>",
        ),
        disc_attributes={},
        titles=[
            MakeMKVTitle(
                index=index,
                attributes={
                    9: "0:22:00",
                    11: str(2_000_000_000 + index),
                    16: unsafe_source
                    if index == 1 and unsafe_source
                    else f"{index:05d}.mpls",
                    26: f"{index:05d}",
                },
            )
            for index in (1, 2)
        ],
        return_code=0,
        started_at="2026-08-10T00:00:00+00:00",
        finished_at="2026-08-10T00:00:01+00:00",
        warnings=[],
    )


def add_history(
    store: PipelineQueueStore,
    tmp_path,
    *,
    title_index: int,
    episode_number: int,
    identification_order: list[str],
) -> None:
    media_id = f"media-{title_index}"
    rip = tmp_path / f"rip-{title_index}.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": FINGERPRINT,
            "title_index": title_index,
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", rip))
    assert store.claim_next(allowed_stages=("identify",)).media_id == media_id
    episode_id = f"S01E{episode_number:02d}"
    identified = tmp_path / f"identified-{title_index}.json"
    identified.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "episode_id": episode_id,
            "library_relative": (
                "Synthetic Series/Season 01/"
                f"Synthetic Series - {episode_id} - Story {episode_number}.mkv"
            ),
            "identification_order": identification_order,
        }),
        encoding="utf-8",
    )
    store.complete_stage(media_id, "identify", build_artifact("identify", identified))


def test_outbox_sends_cumulative_matches_and_supersedes_stale_partial_payloads(
    tmp_path,
) -> None:
    pipeline_store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    outbox = CatalogueContributionStore(tmp_path / "contributions.sqlite3")
    snapshot = snapshot_from_inventory(
        content_hash=CONTENT_HASH,
        disc_fingerprint=FINGERPRINT,
        media_type="bluray",
        release_name="Synthetic Disc",
        inventory=inventory(),
        selected_title_indexes=(1, 2),
    )
    outbox.record_snapshot(snapshot)
    assert outbox.prepare_ready(pipeline_store) == 0

    add_history(
        pipeline_store,
        tmp_path,
        title_index=1,
        episode_number=1,
        identification_order=["tv-local"],
    )
    assert outbox.prepare_ready(pipeline_store) == 1
    first_pending = outbox.pending()[0]
    assert [title["title_index"] for title in first_pending.payload["titles"]] == [1]

    add_history(
        pipeline_store,
        tmp_path,
        title_index=2,
        episode_number=2,
        identification_order=["manual-playback-review"],
    )
    assert outbox.prepare_ready(pipeline_store) == 1

    pending = outbox.pending()[0]
    assert pending.payload["schema_version"] == 2
    assert pending.payload["content_hash"] == CONTENT_HASH
    titles = pending.payload["titles"]
    assert [title["match_source"] for title in titles] == [
        "local_evidence",
        "manual_playback",
    ]
    assert [title["episode_title"] for title in titles] == ["Story 1", "Story 2"]
    serialized = json.dumps(pending.payload)
    assert str(tmp_path) not in serialized
    assert "library_relative" not in serialized

    outbox.mark_sent(pending.payload_sha256)
    assert outbox.status() == {
        "snapshots": 1,
        "pending": 0,
        "sent": 1,
        "superseded": 1,
    }
    assert outbox.prepare_ready(pipeline_store) == 0


def test_snapshot_rejects_source_paths(tmp_path) -> None:
    with pytest.raises(CatalogueContributionError, match="source identifier"):
        snapshot_from_inventory(
            content_hash=CONTENT_HASH,
            disc_fingerprint=FINGERPRINT,
            media_type="bluray",
            release_name=None,
            inventory=inventory(unsafe_source=r"C:\BDMV\00001.mpls"),
            selected_title_indexes=(1,),
        )


def test_worker_is_inert_without_consent_and_sends_one_pending_item(
    monkeypatch,
) -> None:
    payload_sha256 = "a" * 64
    pending = PendingContribution(
        payload_sha256=payload_sha256,
        content_hash=CONTENT_HASH,
        payload={"schema_version": 2, "content_hash": CONTENT_HASH},
        attempt_count=0,
    )

    class Store:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def prepare_ready(self, _pipeline_store) -> int:
            return 0

        def pending(self, *, limit: int):
            assert limit == 1
            return (pending,)

        def mark_sent(self, digest: str) -> None:
            self.sent.append(digest)

        def mark_failed(self, _digest: str, *, error_type: str) -> None:
            raise AssertionError(error_type)

    class Client:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "https://api.ripweaver.com"

        def capabilities(self):
            return SimpleNamespace(compatible=True)

        def contribute(self, payload, **kwargs) -> ContributionReceipt:
            assert payload is pending.payload
            assert kwargs["token"] == "private-token"
            return ContributionReceipt(
                submission_id="submission",
                content_hash=CONTENT_HASH,
                payload_sha256=payload_sha256,
                status="accepted",
                confirmed_items=0,
                unresolved_items=2,
            )

    enabled = SimpleNamespace(
        ripweaver_catalogue_enabled=True,
        ripweaver_catalogue_contributions_enabled=True,
        ripweaver_catalogue_url="https://api.ripweaver.com",
    )
    config = SimpleNamespace(load=lambda: enabled)
    monkeypatch.setattr(catalogue_worker, "get_config_manager", lambda: config)
    monkeypatch.setattr(
        catalogue_worker,
        "load_environment_settings",
        lambda: SimpleNamespace(ripweaver_catalogue_token="private-token"),
    )
    monkeypatch.setattr(catalogue_worker, "RipWeaverCatalogueClient", Client)
    store = Store()
    worker = CatalogueContributionWorker(store, object())
    assert worker.run_once() is True
    assert store.sent == [payload_sha256]

    enabled.ripweaver_catalogue_contributions_enabled = False
    store.sent.clear()
    assert worker.run_once() is False
    assert store.sent == []
