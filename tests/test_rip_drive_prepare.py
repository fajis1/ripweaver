from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import CommandResult
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore


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


def test_drive_preparation_guard_refuses_duplicate_scan_and_releases():
    claimed = rip._claim_drive_preparation(17)
    try:
        with pytest.raises(HTTPException, match="already being prepared"):
            rip._claim_drive_preparation(17)
    finally:
        claimed.release()

    next_claim = rip._claim_drive_preparation(17)
    next_claim.release()


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
    other_disc = matching.with_name(
        "Test-Show--disc-02-fedcba9876543210-title-004.mkv"
    )
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
        PipelineQueueStore(tmp_path / "pipeline.sqlite3"),
        inventory_runner,
    )

    assert calls == [("disc:0", {"minimum_length": 0, "timeout_seconds": 300})]
    assert response["state"] == "awaiting_review"
    assert response["preview"]["execution_authorized"] is False
    assert response["preview"]["jobs"]
    assert public.list_events(response["job_id"])[0].event_type == "job_created"
    binding = private.get(response["job_id"])
    assert binding.output_root == output_root
    assert binding.media_contexts["disc-01"].content_hint == "tv"
    assert binding.media_contexts["disc-01"].series_name == "Dragons Race to the Edge"
    assert binding.media_contexts["disc-01"].season == 1
    assert binding.media_contexts["disc-01"].existing_output_policy == "missing-only"
    assert binding.media_contexts["disc-01"].staging_attempt.startswith("attempt-")
    assert all(
        "/attempt-" in item["staging_destination"]
        for item in response["preview"]["jobs"]
    )


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
