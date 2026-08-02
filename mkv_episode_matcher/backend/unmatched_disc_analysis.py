"""Disc-level all-season identification for seasonless television rips."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.evidence_bundle import (
    SavedFileEvidence,
    SavedTranscriptWindow,
)
from mkv_episode_matcher.media.ffprobe_runner import inspect_mkv, resolve_ffprobe_path
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiEpisodeRanker,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.media.sequence_matcher import (
    SequenceGroup,
    plan_disc_sequences,
)
from mkv_episode_matcher.media.transcript_batch import (
    FFmpegSampleExtractor,
    TranscriptBatchItem,
    collect_transcript_batch,
    resolve_ffmpeg_path,
)
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
    build_artifact,
)
from mkv_episode_matcher.tmdb_client import fetch_aired_episode_catalog_for_show


def execute_unmatched_disc_analysis(  # noqa: C901 - guarded disc-level workflow
    store: PipelineQueueStore,
    disc_fingerprint: str,
    series_name: str,
    config: Config,
    asr,
    contract_root: Path,
    *,
    season: int | None = None,
    allow_gemini: bool = False,
) -> tuple[str, ...]:
    """Transcribe held titles and compare them to the reviewed episode scope."""

    selected_by_title = {}
    for item in store.list_items():
        if item.stage != "identify" or item.state != "review_required":
            continue
        try:
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Verified rip contract is unavailable") from exc
        if payload.get("disc_fingerprint") != disc_fingerprint:
            continue
        title_index = payload.get("title_index")
        if not isinstance(title_index, int) or isinstance(title_index, bool):
            continue
        previous = selected_by_title.get(title_index)
        if previous is None or item.updated_at >= previous[0].updated_at:
            selected_by_title[title_index] = (item, payload)
    selected = [selected_by_title[index] for index in sorted(selected_by_title)]
    if len(selected) < 2:
        raise PipelineQueueError(
            "At least two held titles are required for disc sequence analysis"
        )

    catalog = fetch_aired_episode_catalog_for_show(series_name)
    if season is not None:
        catalog = tuple(item for item in catalog if item.season == season)
    if not catalog:
        raise PipelineQueueError(
            "Episode catalogue is unavailable for the reviewed scope"
        )
    ffprobe = resolve_ffprobe_path(config.ffprobe_path)
    ffmpeg = resolve_ffmpeg_path(config.ffmpeg_path)
    transcript_items = []
    for item, payload in selected:
        source = Path(str(payload.get("source_path", "")))
        inspection = inspect_mkv(ffprobe, source, timeout_seconds=60)
        transcript_items.append(
            TranscriptBatchItem(item.media_id, source, inspection.media)
        )
    transcripts = collect_transcript_batch(
        tuple(transcript_items),
        asr,
        FFmpegSampleExtractor(ffmpeg),
        model_name=config.asr_model_name,
    )
    evidence = tuple(
        SavedFileEvidence(
            file_id=item.file_id,
            duration_seconds=item.duration_seconds,
            windows=tuple(
                SavedTranscriptWindow(window.start_seconds, window.text)
                for window in item.windows
                if window.text.strip()
            ),
        )
        for item in transcripts.files
    )
    if len(evidence) != len(selected) or any(not item.windows for item in evidence):
        raise PipelineQueueError(
            "All-season analysis did not obtain usable evidence for every title"
        )
    plan = plan_disc_sequences(
        evidence,
        catalog,
        (
            SequenceGroup(
                f"disc-{disc_fingerprint}", tuple(item.file_id for item in evidence)
            ),
        ),
    )
    catalog_by_id = {entry.episode_id: entry for entry in catalog}
    provisional: dict[str, float] = {}
    if plan.disposition == "proposed":
        proposed = {
            item.file_id: catalog_by_id[item.proposed_episode]
            for group in plan.groups
            for item in group.items
        }
    elif allow_gemini and config.automatic_gemini_ambiguity_fallback:
        try:
            review = GeminiEpisodeRanker(
                model=config.gemini_model
            ).rank_with_configured_keys(
                tuple(
                    UnmatchedFileEvidence(
                        file_id=item.file_id,
                        duration_seconds=item.duration_seconds,
                        transcript_excerpts=tuple(
                            window.text[:600] for window in item.windows
                        )[:3],
                    )
                    for item in evidence
                ),
                catalog,
            )
            matches = {item.file_id: item for item in review.matches}
            if set(matches) != {item.file_id for item in evidence} or any(
                item.episode_id is None for item in matches.values()
            ):
                raise PipelineQueueError("Gemini did not identify every disc title")
            proposed = {
                file_id: catalog_by_id[str(match.episode_id)]
                for file_id, match in matches.items()
            }
            provisional = {
                file_id: match.confidence for file_id, match in matches.items()
            }
        except Exception as exc:
            raise PipelineQueueError(
                "Automatic Gemini all-season fallback failed"
            ) from exc
    else:
        raise PipelineQueueError("All-season sequence result requires review")
    assignments = [
        {
            "title_index": title_index,
            "season": proposed[item.media_id].season,
            "episode": proposed[item.media_id].episode,
            "title": proposed[item.media_id].title,
            "provisional_match": item.media_id in provisional,
            "gemini_confidence": provisional.get(item.media_id),
        }
        for title_index, (item, _payload) in sorted(selected_by_title.items())
    ]
    contract_root.mkdir(parents=True, exist_ok=True)
    applied = []
    for item, payload in selected:
        revised = dict(payload)
        context = dict(revised.get("media_context", {}))
        context.update(
            series_name=series_name.strip(),
            season=season,
            episode_assignments=assignments,
        )
        revised["media_context"] = context
        path = contract_root / f"{item.media_id}.all-season-{uuid4().hex[:12]}.json"
        path.write_text(
            json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        store.apply_reviewed_identification_input(
            item.media_id, build_artifact("rip", path)
        )
        applied.append(item.media_id)
    return tuple(applied)
