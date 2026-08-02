import json
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend import unmatched_disc_analysis as analysis
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


@pytest.mark.parametrize("use_gemini", [False, True])
def test_all_season_analysis_applies_sequence_without_saved_release_map(
    tmp_path, monkeypatch, use_gemini
):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "0123456789abcdef"
    media_ids = []
    for title_index in range(2):
        media_id = f"disc-04-{fingerprint}-title-{title_index:03d}"
        media_ids.append(media_id)
        source = tmp_path / f"source-{title_index}.mkv"
        source.write_bytes(b"synthetic")
        contract = contracts / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {"series_name": "Unmatched", "season": None},
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.claim_next()
        store.require_review(media_id, "unmatched_disc_analysis_required")

    catalog = tuple(
        EpisodeCatalogEntry(f"S0{index + 1}E01", index + 1, 1, title, "overview", 1200)
        for index, title in enumerate(("First", "Second"))
    )
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog_for_show", lambda _name: catalog
    )
    monkeypatch.setattr(
        analysis, "resolve_ffprobe_path", lambda _path: tmp_path / "ffprobe"
    )
    monkeypatch.setattr(
        analysis, "resolve_ffmpeg_path", lambda _path: tmp_path / "ffmpeg"
    )
    monkeypatch.setattr(
        analysis,
        "inspect_mkv",
        lambda *_args, **_kwargs: SimpleNamespace(
            media=SimpleNamespace(duration_seconds=1200)
        ),
    )
    monkeypatch.setattr(
        analysis,
        "collect_transcript_batch",
        lambda items, *_args, **_kwargs: SimpleNamespace(
            files=tuple(
                SimpleNamespace(
                    file_id=item.file_id,
                    duration_seconds=1200,
                    windows=(SimpleNamespace(start_seconds=0, text="useful dialogue"),),
                )
                for item in items
            )
        ),
    )
    monkeypatch.setattr(
        analysis,
        "plan_disc_sequences",
        lambda *_args, **_kwargs: SimpleNamespace(
            disposition="review" if use_gemini else "proposed",
            groups=(
                SimpleNamespace(
                    items=tuple(
                        SimpleNamespace(
                            file_id=media_id, proposed_episode=entry.episode_id
                        )
                        for media_id, entry in zip(media_ids, catalog, strict=True)
                    )
                ),
            ),
        ),
    )
    if use_gemini:

        class FakeRanker:
            def __init__(self, *, model):
                assert model == "gemini-test"

            def rank_with_configured_keys(self, files, _catalog):
                return SimpleNamespace(
                    matches=tuple(
                        SimpleNamespace(
                            file_id=item.file_id,
                            episode_id=entry.episode_id,
                            confidence=0.8,
                        )
                        for item, entry in zip(files, catalog, strict=True)
                    )
                )

        monkeypatch.setattr(analysis, "GeminiEpisodeRanker", FakeRanker)
    for media_id in media_ids:
        store.choose_review_path(media_id, "all_season_analysis_running")

    applied = analysis.execute_unmatched_disc_analysis(
        store,
        fingerprint,
        "Example Series",
        SimpleNamespace(
            ffprobe_path=None,
            ffmpeg_path=None,
            asr_model_name="small",
            automatic_gemini_ambiguity_fallback=use_gemini,
            gemini_model="gemini-test",
        ),
        SimpleNamespace(),
        contracts,
        allow_gemini=use_gemini,
    )

    assert applied == tuple(media_ids)
    for item in store.list_items():
        assert item.state == "queued"
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        assert payload["media_context"]["series_name"] == "Example Series"
        assert len(payload["media_context"]["episode_assignments"]) == 2
        assert all(
            assignment["provisional_match"] is use_gemini
            for assignment in payload["media_context"]["episode_assignments"]
        )
