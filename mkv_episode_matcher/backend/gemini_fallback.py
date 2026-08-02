"""Bounded special-feature evidence and Gemini assignment workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.ffprobe_runner import inspect_mkv, resolve_ffprobe_path
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiDescriptiveRanker,
    GeminiEpisodeRanker,
    UnmatchedFileEvidence,
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

_TITLE_INDEX_PATTERN = re.compile(r"-title-(\d{3})(?:-|$)")


def _title_index(media_id: str) -> int:
    match = _TITLE_INDEX_PATTERN.search(media_id)
    if match is None:
        raise PipelineQueueError("Pipeline media ID has no title index")
    return int(match.group(1))


def _catalog(config: Config, catalog_id: str) -> dict:
    roots = (
        Path(__file__).resolve().parents[1] / "feature_catalogs",
        config.cache_dir.parent / "feature-catalogs",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("catalog_id") == catalog_id:
                return payload
    raise PipelineQueueError("Reviewed special-feature catalogue is unavailable")


def execute_gemini_fallback(  # noqa: C901 - linear guarded workflow
    store: PipelineQueueStore,
    media_ids: tuple[str, ...],
    config: Config,
    asr,
    contract_root: Path,
) -> tuple[str, ...]:
    """Read exact held MKVs, send bounded excerpts, then requeue confident matches."""

    if not media_ids:
        raise PipelineQueueError("No Gemini-held items were selected")
    held = [store.get(media_id) for media_id in media_ids]
    payloads = []
    catalog_id = None
    catalog_seen = False
    for item in held:
        if item.review_code not in {
            "gemini_evidence_required",
            "gemini_analysis_running",
        }:
            raise PipelineQueueError("Selected item is not awaiting Gemini evidence")
        try:
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Verified rip contract is unavailable") from exc
        context = payload.get("media_context")
        if not isinstance(context, dict):
            raise PipelineQueueError("Special-feature context is unavailable")
        current_catalog = context.get("special_feature_catalog_id")
        if current_catalog is not None and not isinstance(current_catalog, str):
            raise PipelineQueueError("Special-feature catalogue ID is invalid")
        if catalog_seen and current_catalog != catalog_id:
            raise PipelineQueueError("Gemini batch must use one reviewed catalogue")
        catalog_id = current_catalog
        catalog_seen = True
        payloads.append(payload)

    catalogue = _catalog(config, catalog_id) if catalog_id is not None else None
    features = (
        {
            item["feature_id"]: item
            for item in catalogue.get("features", [])
            if isinstance(item, dict)
            and isinstance(item.get("feature_id"), str)
            and isinstance(item.get("title"), str)
        }
        if catalogue is not None
        else {}
    )
    assigned_titles = {
        entry.get("matched_title")
        for payload in payloads
        for entry in payload["media_context"].get("special_feature_assignments", [])
        if isinstance(entry, dict) and entry.get("classification") == "matched-feature"
    }
    remaining = [
        item for item in features.values() if item["title"] not in assigned_titles
    ]
    ffprobe = resolve_ffprobe_path(config.ffprobe_path)
    ffmpeg = resolve_ffmpeg_path(config.ffmpeg_path)
    transcript_items = []
    inspected_durations = {}
    for item, payload in zip(held, payloads, strict=True):
        source = Path(str(payload.get("source_path", "")))
        inspection = inspect_mkv(ffprobe, source, timeout_seconds=60)
        inspected_durations[item.media_id] = inspection.media.duration_seconds
        transcript_items.append(
            TranscriptBatchItem(item.media_id, source, inspection.media)
        )
    transcripts = collect_transcript_batch(
        tuple(transcript_items),
        asr,
        FFmpegSampleExtractor(ffmpeg),
        model_name=config.asr_model_name,
    )
    transcript_files = {item.file_id: item for item in transcripts.files}
    evidence = tuple(
        UnmatchedFileEvidence(
            file_id=item.media_id,
            duration_seconds=inspected_durations[item.media_id],
            transcript_excerpts=tuple(
                window.text[:600]
                for window in transcript_files.get(item.media_id, ()).windows
                if window.text
            )[:3]
            if item.media_id in transcript_files
            else (),
        )
        for item in held
    )
    durations = inspected_durations
    runtime_candidates = [
        item
        for item in remaining
        if isinstance(item.get("runtime_seconds"), int | float)
        and any(
            abs(float(item["runtime_seconds"]) - duration) <= 20
            for duration in durations.values()
        )
    ]
    candidate_items = (
        runtime_candidates if len(runtime_candidates) >= len(held) else remaining
    )
    candidates = tuple(
        EpisodeCatalogEntry(
            episode_id=item["feature_id"],
            season=0,
            episode=index + 1,
            title=item["title"],
            overview="",
            runtime_seconds=float(item["runtime_seconds"]),
        )
        for index, item in enumerate(candidate_items)
        if isinstance(item.get("runtime_seconds"), int | float)
    )
    descriptive = catalog_id is None
    if descriptive:
        release_hint = str(
            payloads[0].get("media_context", {}).get("series_name") or "Unknown disc"
        )
        review = GeminiDescriptiveRanker(
            model=config.gemini_model
        ).describe_with_configured_keys(evidence, release_hint=release_hint)
    else:
        review = GeminiEpisodeRanker(
            model=config.gemini_model
        ).rank_with_configured_keys(evidence, candidates)
    results = {item.file_id: item for item in review.matches}
    applied = []
    contract_root.mkdir(parents=True, exist_ok=True)
    for item, payload in zip(held, payloads, strict=True):
        result = results[item.media_id]
        context = payload["media_context"]
        title_index = _title_index(item.media_id)
        assignments = context.get("special_feature_assignments", [])
        if not isinstance(assignments, list):
            assignments = []
            context["special_feature_assignments"] = assignments
        if descriptive:
            if result.content_kind in {"menu", "unknown", "tv_episode"}:
                continue
            feature_id = f"provisional-title-{title_index:03d}"
            feature_title = result.suggested_title
            feature_folder = "other"
            media_kind = result.content_kind
            existing = next(
                (
                    assignment
                    for assignment in assignments
                    if isinstance(assignment, dict)
                    and assignment.get("title_index") == title_index
                ),
                None,
            )
            if existing is None:
                existing = {"title_index": title_index}
                assignments.append(existing)
            existing.update(
                classification="matched-feature",
                matched_title=feature_title,
                candidate_feature_ids=[feature_id],
                jellyfin_folder=feature_folder,
                fallback_name_policy="none",
                media_kind=media_kind,
                provisional_match=True,
                gemini_confidence=result.confidence,
            )
            if media_kind == "movie":
                context["special_feature_library_title"] = feature_title
                context["special_feature_library_year"] = result.year
            elif not context.get("special_feature_library_title"):
                context["special_feature_library_title"] = release_hint
        else:
            if result.episode_id is None:
                continue
            feature = features[result.episode_id]
        for assignment in assignments:
            if (
                isinstance(assignment, dict)
                and assignment.get("title_index") == title_index
                and not descriptive
            ):
                assignment.update(
                    classification="matched-feature",
                    matched_title=feature["title"],
                    candidate_feature_ids=[result.episode_id],
                    fallback_name_policy="none",
                    provisional_match=(
                        not next(
                            evidence_item.transcript_excerpts
                            for evidence_item in evidence
                            if evidence_item.file_id == item.media_id
                        )
                        or result.confidence < 0.75
                    ),
                    gemini_confidence=result.confidence,
                )
        revised = (
            contract_root
            / f"{item.media_id}.gemini-{uuid4().hex[:12]}.verified-rip.json"
        )
        revised.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        store.apply_reviewed_identification_input(
            item.media_id, build_artifact("rip", revised)
        )
        applied.append(item.media_id)
    return tuple(applied)
