import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.disc import rip_manifest
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import CommandResult, PreflightError
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.ripweaver_catalogue import (
    CatalogueUsage,
    RipWeaverCatalogueSupportRequiredError,
    SupportPolicy,
)
from mkv_episode_matcher.disc.thediscdb import TheDiscDbResolution
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


def test_existing_rip_recovery_media_id_normalizes_makemkv_spaces():
    candidate = SimpleNamespace(
        basename="Synthetic Show Complete Series Disc 8_t00.mkv",
        candidate_id="a" * 64,
    )

    assert rip._existing_rip_recovery_media_id(candidate) == (
        "Synthetic-Show-Complete-Series-Disc-8_t00-recovery-" + "a" * 16
    )


def test_pipeline_display_name_uses_pending_episode_assignment(tmp_path):
    contract = tmp_path / "identify.json"
    contract.write_text(
        """{
  "title_index": 18,
  "media_context": {
    "series_name": "The Flintstones",
    "episode_assignments": [
      {"title_index": 18, "season": 2, "episode": 1, "title": "The Hit Song Writers"}
    ]
  }
}
""",
        encoding="utf-8",
    )
    item = SimpleNamespace(artifact=SimpleNamespace(contract_path=contract))

    assert (
        rip._pipeline_item_display_name(item)
        == "The Flintstones - S02E01 - The Hit Song Writers"
    )


def test_pipeline_display_name_omits_legacy_untitled_placeholder(tmp_path):
    contract = tmp_path / "identify.json"
    contract.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "episode_id": "S06E02",
            "library_relative": (
                "Test Show/Season 06/Test Show - S06E02 - Untitled.mkv"
            ),
        }),
        encoding="utf-8",
    )
    item = SimpleNamespace(artifact=SimpleNamespace(contract_path=contract))

    assert rip._pipeline_item_display_name(item) == "Test Show - S06E02"


def test_pipeline_response_exposes_candidate_help_without_authorizing_it(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contract = tmp_path / "rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": "candidate-title",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 7,
            "media_context": {
                "series_name": "Example Series",
                "catalogue_help_assignments": [
                    {
                        "title_index": 7,
                        "season": 2,
                        "episode": 3,
                        "title": "Candidate Episode",
                        "identification_source": "ripweaver-catalogue-help",
                    }
                ],
            },
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip("candidate-title", build_artifact("rip", contract))
    store.claim_next()
    store.require_review("candidate-title", "catalogue_candidate_help_available")

    public = rip._pipeline_item_response(store.list_items()[0])

    assert public["catalogue_candidate_help"] == {
        "series_name": "Example Series",
        "season": 2,
        "episode": 3,
        "title": "Candidate Episode",
        "independent_support": 1,
        "automatic": False,
    }
    assert public["display_name"] is None


def test_drive_preparation_guard_refuses_duplicate_scan_and_releases():
    claimed = rip._claim_drive_preparation(17)
    try:
        with pytest.raises(
            HTTPException,
            match="Disc preparation scan is already active on optical drive 18",
        ):
            rip._claim_drive_preparation(17)
    finally:
        claimed.release()

    next_claim = rip._claim_drive_preparation(17)
    next_claim.release()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("MakeMKV executable not found: private", "Repair the MakeMKVCLI path"),
        ("MakeMKV info timed out after 30s", "timed out"),
        (
            "MakeMKV returned no optical-drive records",
            "did not report any optical drives",
        ),
        (
            "MakeMKV returned no optical-drive records; "
            "Windows-only drives remain provisional",
            "visible and safely locked",
        ),
    ],
)
def test_drive_refresh_errors_are_actionable_and_path_free(message, expected):
    detail = rip._safe_drive_refresh_error(PreflightError(message))

    assert expected in detail
    assert "private" not in detail


def test_disc_staged_mkv_plan_selects_only_exact_fingerprint_files(tmp_path):
    fingerprint = "0123456789abcdef"
    matching = (
        tmp_path
        / "TV Shows"
        / "Test Show"
        / "Unmatched"
        / f"Test-Show--disc-02-{fingerprint}-title-004.mkv"
    )
    matching.parent.mkdir(parents=True)
    matching.write_bytes(b"matching-media")
    other_disc = matching.with_name("Test-Show--disc-02-fedcba9876543210-title-004.mkv")
    other_disc.write_bytes(b"other-disc")
    generic = matching.with_name("episode.mkv")
    generic.write_bytes(b"generic")

    digest, candidates = rip._disc_staged_mkv_plan(tmp_path, fingerprint)

    assert len(digest) == 64
    assert candidates == (
        (
            matching.resolve(),
            matching.stat().st_size,
            matching.stat().st_mtime_ns,
            matching.relative_to(tmp_path).as_posix(),
        ),
    )


def test_disc_staged_mkv_plan_digest_changes_with_candidate_metadata(tmp_path):
    fingerprint = "0123456789abcdef"
    candidate = tmp_path / f"Series--disc-01-{fingerprint}-title-001.mkv"
    candidate.write_bytes(b"first")
    initial_digest, _ = rip._disc_staged_mkv_plan(tmp_path, fingerprint)

    candidate.write_bytes(b"changed-size")
    changed_digest, _ = rip._disc_staged_mkv_plan(tmp_path, fingerprint)

    assert changed_digest != initial_digest


def test_delete_disc_staged_mkvs_requires_exact_unchanged_preview(tmp_path):
    fingerprint = "0123456789abcdef"
    candidate = tmp_path / f"Series--disc-01-{fingerprint}-title-001.mkv"
    candidate.write_bytes(b"media")
    digest, _ = rip._disc_staged_mkv_plan(tmp_path, fingerprint)
    request = rip.ForgetDiscIdentityRequest(
        expected_disc_fingerprint=fingerprint,
        confirm_forget=True,
        delete_staged_media=True,
        expected_media_plan_sha256=digest,
        authorized_media_file_count=1,
        confirm_delete_staged_media=True,
    )

    assert rip._delete_disc_staged_mkvs(request, tmp_path) == 1
    assert not candidate.exists()


def test_delete_disc_staged_mkvs_refuses_stale_preview(tmp_path):
    fingerprint = "0123456789abcdef"
    candidate = tmp_path / f"Series--disc-01-{fingerprint}-title-001.mkv"
    candidate.write_bytes(b"media")
    request = rip.ForgetDiscIdentityRequest(
        expected_disc_fingerprint=fingerprint,
        confirm_forget=True,
        delete_staged_media=True,
        expected_media_plan_sha256="0" * 64,
        authorized_media_file_count=1,
        confirm_delete_staged_media=True,
    )

    with pytest.raises(HTTPException, match="review a fresh deletion preview"):
        rip._delete_disc_staged_mkvs(request, tmp_path)
    assert candidate.exists()


def _result(
    source: str, *, inventory: bool = False, label: str = "Test Disc"
) -> CommandResult:
    rows = [f'DRV:0,2,999,1,"private hardware","{label}","D:"']
    if inventory:
        for index in range(3):
            rows.extend([
                f'TINFO:{index},2,0,"Episode {index + 1}"',
                f'TINFO:{index},9,0,"0:22:0{index}"',
                f'TINFO:{index},11,0,"2000000000"',
                f'TINFO:{index},16,0,"0080{index}.mpls"',
                f'TINFO:{index},26,0,"{40 + index}"',
                f'TINFO:{index},27,0,"title_t{index:02d}.mkv"',
                f'SINFO:{index},0,1,0,"Video"',
                f'SINFO:{index},1,1,0,"Audio"',
                f'SINFO:{index},1,3,0,"eng"',
            ])
    return CommandResult(
        command=("makemkvcon64.exe", "info", source),
        return_code=0,
        stdout="\n".join(rows) + "\n",
        stderr="",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:00:01+00:00",
    )


def _substantial_review_result(
    source: str, *, inventory: bool = False
) -> CommandResult:
    rows = ['DRV:0,2,999,1,"private hardware","Example Series S7 D4","D:"']
    if inventory:
        durations = [120, 1935, 2725, 1980, 2001, 3570, 10, 1, 60, 25]
        sizes = [
            120_000_000,
            6_800_000_000,
            8_170_000_000,
            6_930_000_000,
            6_780_000_000,
            10_660_000_000,
            1_000_000,
            100_000,
            60_000_000,
            25_000_000,
        ]
        for index, (duration, size) in enumerate(zip(durations, sizes, strict=True)):
            rows.extend([
                f'TINFO:{index},2,0,"Title {index}"',
                f'TINFO:{index},9,0,"{duration // 3600}:{(duration % 3600) // 60:02d}:{duration % 60:02d}"',
                f'TINFO:{index},11,0,"{size}"',
                f'TINFO:{index},27,0,"title_t{index:02d}.mkv"',
                f'SINFO:{index},0,1,0,"Video"',
                f'SINFO:{index},1,1,0,"Audio"',
                f'SINFO:{index},1,3,0,"eng"',
            ])
    return CommandResult(
        command=("makemkvcon64.exe", "info", source),
        return_code=0,
        stdout="\n".join(rows) + "\n",
        stderr="",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:00:01+00:00",
    )


def _record_safe_pipeline_title(
    store: PipelineQueueStore,
    tmp_path,
    output_root,
    *,
    fingerprint: str,
    title_index: int,
    suffix: str,
) -> None:
    media_id = f"safe-title-{title_index}-{suffix}"
    source = output_root / f"{media_id}.mkv"
    source.write_bytes(f"synthetic-{title_index}".encode())
    contract = tmp_path / f"{media_id}.verified-rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": fingerprint,
            "title_index": title_index,
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))


def test_safe_pipeline_titles_include_verified_encoded_staging(tmp_path):
    fingerprint = "0123456789abcdef"
    media_id = "encoded-title"
    removed_rip = tmp_path / "removed-rip.mkv"
    rip_contract = tmp_path / f"{media_id}.verified-rip.json"
    rip_contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "source_path": str(removed_rip),
            "source_size_bytes": 128,
            "disc_fingerprint": fingerprint,
            "title_index": 1,
        }),
        encoding="utf-8",
    )
    identify_contract = tmp_path / f"{media_id}.identify.json"
    identify_contract.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "media_id": media_id,
            "source_path": str(removed_rip),
            "source_size_bytes": 128,
            "episode_id": "S08E01",
        }),
        encoding="utf-8",
    )
    encoded = tmp_path / "encoded.mkv"
    encoded.write_bytes(b"verified-encoded-media")
    transcode_contract = tmp_path / f"{media_id}.transcode.json"
    transcode_contract.write_text(
        json.dumps({
            "mode": "verified-transcode-contract",
            "media_id": media_id,
            "encoded_path": str(encoded),
            "encoded_size_bytes": encoded.stat().st_size,
            "original_source_path": str(removed_rip),
            "original_source_size_bytes": 128,
            "episode_id": "S08E01",
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", rip_contract))
    store.claim_next()
    store.complete_stage(
        media_id, "identify", build_artifact("identify", identify_contract)
    )
    store.claim_next()
    store.complete_stage(
        media_id, "transcode", build_artifact("transcode", transcode_contract)
    )

    assert store.get(media_id).stage == "organize"
    assert rip._safely_present_pipeline_title_indexes(store, fingerprint) == frozenset({
        1
    })
    assert (
        rip._pipeline_item_response(store.get(media_id))["pipeline_media_available"]
        is True
    )


def test_gemini_retry_starts_only_the_exact_requested_item(tmp_path, monkeypatch):
    requested = SimpleNamespace(
        media_id="disc-title-001",
        state="review_required",
        stage="identify",
        review_code="gemini_provider_failed",
    )
    unrelated = SimpleNamespace(
        media_id="disc-title-002",
        state="review_required",
        stage="identify",
        review_code="gemini_provider_failed",
    )

    class FakeStore:
        def __init__(self):
            self.items = {item.media_id: item for item in (requested, unrelated)}
            self.transitions = []

        def get(self, media_id):
            return self.items[media_id]

        def choose_review_path(self, media_id, code):
            self.transitions.append((media_id, code))
            self.items[media_id].review_code = code
            return self.items[media_id]

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target

        def start(self):
            return None

    store = FakeStore()
    monkeypatch.setattr(rip.threading, "Thread", FakeThread)

    response = rip.execute_pipeline_gemini_fallback(
        rip.GeminiFallbackExecutionRequest(
            media_ids=[requested.media_id],
            confirm_media_read=True,
            confirm_external_transmission=True,
        ),
        store,
        tmp_path / "contracts",
    )

    assert response == {"started": True, "item_count": 1}
    assert store.transitions == [(requested.media_id, "gemini_analysis_running")]
    assert unrelated.review_code == "gemini_provider_failed"


def test_staged_library_duplicate_plan_requires_both_exact_files(tmp_path):
    output_root = tmp_path / "rips"
    tv_root = tmp_path / "tv"
    staged = output_root / "Show" / "Season 01" / "episode.mkv"
    library = tv_root / "Show" / "Season 01" / "episode.mkv"
    staged.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    staged.write_bytes(b"staged")
    library.write_bytes(b"library")
    job = SimpleNamespace(
        preview={
            "jobs": [
                {
                    "collision_status": "final-exists",
                    "final_destination": "Show/Season 01/episode.mkv",
                    "prior_library_relative": "Show/Season 01/episode.mkv",
                    "prior_episode_id": "S01E01",
                }
            ]
        }
    )
    binding = SimpleNamespace(output_root=output_root)
    config = SimpleNamespace(jellyfin_tv_root=tv_root, jellyfin_movie_root=None)

    digest, entries = rip._staged_library_duplicate_plan(job, binding, config)

    assert len(digest) == 64
    assert entries == [(staged.resolve(), len(b"staged"))]


def test_staged_library_duplicate_plan_refuses_orphaned_staging(tmp_path):
    output_root = tmp_path / "rips"
    tv_root = tmp_path / "tv"
    staged = output_root / "Show" / "Season 01" / "episode.mkv"
    staged.parent.mkdir(parents=True)
    tv_root.mkdir()
    staged.write_bytes(b"staged")
    job = SimpleNamespace(
        preview={
            "jobs": [
                {
                    "collision_status": "final-exists",
                    "final_destination": "Show/Season 01/episode.mkv",
                    "prior_library_relative": "Show/Season 01/episode.mkv",
                    "prior_episode_id": "S01E01",
                }
            ]
        }
    )

    _digest, entries = rip._staged_library_duplicate_plan(
        job,
        SimpleNamespace(output_root=output_root),
        SimpleNamespace(jellyfin_tv_root=tv_root, jellyfin_movie_root=None),
    )

    assert entries == []
    assert staged.is_file()


def _bonus_result(source: str) -> CommandResult:
    rows = ['DRV:0,2,999,1,"private hardware","Bonus Disc","D:"']
    durations = [1123, 882, 95, 360, 220, 300, 1352, 557, 1037, 1052, 97, 420, 142]
    for index, duration in enumerate(durations):
        rows.extend([
            f'TINFO:{index},2,0,"Feature {index + 1}"',
            f'TINFO:{index},9,0,"{duration // 3600}:{(duration % 3600) // 60:02d}:{duration % 60:02d}"',
            f'TINFO:{index},11,0,"{duration * 400000}"',
            f'TINFO:{index},27,0,"title_t{index:02d}.mkv"',
            f'SINFO:{index},0,1,0,"Video"',
            f'SINFO:{index},1,1,0,"Audio"',
            f'SINFO:{index},1,3,0,"eng"',
        ])
    return CommandResult(
        command=("makemkvcon64.exe", "info", source),
        return_code=0,
        stdout="\n".join(rows) + "\n",
        stderr="",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:00:01+00:00",
    )


def _faerie_result(source: str) -> CommandResult:
    rows = ['DRV:0,2,999,1,"private hardware","FAERIE_TALE_THEATRE_4","D:"']
    for index in range(4):
        rows.extend([
            f'TINFO:{index},2,0,"Episode {index + 1}"',
            f'TINFO:{index},9,0,"0:50:0{index}"',
            f'TINFO:{index},11,0,"2000000000"',
            f'TINFO:{index},27,0,"title_t{index:02d}.mkv"',
            f'SINFO:{index},0,1,0,"Video"',
            f'SINFO:{index},1,1,0,"Audio"',
        ])
    return CommandResult(
        command=("makemkvcon64.exe", "info", source),
        return_code=0,
        stdout="\n".join(rows) + "\n",
        stderr="",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:00:01+00:00",
    )


def test_loaded_drive_can_prepare_non_authorized_pipeline(monkeypatch, tmp_path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    calls = []

    def inventory_runner(_executable, source, **kwargs):
        calls.append((source, kwargs))
        return _result(
            source,
            inventory=True,
            label="Dragons Race to the Edge Season 1 DVD2",
        )

    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    fingerprint = "0123456789abcdef"
    monkeypatch.setattr(
        rip_manifest, "_inventory_fingerprint", lambda _payload: fingerprint
    )
    skipped_contract = tmp_path / "skipped-rip.json"
    skipped_contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": fingerprint,
            "title_index": 1,
        }),
        encoding="utf-8",
    )
    pipeline.enqueue_verified_rip(
        "prior-warning", build_artifact("rip", skipped_contract)
    )
    pipeline.record_silent_video_review("prior-warning", "likely_warning_screen")
    pipeline.delete_queued_item_media(
        "prior-warning", lambda _item: None, remember_future_skip=True
    )

    response = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(
            drive_index=0,
            content_hint=None,
            handbrake_profile_id="balanced",
            library_policy="missing-only",
            confirm_read=True,
        ),
        "prepare-drive-test-0001",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert calls == [("disc:0", {"minimum_length": 0, "timeout_seconds": 300})]
    assert response["state"] == "awaiting_review"
    assert response["preview"]["execution_authorized"] is False
    assert response["preview"]["jobs"]
    assert {item["title_index"] for item in response["preview"]["jobs"]} == {0, 2}
    assert response["preview"]["skipped_titles"] == [
        {
            "disc_fingerprint": fingerprint,
            "title_index": 1,
            "reason": "likely_warning_screen",
        }
    ]
    assert public.list_events(response["job_id"])[0].event_type == "job_created"
    binding = private.get(response["job_id"])
    assert binding.output_root == output_root
    assert binding.media_contexts["disc-01"].content_hint == "tv"
    assert binding.media_contexts["disc-01"].series_name == "Dragons Race to the Edge"
    assert binding.media_contexts["disc-01"].season == 1
    assert binding.media_contexts["disc-01"].disc_number == 2
    assert binding.media_contexts["disc-01"].existing_output_policy == "missing-only"
    assert binding.media_contexts["disc-01"].staging_attempt.startswith("attempt-")
    assert all(
        "/attempt-" in item["staging_destination"]
        for item in response["preview"]["jobs"]
    )


def test_fresh_preparation_rebinds_exact_stale_drive_continuation(
    monkeypatch, tmp_path
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"

    def indexed_result(source, drive_index, *, inventory=False):
        result = _result(source, inventory=inventory)
        return CommandResult(
            command=result.command,
            return_code=result.return_code,
            stdout=result.stdout.replace("DRV:0,", f"DRV:{drive_index},"),
            stderr=result.stderr,
            started_at=result.started_at,
            finished_at=result.finished_at,
        )

    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    fingerprint = "0123456789abcdef"
    monkeypatch.setattr(
        rip_manifest, "_inventory_fingerprint", lambda _payload: fingerprint
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    old_watcher = DriveWatcher(
        lambda _exe, source, **_kwargs: indexed_result(source, 0)
    )
    old_watcher.refresh(executable)
    original = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-old-drive",
        old_watcher,
        public,
        private,
        pipeline,
        lambda _exe, source, **_kwargs: indexed_result(source, 0, inventory=True),
    )
    continuation = rip.select_rip_titles(
        original["job_id"],
        rip.SelectRipTitlesRequest(title_indexes=[2], confirm_selection=True),
        "select-one-title",
        public,
        private,
        pipeline,
    )
    new_watcher = DriveWatcher(
        lambda _exe, source, **_kwargs: indexed_result(source, 2)
    )
    new_watcher.refresh(executable)

    rebound = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=2, confirm_read=True),
        "prepare-new-drive",
        new_watcher,
        public,
        private,
        pipeline,
        lambda _exe, source, **_kwargs: indexed_result(source, 2, inventory=True),
    )

    assert rebound["job_id"] != continuation["job_id"]
    assert rebound["preview"]["drives"][0]["drive_index"] == 2
    assert [item["title_index"] for item in rebound["preview"]["jobs"]] == [2]
    binding = private.get(rebound["job_id"])
    assert binding.media_contexts["disc-01"].selected_title_indexes == (2,)
    assert new_watcher.snapshot().drives[0].current_job_id == rebound["job_id"]


@pytest.mark.parametrize(
    ("present_indexes", "expected_jobs", "expected_collisions"),
    [
        ({0, 1}, {2}, 0),
        ({0, 1, 2}, {0, 1, 2}, 3),
    ],
)
def test_missing_only_preparation_recognizes_resolution_suffixed_jellyfin_episodes(
    monkeypatch,
    tmp_path,
    present_indexes,
    expected_jobs,
    expected_collisions,
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    tv_root = tmp_path / "tv"
    season_dir = tv_root / "Show" / "Season 01"
    season_dir.mkdir(parents=True)
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
                jellyfin_tv_root=tv_root,
                jellyfin_movie_root=None,
            )
        ),
    )
    fingerprint = "0123456789abcdef"
    monkeypatch.setattr(
        rip_manifest, "_inventory_fingerprint", lambda _payload: fingerprint
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    with pipeline._connect() as connection:
        for title_index in range(3):
            episode_id = f"S01E{title_index + 1:02d}"
            historical = (
                f"Show/Season 01/Show - {episode_id} - Episode {title_index + 1}.mkv"
            )
            connection.execute(
                """
                INSERT INTO disc_title_history (
                    disc_fingerprint, title_index, outcome_name,
                    library_relative, episode_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    title_index,
                    f"Show - {episode_id} - Episode {title_index + 1}.mkv",
                    historical,
                    episode_id,
                    "2026-08-10T00:00:00+00:00",
                ),
            )
            if title_index in present_indexes:
                (
                    season_dir
                    / f"Show - {episode_id} - Episode {title_index + 1} - 1080p.mkv"
                ).write_bytes(b"organized")

    response = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(
            drive_index=0,
            content_hint="tv",
            library_policy="missing-only",
            confirm_read=True,
        ),
        "prepare-library-aware",
        watcher,
        public,
        private,
        pipeline,
        lambda _exe, source, **_kwargs: _result(source, inventory=True),
    )

    preview = response["preview"]
    assert {item["title_index"] for item in preview["jobs"]} == expected_jobs
    assert {item["title_index"] for item in preview["held_titles"]} == present_indexes
    assert preview["collision_count"] == expected_collisions
    assert preview["requires_review"] is bool(expected_collisions)
    if expected_collisions:
        assert {item["prior_library_status"] for item in preview["jobs"]} == {"present"}
        assert {item["collision_status"] for item in preview["jobs"]} == {
            "library-exists"
        }
    binding = private.get(response["job_id"])
    assert set(binding.media_contexts["disc-01"].selected_title_indexes or ()) == (
        expected_jobs
    )


def test_failed_per_title_disc_prepares_only_its_relevant_failed_scope(
    monkeypatch, tmp_path
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    inventory_runner = lambda _exe, source, **_kwargs: _result(  # noqa: E731
        source, inventory=True
    )

    fresh = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-failed-scope-fresh",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )
    assert {item["title_index"] for item in fresh["preview"]["jobs"]} == {0, 1, 2}

    selected = rip.select_rip_titles(
        fresh["job_id"],
        rip.SelectRipTitlesRequest(title_indexes=[1], confirm_selection=True),
        "select-failed-scope-title",
        public,
        private,
        pipeline,
    )
    public.authorize(
        selected["job_id"],
        expected_plan_sha256=selected["plan_sha256"],
        idempotency_key="authorize-failed-scope-title",
    )
    public.queue(selected["job_id"], idempotency_key="queue-failed-scope-title")
    public.claim_for_dispatch(
        selected["job_id"], idempotency_key="claim-failed-scope-title"
    )
    public.fail(
        selected["job_id"],
        idempotency_key="fail-failed-scope-title",
        error_type="RipError",
        error_category="makemkv_failure",
        failed_drive_indexes=(0,),
        completed_job_ids=(),
    )

    recovery = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-failed-scope-recovery",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert recovery["job_id"] != fresh["job_id"]
    assert {item["title_index"] for item in recovery["preview"]["jobs"]} == {1}
    context = private.get(recovery["job_id"]).media_contexts["disc-01"]
    assert context.selected_title_indexes == (1,)
    assert pipeline.disc_matching_scope(
        next(
            part
            for part in recovery["preview"]["jobs"][0]["staging_destination"].split("/")
            if len(part) == 16
        )
    ) == (0, 1, 2)


def test_failed_whole_disc_batch_recovers_only_current_relevant_scope(
    monkeypatch, tmp_path
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    monkeypatch.setattr(
        rip,
        "select_pipeline_titles",
        lambda plan, _hint: (plan.decisions[1],),
    )
    monkeypatch.setattr(
        rip,
        "select_recovery_titles",
        lambda plan, _hint: (plan.decisions[1],),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    inventory_runner = lambda _exe, source, **_kwargs: _result(  # noqa: E731
        source, inventory=True
    )

    fresh = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-failed-batch-fresh",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )
    assert {item["title_index"] for item in fresh["preview"]["jobs"]} == {0, 1, 2}
    assert fresh["preview"]["drives"][0]["strategy"] == "single-open"
    public.authorize(
        fresh["job_id"],
        expected_plan_sha256=fresh["plan_sha256"],
        idempotency_key="authorize-failed-batch",
    )
    public.queue(fresh["job_id"], idempotency_key="queue-failed-batch")
    public.claim_for_dispatch(fresh["job_id"], idempotency_key="claim-failed-batch")
    public.fail(
        fresh["job_id"],
        idempotency_key="fail-failed-batch",
        error_type="ParallelRipError",
        error_category="makemkv_failure",
        failed_drive_indexes=(0,),
        completed_job_ids=(),
    )

    recovery = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-failed-batch-recovery",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert {item["title_index"] for item in recovery["preview"]["jobs"]} == {1}
    context = private.get(recovery["job_id"]).media_contexts["disc-01"]
    assert context.selected_title_indexes == (1,)


def test_failed_tv_recovery_keeps_substantial_review_titles(monkeypatch, tmp_path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(
        lambda _exe, source, **_kwargs: _substantial_review_result(source)
    )
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                ffprobe_path=ffprobe,
                rip_output_root=output_root,
                cache_dir=cache_dir,
                jellyfin_tv_root=None,
                jellyfin_movie_root=None,
            )
        ),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    from dataclasses import replace

    def inventory_runner(_exe, source, **_kwargs):
        result = _substantial_review_result(source, inventory=True)
        stdout = result.stdout
        for estimated_size in (
            120_000_000,
            6_800_000_000,
            8_170_000_000,
            6_930_000_000,
            6_780_000_000,
            10_660_000_000,
        ):
            stdout = stdout.replace(f'"{estimated_size}"', '"2000000"')
        return replace(result, stdout=stdout)

    fresh = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(
            drive_index=0,
            content_hint="tv",
            confirm_read=True,
        ),
        "prepare-substantial-review-fresh",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )
    assert {item["title_index"] for item in fresh["preview"]["jobs"]} == set(range(10))
    public.authorize(
        fresh["job_id"],
        expected_plan_sha256=fresh["plan_sha256"],
        idempotency_key="authorize-substantial-review",
    )
    public.queue(fresh["job_id"], idempotency_key="queue-substantial-review")
    public.claim_for_dispatch(
        fresh["job_id"], idempotency_key="claim-substantial-review"
    )
    public.fail(
        fresh["job_id"],
        idempotency_key="fail-substantial-review",
        error_type="ParallelRipError",
        error_category="makemkv_failure",
        failed_drive_indexes=(0,),
        completed_job_ids=(),
    )
    fingerprint = next(
        part
        for part in fresh["preview"]["jobs"][0]["staging_destination"].split("/")
        if len(part) == 16
    )
    for title_index in (1, 3, 4):
        _record_safe_pipeline_title(
            pipeline,
            tmp_path,
            output_root,
            fingerprint=fingerprint,
            title_index=title_index,
            suffix="substantial-review",
        )
    failed_batch_dir = output_root / fresh["preview"]["jobs"][0]["staging_destination"]
    failed_batch_dir.mkdir(parents=True, exist_ok=True)
    for title_index in range(1, 6):
        (failed_batch_dir / f"title_t{title_index:02d}.mkv").write_bytes(
            b"x" * 2_000_000
        )

    inspected: list[str] = []

    def fake_inspector(_exe, source, **_kwargs):
        from mkv_episode_matcher.media.ffprobe_runner import FFprobeInspection
        from mkv_episode_matcher.media.probe import ProbedMedia

        inspected.append(source.name)
        return FFprobeInspection(
            return_code=0,
            stdout="",
            stderr="",
            started_at="2026-08-22T00:00:00Z",
            finished_at="2026-08-22T00:00:01Z",
            media=ProbedMedia(
                duration_seconds=1200,
                size_bytes=source.stat().st_size,
                container="matroska",
                audio_streams=(),
            ),
        )

    monkeypatch.setattr(rip, "get_ffprobe_inspector", lambda: fake_inspector)
    monkeypatch.setattr(
        rip, "get_pipeline_contract_root", lambda: tmp_path / "contracts"
    )

    recovery = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(
            drive_index=0,
            content_hint="tv",
            confirm_read=True,
        ),
        "prepare-substantial-review-recovery",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert recovery["state"] == "completed"
    assert inspected == ["title_t02.mkv", "title_t05.mkv"]
    assert len(pipeline.list_items()) == 5
    assert {item["title_index"] for item in recovery["preview"]["jobs"]} == {
        1,
        2,
        3,
        4,
        5,
    }
    context = private.get(recovery["job_id"]).media_contexts["disc-01"]
    assert context.selected_title_indexes == (1, 2, 3, 4, 5)
    assert pipeline.disc_matching_scope(fingerprint) == (1, 3, 4)
    assert pipeline.disc_recovery_scope(fingerprint) == (1, 2, 3, 4, 5)


def test_retry_preparation_replaces_same_drive_awaiting_review(monkeypatch, tmp_path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    inventory_runner = lambda _exe, source, **_kwargs: _result(  # noqa: E731
        source, inventory=True
    )
    first = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-same-drive-first",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    second = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-same-drive-second",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert second["job_id"] != first["job_id"]
    assert watcher.snapshot().drives[0].current_job_id == second["job_id"]


def test_retry_preparation_rebinds_same_completed_disc(monkeypatch, tmp_path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    inventory_runner = lambda _exe, source, **_kwargs: _result(  # noqa: E731
        source, inventory=True
    )
    first = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-completed-first",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )
    public.authorize(
        first["job_id"],
        expected_plan_sha256=first["plan_sha256"],
        idempotency_key="authorize-completed-first",
    )
    public.queue(first["job_id"], idempotency_key="queue-completed-first")
    public.claim_for_dispatch(first["job_id"], idempotency_key="claim-completed-first")
    public.complete(
        first["job_id"],
        idempotency_key="complete-completed-first",
        completed_count=len(first["preview"]["jobs"]),
        pipeline_queued_count=len(first["preview"]["jobs"]),
    )
    fingerprint = next(
        part
        for part in first["preview"]["jobs"][0]["staging_destination"].split("/")
        if len(part) == 16
    )
    for item in first["preview"]["jobs"]:
        _record_safe_pipeline_title(
            pipeline,
            tmp_path,
            output_root,
            fingerprint=fingerprint,
            title_index=item["title_index"],
            suffix="completed",
        )

    rebound = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-completed-second",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert rebound["job_id"] == first["job_id"]
    assert rebound["state"] == "completed"
    assert len(public.list_jobs()) == 1
    assert watcher.snapshot().drives[0].current_job_id == first["job_id"]


def test_retry_preparation_retires_completed_duplicate_but_keeps_uncovered_titles(
    monkeypatch, tmp_path
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    inventory_runner = lambda _exe, source, **_kwargs: _result(  # noqa: E731
        source, inventory=True
    )
    original = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-resume-original",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )
    selected_index = original["preview"]["jobs"][0]["title_index"]
    queued = rip.select_rip_titles(
        original["job_id"],
        rip.SelectRipTitlesRequest(
            title_indexes=[selected_index], confirm_selection=True
        ),
        "select-queued-recovery",
        public,
        private,
        pipeline,
    )
    public.authorize(
        queued["job_id"],
        expected_plan_sha256=queued["plan_sha256"],
        idempotency_key="authorize-queued-recovery",
    )
    public.queue(queued["job_id"], idempotency_key="queue-queued-recovery")

    completed = rip.select_rip_titles(
        original["job_id"],
        rip.SelectRipTitlesRequest(
            title_indexes=[selected_index], confirm_selection=True
        ),
        "select-newer-completed",
        public,
        private,
        pipeline,
    )
    public.authorize(
        completed["job_id"],
        expected_plan_sha256=completed["plan_sha256"],
        idempotency_key="authorize-newer-completed",
    )
    public.queue(completed["job_id"], idempotency_key="queue-newer-completed")
    public.claim_for_dispatch(
        completed["job_id"], idempotency_key="claim-newer-completed"
    )
    public.complete(
        completed["job_id"],
        idempotency_key="complete-newer-completed",
        completed_count=1,
        pipeline_queued_count=1,
    )
    fingerprint = next(
        part
        for part in completed["preview"]["jobs"][0]["staging_destination"].split("/")
        if len(part) == 16
    )
    _record_safe_pipeline_title(
        pipeline,
        tmp_path,
        output_root,
        fingerprint=fingerprint,
        title_index=selected_index,
        suffix="newer-completed",
    )

    rebound = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-resume-queued",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert rebound["job_id"] not in {queued["job_id"], completed["job_id"]}
    assert rebound["state"] == "awaiting_review"
    assert len(rebound["preview"]["jobs"]) == len(original["preview"]["jobs"])
    assert public.get_job(queued["job_id"]).state == "cancelled"
    assert watcher.snapshot().drives[0].current_job_id == rebound["job_id"]

    newer_queued = rip.select_rip_titles(
        original["job_id"],
        rip.SelectRipTitlesRequest(
            title_indexes=[selected_index], confirm_selection=True
        ),
        "select-newer-queued-recovery",
        public,
        private,
        pipeline,
    )
    public.authorize(
        newer_queued["job_id"],
        expected_plan_sha256=newer_queued["plan_sha256"],
        idempotency_key="authorize-newer-queued-recovery",
    )
    public.queue(newer_queued["job_id"], idempotency_key="queue-newer-queued-recovery")

    resumed = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-resume-newer-queued",
        watcher,
        public,
        private,
        pipeline,
        inventory_runner,
    )

    assert resumed["job_id"] == newer_queued["job_id"]
    assert resumed["state"] == "queued"
    assert public.get_job(newer_queued["job_id"]).state == "queued"


def test_bonus_drive_preparation_attaches_reviewed_catalogue(monkeypatch, tmp_path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")

    response = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(
            drive_index=0,
            content_hint=None,
            confirm_read=True,
        ),
        "prepare-bonus-test-0001",
        watcher,
        public,
        private,
        PipelineQueueStore(tmp_path / "pipeline.sqlite3"),
        lambda _exe, source, **_kwargs: _bonus_result(source),
    )

    binding = private.get(response["job_id"])
    context = binding.media_contexts["disc-01"]
    assert context.content_hint == "extras"
    assert context.special_feature_catalog_id == "parent-trap-2005-r1-disc2-v1"
    assert context.special_feature_library_title == "The Parent Trap"
    assert context.selected_title_indexes
    assert context.special_feature_assignments
    assert all(
        "candidate_feature_ids" in assignment
        for assignment in context.special_feature_assignments
    )
    assert response["preview"]["drives"][0]["selection_mode"] == (
        "reviewed-special-features"
    )
    assert all(
        item["final_destination"] is None for item in response["preview"]["jobs"]
    )
    assert any(
        item["display_name"] == "Caught in the Act: The Making of The Parent Trap"
        and item["extras_folder"] == "behind the scenes"
        and item["identification_status"] == "catalogue-match"
        for item in response["preview"]["jobs"]
    )
    assert len({item["display_name"] for item in response["preview"]["jobs"]}) > 1
    assert all(
        item["identification_status"] == "catalogue-match"
        for item in response["preview"]["jobs"]
    )


def test_faerie_drive_preparation_requires_fresh_cross_season_analysis(
    monkeypatch, tmp_path
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _faerie_result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
            )
        ),
    )
    private = PrivateBindingStore(tmp_path / "private.sqlite3")

    response = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-faerie-test-0001",
        watcher,
        OrchestrationStore(tmp_path / "public.sqlite3"),
        private,
        PipelineQueueStore(tmp_path / "pipeline.sqlite3"),
        lambda _exe, source, **_kwargs: _faerie_result(source),
    )

    context = private.get(response["job_id"]).media_contexts["disc-01"]
    assert context.series_name == "FAERIE TALE THEATRE"
    assert context.season is None
    assert context.episode_assignments == ()


def test_drive_preparation_uses_thediscdb_episode_assignments_when_enabled(
    monkeypatch, tmp_path
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
                thediscdb_lookup_enabled=True,
            )
        ),
    )
    calls = []

    def lookup(root, inventory, *, timeout_seconds):
        calls.append((root, len(inventory.titles), timeout_seconds))
        return TheDiscDbResolution(
            status="matched",
            media_title="Example Series",
            media_type="Series",
            tmdb_id=123,
            episode_assignments=(
                {
                    "title_index": 0,
                    "season": 2,
                    "episode": 3,
                    "title": "Database Episode",
                    "identification_source": "thediscdb",
                },
            ),
            matched_title_indexes=(0,),
            unmatched_title_indexes=(1, 2),
        )

    monkeypatch.setattr(rip, "lookup_disc_metadata", lookup)
    private = PrivateBindingStore(tmp_path / "private.sqlite3")

    response = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-thediscdb-test-0001",
        watcher,
        OrchestrationStore(tmp_path / "public.sqlite3"),
        private,
        PipelineQueueStore(tmp_path / "pipeline.sqlite3"),
        lambda _exe, source, **_kwargs: _result(source, inventory=True),
    )

    assert len(calls) == 1
    context = private.get(response["job_id"]).media_contexts["disc-01"]
    assert context.series_name == "Example Series"
    assert context.season == 2
    assert context.tmdb_id == 123
    assert context.content_hint == "tv"
    assert context.disc_metadata_source == "thediscdb"
    assert context.disc_metadata_status == "matched"
    assert context.disc_metadata_matched_title_count == 1
    assert context.episode_assignments[0]["title"] == "Database Episode"
    preview_drive = response["preview"]["drives"][0]
    assert preview_drive["metadata_source"] == "thediscdb"
    assert preview_drive["metadata_status"] == "matched"
    title = next(
        item for item in response["preview"]["jobs"] if item["title_index"] == 0
    )
    assert title["display_name"] == "S02E03 - Database Episode"
    assert title["identification_status"] == "disc-database-match"


def test_drive_preparation_holds_automatic_work_for_catalogue_support_prompt(
    monkeypatch, tmp_path
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    cache_dir = tmp_path / "cache"
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                rip_output_root=output_root,
                cache_dir=cache_dir,
                ripweaver_catalogue_enabled=True,
                ripweaver_catalogue_url="https://api.ripweaver.com",
                thediscdb_lookup_enabled=True,
            )
        ),
    )
    monkeypatch.setattr(
        rip,
        "read_disc_filesystem_identity",
        lambda _root: SimpleNamespace(content_hash="A" * 32),
    )
    monkeypatch.setattr(
        rip,
        "load_environment_settings",
        lambda: SimpleNamespace(ripweaver_catalogue_token="rwc_synthetic"),
    )

    class Client:
        def __init__(self, *, base_url):
            assert base_url == "https://api.ripweaver.com"

        def capabilities(self):
            return SimpleNamespace(compatible=True)

        def lookup(self, *_args, **_kwargs):
            raise RipWeaverCatalogueSupportRequiredError(
                usage=CatalogueUsage(10, 10, 0, 0, 0, 0),
                policy=SupportPolicy(
                    policy_version="2026-08-10",
                    terms_version="2026-08-10",
                    minimum_amount_cents=1000,
                    minimum_rate_cents=1,
                    maximum_rate_cents=100,
                    default_rate_cents=10,
                    payments_enabled=False,
                    support_message="Support or continue manually.",
                    availability_disclosure="Best effort.",
                    refund_disclosure="Generally final.",
                ),
            )

    monkeypatch.setattr(rip, "RipWeaverCatalogueClient", Client)
    direct_fallback_called = False

    def direct_fallback(*_args, **_kwargs):
        nonlocal direct_fallback_called
        direct_fallback_called = True
        return TheDiscDbResolution(status="matched")

    monkeypatch.setattr(rip, "lookup_disc_metadata", direct_fallback)
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    response = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "prepare-catalogue-support-0001",
        watcher,
        OrchestrationStore(tmp_path / "public.sqlite3"),
        private,
        PipelineQueueStore(tmp_path / "pipeline.sqlite3"),
        lambda _exe, source, **_kwargs: _result(source, inventory=True),
    )

    assert direct_fallback_called is False
    context = private.get(response["job_id"]).media_contexts["disc-01"]
    assert context.disc_metadata_source == "ripweaver-catalogue"
    assert context.disc_metadata_status == "support-required"
    assert response["preview"]["requires_review"] is True


def test_drive_preparation_auto_admits_complete_staged_disc(monkeypatch, tmp_path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"synthetic")
    output_root = tmp_path / "rips"
    output_root.mkdir()
    watcher = DriveWatcher(lambda _exe, source, **_kwargs: _result(source))
    watcher.refresh(executable)
    fingerprint = "0123456789abcdef"
    monkeypatch.setattr(
        rip_manifest, "_inventory_fingerprint", lambda _payload: fingerprint
    )

    staging_dir = (
        output_root
        / ".staging"
        / "disc-01"
        / "attempt-0001"
        / fingerprint
        / "title-000"
    )
    staging_dir.mkdir(parents=True)
    for index in range(3):
        target = staging_dir / f"title_t{index:02d}.mkv"
        target.write_bytes(b"x" * 2_000_000)

    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                makemkv_path=executable,
                ffprobe_path=ffprobe,
                rip_output_root=output_root,
                cache_dir=tmp_path / "cache",
                jellyfin_tv_root=None,
                jellyfin_movie_root=None,
            )
        ),
    )

    def fake_inspector(_exe, source, **_kwargs):
        from mkv_episode_matcher.media.ffprobe_runner import FFprobeInspection
        from mkv_episode_matcher.media.probe import ProbedMedia

        return FFprobeInspection(
            return_code=0,
            stdout="",
            stderr="",
            started_at="2026-08-22T00:00:00Z",
            finished_at="2026-08-22T00:00:01Z",
            media=ProbedMedia(
                duration_seconds=1200,
                size_bytes=2_000_000,
                container="matroska",
                audio_streams=(),
            ),
        )

    monkeypatch.setattr(rip, "get_ffprobe_inspector", lambda: fake_inspector)
    monkeypatch.setattr(
        rip, "get_pipeline_contract_root", lambda: tmp_path / "contracts"
    )

    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")

    from dataclasses import replace

    def custom_inventory(_exe, source, **_kwargs):
        res = _result(source, inventory=True)
        return replace(res, stdout=res.stdout.replace("2000000000", "2000000"))

    response = rip.prepare_drive_pipeline(
        rip.PrepareDrivePipelineRequest(drive_index=0, confirm_read=True),
        "auto-admit-test-0001",
        watcher,
        public,
        private,
        pipeline,
        custom_inventory,
    )

    assert response["state"] == "completed"
    items = pipeline.list_items()
    assert len(items) == 3
    assert all(item.stage == "identify" for item in items)
