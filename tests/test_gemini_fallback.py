import json
import re
from types import SimpleNamespace

from mkv_episode_matcher.backend.gemini_fallback import execute_gemini_fallback
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiDescriptivePlan,
    GeminiDescriptiveResult,
)


class FakeStore:
    def __init__(self, items):
        self.items = {item.media_id: item for item in items}
        self.applied = {}

    def get(self, media_id):
        return self.items[media_id]

    def apply_reviewed_identification_input(self, media_id, artifact):
        self.applied[media_id] = artifact


def _item(tmp_path, media_id, duration):
    source = tmp_path / f"{media_id}.mkv"
    source.write_bytes(b"synthetic")
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": int(re.search(r"-title-(\d{3})", media_id).group(1)),
            "media_context": {
                "series_name": "Example Double Feature",
                "content_hint": "extras",
                "special_feature_catalog_id": None,
                "special_feature_assignments": [],
            },
            "test_duration": duration,
        }),
        encoding="utf-8",
    )
    return SimpleNamespace(
        media_id=media_id,
        review_code="gemini_analysis_running",
        artifact=SimpleNamespace(contract_path=contract),
    )


def test_catalogue_free_fallback_applies_mixed_provisional_results(
    tmp_path, monkeypatch
):
    items = [
        _item(tmp_path, "disc-title-000", 7200),
        _item(tmp_path, "disc-title-003-recovery-abcd", 300),
    ]
    store = FakeStore(items)
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe.write_bytes(b"tool")
    ffmpeg.write_bytes(b"tool")
    durations = {
        item.media_id: 7200 if "title-000" in item.media_id else 300 for item in items
    }

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.resolve_ffprobe_path",
        lambda _path: ffprobe,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.resolve_ffmpeg_path",
        lambda _path: ffmpeg,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.inspect_mkv",
        lambda _tool, source, timeout_seconds: SimpleNamespace(
            media=SimpleNamespace(
                duration_seconds=durations[source.stem], audio_streams=()
            )
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_transcript_batch",
        lambda *args, **kwargs: SimpleNamespace(
            files=tuple(
                SimpleNamespace(
                    file_id=item.media_id,
                    windows=(SimpleNamespace(text="bounded evidence"),),
                )
                for item in items
            )
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda self, evidence, release_hint: GeminiDescriptivePlan(
            mode="gemini-descriptive-review-plan",
            model="test",
            matches=(
                GeminiDescriptiveResult(
                    "disc-title-000",
                    "movie",
                    "Example Movie",
                    2001,
                    0.9,
                    ("Feature-length evidence.",),
                ),
                GeminiDescriptiveResult(
                    "disc-title-003-recovery-abcd",
                    "extra",
                    "Making Example Movie",
                    None,
                    0.7,
                    ("Short production evidence.",),
                ),
            ),
        ),
    )

    applied = execute_gemini_fallback(
        store,
        tuple(item.media_id for item in items),
        Config(ffprobe_path=ffprobe, ffmpeg_path=ffmpeg, gemini_model="test"),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert applied == ("disc-title-000", "disc-title-003-recovery-abcd")
    movie = json.loads(
        store.applied["disc-title-000"].contract_path.read_text(encoding="utf-8")
    )
    extra = json.loads(
        store.applied["disc-title-003-recovery-abcd"].contract_path.read_text(
            encoding="utf-8"
        )
    )
    assert (
        movie["media_context"]["special_feature_assignments"][0]["media_kind"]
        == "movie"
    )
    assert movie["media_context"]["special_feature_library_year"] == 2001
    assert (
        extra["media_context"]["special_feature_assignments"][0]["media_kind"]
        == "extra"
    )
