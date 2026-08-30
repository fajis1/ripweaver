import io
import json
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

from mkv_episode_matcher.backend import dependencies
from mkv_episode_matcher.backend.routers import system
from mkv_episode_matcher.backend.support_bundle import (
    MAX_EVENT_COUNT,
    bounded_public_events,
    build_support_bundle,
    redact_text,
)
from mkv_episode_matcher.core import config_manager, credentials
from mkv_episode_matcher.core.models import Config


def _archive_files(content: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_redact_text_removes_private_diagnostic_values():
    private = (
        "Authorization: Bearer abc.def.ghi\n"
        "api_key=super-secret\n"
        "Reading C:\\Users\\Alice\\Videos\\Private Show\\Episode 1.mkv\n"
        'Selected "Private Bonus Feature.mkv"\n'
        "Contact alice@example.com from 192.168.10.15\n"
        "Linux source /home/alice/media/private.mkv\n"
    )

    redacted = redact_text(private)

    for value in (
        "abc.def.ghi",
        "super-secret",
        "Alice",
        "Private Show",
        "Private Bonus Feature",
        "alice@example.com",
        "192.168.10.15",
        "/home/alice",
    ):
        assert value not in redacted
    assert "[redacted]" in redacted
    assert "[path redacted]" in redacted


def test_support_bundle_is_bounded_and_excludes_private_files(tmp_path):
    cache_dir = tmp_path / "private-cache" / "cache"
    log_dir = cache_dir.parent / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "mkv-match.log").write_text(
        "Opening C:\\Users\\Alice\\Videos\\Private Show\\Episode 1.mkv\n"
        "password=hunter2\n"
        "provider failed for alice@example.com\n",
        encoding="utf-8",
    )
    (log_dir / ".env").write_text("API_KEY=must-never-appear", encoding="utf-8")
    config = Config(
        cache_dir=cache_dir,
        rip_output_root=tmp_path / "Private Rips",
        jellyfin_tv_root=tmp_path / "Private Library",
        automatic_processing_enabled=True,
    )
    events = [
        {
            "sequence": 7,
            "media_id": "private-series-disc-1-title-1",
            "event_type": "stage_failed",
            "stage": "identify",
            "state": "review_required",
            "created_at": "2026-08-29T12:00:00Z",
            "details": {
                "review_code": "provider_failed",
                "source_path": "C:\\Users\\Alice\\Episode 1.mkv",
                "transcript": "private dialogue",
                "series_name": "Private Show",
            },
        }
    ]

    bundle = build_support_bundle(
        app_version="1.3.7b1",
        config=config,
        system_health={
            "status": "needs_setup",
            "download_url": "https://ffmpeg.org/download.html",
            "configured_path": "C:\\Private\\ffmpeg.exe",
        },
        pipeline_events=events,
        log_dir=log_dir,
        generated_at=datetime(2026, 8, 29, 12, 30, tzinfo=timezone.utc),
        support_id="abc123def456",
    )

    files = _archive_files(bundle.content)
    assert set(files) == {
        "README.txt",
        "support-summary.json",
        "system-health.json",
        "pipeline-events.json",
        "application.log",
    }
    combined = "\n".join(files.values())
    for private_value in (
        "must-never-appear",
        "hunter2",
        "Alice",
        "Private Show",
        "private dialogue",
        "private-series-disc-1-title-1",
        "Private Library",
        "Private Rips",
    ):
        assert private_value not in combined
    assert "https://ffmpeg.org/download.html" in files["system-health.json"]
    assert "media-" in files["pipeline-events.json"]
    assert json.loads(files["support-summary.json"])["privacy"] == {
        "credentials_redacted": True,
        "dialogue_excluded": True,
        "environment_excluded": True,
        "media_names_redacted": True,
        "paths_redacted": True,
        "private_provider_transactions_excluded": True,
    }
    assert bundle.filename == (
        "RipWeaver-Support-1.3.7b1-20260829T123000Z-abc123def456.zip"
    )
    assert len(bundle.sha256) == 64


def test_pipeline_event_export_keeps_only_newest_bounded_events():
    events = [
        {
            "sequence": index,
            "media_id": f"private-media-{index}",
            "event_type": "advanced",
            "stage": "identify",
            "state": "queued",
            "created_at": "2026-08-29T12:00:00Z",
            "details": {},
        }
        for index in range(MAX_EVENT_COUNT + 2)
    ]

    public, truncated = bounded_public_events(events)

    assert truncated is True
    assert len(public) == MAX_EVENT_COUNT
    assert public[0]["sequence"] == 2
    assert public[-1]["sequence"] == MAX_EVENT_COUNT + 1
    assert all(not item["media_id"].startswith("private-") for item in public)


def test_support_bundle_route_returns_download_without_persisting_archive(
    monkeypatch, tmp_path
):
    config = Config(cache_dir=tmp_path / "cache")
    monkeypatch.setattr(
        config_manager,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: config),
    )
    monkeypatch.setattr(
        credentials, "credential_is_configured", lambda _credential: False
    )
    monkeypatch.setattr(system, "discover_tools", lambda: {"tools": {}})
    monkeypatch.setattr(
        dependencies,
        "get_pipeline_queue_store",
        lambda: SimpleNamespace(list_events=lambda: ()),
    )

    response = system.export_support_bundle()

    assert response.media_type == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].endswith('.zip"')
    assert len(response.headers["x-ripweaver-support-sha256"]) == 64
    assert _archive_files(response.body)["README.txt"].startswith(
        "RipWeaver support bundle"
    )
    assert not list(tmp_path.rglob("*.zip"))
