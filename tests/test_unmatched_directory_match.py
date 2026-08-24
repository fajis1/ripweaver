import json
from pathlib import Path
from types import SimpleNamespace

from mkv_episode_matcher.backend.routers import match
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore


def test_unmatched_directory_match_admits_verified_files_without_running_worker(
    monkeypatch, tmp_path
):
    files = []
    for index in range(2):
        path = tmp_path / f"title-{index}.mkv"
        path.write_bytes(b"synthetic-mkv")
        files.append(str(path))

    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"synthetic")
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    started = []

    monkeypatch.setattr(
        match,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace(ffprobe_path=executable)),
    )
    monkeypatch.setattr(
        match.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(
            start=lambda: started.append(kwargs["target"])
        ),
    )

    def inspect(_executable, _source, *, timeout_seconds):
        assert timeout_seconds == 60
        return SimpleNamespace(media=SimpleNamespace(duration_seconds=1200))

    response = match.start_unmatched_match(
        match.UnmatchedMatchRequest(
            files=files,
            series_name="Faerie Tale Theatre",
            confirm_media_read=True,
            confirm_provider_lookup=True,
        ),
        store,
        tmp_path / "contracts",
        inspect,
    )

    assert response["status"] == "started"
    assert response["item_count"] == 2
    assert len(response["disc_fingerprint"]) == 16
    assert len(started) == 1
    items = store.list_items()
    assert len(items) == 2
    assert all(item.state == "review_required" for item in items)
    assert all(item.review_code == "unmatched_disc_analysis_required" for item in items)
    contract = Path(items[0].artifact.contract_path)
    assert "Faerie Tale Theatre" in contract.read_text(encoding="utf-8")


def test_unmatched_directory_match_accepts_one_verified_file(monkeypatch, tmp_path):
    source = tmp_path / "only-title.mkv"
    source.write_bytes(b"synthetic-mkv")
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"synthetic")
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    started = []
    monkeypatch.setattr(
        match,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace(ffprobe_path=executable)),
    )
    monkeypatch.setattr(
        match.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(
            start=lambda: started.append(kwargs["target"])
        ),
    )

    response = match.start_unmatched_match(
        match.UnmatchedMatchRequest(
            files=[str(source)],
            series_name="The Office",
            season=6,
            confirm_media_read=True,
            confirm_provider_lookup=True,
        ),
        store,
        tmp_path / "contracts",
        lambda *_args, **_kwargs: SimpleNamespace(
            media=SimpleNamespace(duration_seconds=1800)
        ),
    )

    assert response["status"] == "started"
    assert response["item_count"] == 1
    assert len(store.list_items()) == 1
    assert len(started) == 1


def test_smart_folder_routes_episode_cluster_to_durable_tv_analysis(
    monkeypatch, tmp_path
):
    folder = tmp_path / "Example Show" / "Unmatched"
    folder.mkdir(parents=True)
    files = []
    for index in range(3):
        path = folder / f"title-{index}.mkv"
        path.write_bytes(b"synthetic-mkv")
        files.append(str(path))
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"synthetic")
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    started = []
    monkeypatch.setattr(
        match,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace(ffprobe_path=executable)),
    )
    monkeypatch.setattr(
        match.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["name"])),
    )

    response = match.start_smart_folder_match(
        match.SmartFolderMatchRequest(
            files=files,
            confirm_media_read=True,
            confirm_provider_lookup=True,
        ),
        store,
        tmp_path / "contracts",
        lambda *_args, **_kwargs: SimpleNamespace(
            media=SimpleNamespace(duration_seconds=1500)
        ),
    )

    assert response["classification"] == "tv"
    assert response["series_name"] == "Example Show"
    assert started == ["directory-smart-tv-analysis"]
    payload = json.loads(store.list_items()[0].artifact.contract_path.read_text())
    assert payload["disc_expected_title_indexes"] == [0, 1, 2]


def test_smart_folder_routes_dominant_feature_to_descriptive_review(
    monkeypatch, tmp_path
):
    files = []
    for index in range(2):
        path = tmp_path / f"feature-{index}.mkv"
        path.write_bytes(b"synthetic-mkv")
        files.append(str(path))
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"synthetic")
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    durations = iter((7200, 600))
    monkeypatch.setattr(
        match,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace(ffprobe_path=executable)),
    )

    response = match.start_smart_folder_match(
        match.SmartFolderMatchRequest(
            files=files,
            series_name="Example Movie",
            confirm_media_read=True,
            confirm_provider_lookup=True,
        ),
        store,
        tmp_path / "contracts",
        lambda *_args, **_kwargs: SimpleNamespace(
            media=SimpleNamespace(duration_seconds=next(durations))
        ),
    )

    assert response["classification"] == "movie"
    assert all(
        item.review_code == "gemini_evidence_required" for item in store.list_items()
    )
