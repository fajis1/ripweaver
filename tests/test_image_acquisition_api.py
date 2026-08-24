from pathlib import Path
from types import SimpleNamespace

from mkv_episode_matcher.backend.routers.acquisition import (
    ExecuteRequest,
    PlanRequest,
    TransitionRequest,
    authorize,
    create_acquisition,
    execute,
    queue,
)
from mkv_episode_matcher.disc.image_acquisition import VerifiedLocalSource
from mkv_episode_matcher.disc.image_acquisition_bindings import (
    PrivateAcquisitionBindingStore,
)
from mkv_episode_matcher.disc.image_acquisition_store import ImageAcquisitionStore


def test_complete_api_handoff_is_path_redacted(tmp_path: Path, monkeypatch):
    tool = tmp_path / "DiscImageCreator.exe"
    tool.write_bytes(b"tool")
    image_root = tmp_path / "images"
    image_root.mkdir()
    public = ImageAcquisitionStore(tmp_path / "public.sqlite3")
    private = PrivateAcquisitionBindingStore(tmp_path / "private.sqlite3")
    calls = []

    def executor(plan, **kwargs):
        calls.append((plan, kwargs))
        return VerifiedLocalSource(
            plan.acquisition_id, image_root / "disc.iso", "iso:private", 2048
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.routers.acquisition.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                disc_image_creator_path=tool,
                makemkv_path=None,
                disc_image_root=image_root,
            )
        ),
    )
    created = create_acquisition(
        PlanRequest(
            drive_index=0,
            media_kind="dvd",
            estimated_bytes=2048,
            drive_letter="E",
            idempotency_key="create",
        ),
        public,
        private,
    )
    job_id, digest = created["job_id"], created["plan_sha256"]
    assert "E" not in str(created)
    authorize(
        job_id,
        TransitionRequest(plan_sha256=digest, idempotency_key="authorize"),
        public,
    )
    queue(
        job_id,
        TransitionRequest(plan_sha256=digest, idempotency_key="queue"),
        public,
    )
    result = execute(
        job_id,
        ExecuteRequest(
            plan_sha256=digest,
            idempotency_key="execute",
            authorized_acquisition_count=1,
            confirm_acquisition=True,
            timeout_seconds=60,
        ),
        public,
        private,
        executor,
    )
    assert result["state"] == "verified"
    assert result["result"]["local_source_verified"] is True
    assert len(calls) == 1
