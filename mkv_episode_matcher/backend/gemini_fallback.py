"""Bounded special-feature evidence and Gemini assignment workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.backend.identification_dossier import collect_dossier_evidence
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiDescriptiveRanker,
    GeminiDescriptiveResult,
    GeminiEpisodeRanker,
)
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
    build_artifact,
)


def _descriptive_release_hint(payload: dict, media_id: str) -> str:
    context = payload.get("media_context", {})
    series_name = context.get("series_name") if isinstance(context, dict) else None
    if isinstance(series_name, str) and series_name.strip().casefold() != "unmatched":
        return series_name
    prefix = media_id.split("--disc-", 1)[0]
    recovered = " ".join(prefix.replace("_", "-").split("-")).strip()
    return recovered or "Unknown disc"


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
    requested_media_ids = media_ids
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
    visual_categories: dict[str, str] = {}

    def record_visual_review(media_id: str, category: str) -> None:
        store.record_silent_video_review(media_id, category)
        visual_categories[media_id] = category

    evidence, dossier = collect_dossier_evidence(
        tuple(zip(held, payloads, strict=True)),
        config,
        asr,
        contract_root,
        True,
        record_visual_review,
    )
    visually_held = {
        media_id
        for media_id, category in visual_categories.items()
        if category in {"likely_warning_screen", "likely_disc_menu"}
    }
    for media_id in visually_held:
        store.choose_review_path(media_id, "visual_content_review_required")
    eligible = tuple(
        (item, payload, item_evidence)
        for item, payload, item_evidence in zip(held, payloads, evidence, strict=True)
        if item.media_id not in visually_held
    )
    if not eligible:
        return tuple(
            media_id for media_id in requested_media_ids if media_id in visually_held
        )
    held = [entry[0] for entry in eligible]
    payloads = [entry[1] for entry in eligible]
    evidence = tuple(entry[2] for entry in eligible)
    media_ids = tuple(item.media_id for item in held)
    first_context = payloads[0]["media_context"]
    series_name = str(first_context.get("series_name") or "").strip()
    tv_series_context = (
        first_context.get("content_hint") not in {"movie", "extras"}
        and bool(series_name)
        and series_name.casefold() not in {"unmatched", "unknown"}
    )
    durations = {item.file_id: item.duration_seconds for item in evidence}
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
    release_hint = _descriptive_release_hint(payloads[0], held[0].media_id)
    related_matches = {}
    if descriptive and tv_series_context:
        from mkv_episode_matcher.backend.related_movie_analysis import (
            match_related_tv_movies,
        )

        excluded_movie_ids = set()
        for payload in payloads:
            assignments = payload["media_context"].get(
                "special_feature_assignments", []
            )
            for assignment in assignments if isinstance(assignments, list) else []:
                if not isinstance(assignment, dict):
                    continue
                for candidate_id in assignment.get("candidate_feature_ids", []):
                    match = re.fullmatch(r"tmdb-movie-(\d+)", str(candidate_id))
                    if match is not None:
                        excluded_movie_ids.add(int(match.group(1)))
        try:
            related_matches, related_diagnostics = match_related_tv_movies(
                evidence,
                series_name,
                config,
                asr,
                excluded_tmdb_ids=frozenset(excluded_movie_ids),
            )
        except Exception as exc:
            dossier.record_attempt(
                media_ids,
                branch="tv-movie",
                disposition="failed",
                summary={"reason": type(exc).__name__},
            )
        else:
            for media_id, summary in related_diagnostics.items():
                dossier.record_attempt(
                    (media_id,),
                    branch="tv-movie",
                    disposition=(
                        "matched" if media_id in related_matches else "review"
                    ),
                    summary=summary,
                )

    comparison_evidence = tuple(
        item for item in evidence if item.file_id not in related_matches
    )
    comparison_ids = tuple(item.file_id for item in comparison_evidence)
    results = {}
    if comparison_evidence:
        dossier.record_attempt(
            comparison_ids,
            branch="tv-bonus" if tv_series_context else "movie-bonus",
            disposition="started",
            summary={"catalogue_available": catalogue is not None},
        )
    if descriptive and comparison_evidence:
        review = GeminiDescriptiveRanker(
            model=config.gemini_model
        ).describe_with_configured_keys(
            comparison_evidence,
            release_hint=release_hint,
            prior_attempts={
                media_id: dossier.safe_attempts(media_id) for media_id in comparison_ids
            },
        )
        results.update({item.file_id: item for item in review.matches})
    elif not descriptive:
        review = GeminiEpisodeRanker(
            model=config.gemini_model
        ).rank_with_configured_keys(
            comparison_evidence,
            candidates,
            prior_attempts={
                media_id: dossier.safe_attempts(media_id) for media_id in comparison_ids
            },
        )
        results.update({item.file_id: item for item in review.matches})
    for media_id, movie_match in related_matches.items():
        candidate = movie_match.candidate
        results[media_id] = GeminiDescriptiveResult(
            file_id=media_id,
            content_kind="movie",
            suggested_title=candidate.title,
            year=candidate.release_year,
            confidence=movie_match.confidence,
            evidence=(
                "Matched ordinary movie subtitles using independent dialogue anchors.",
            ),
            summary=(
                "TMDb runtime metadata and ordinary movie subtitles matched "
                "independent dialogue anchors from this TV-disc title."
            ),
        )
    applied = []
    contract_root.mkdir(parents=True, exist_ok=True)
    for item, payload in zip(held, payloads, strict=True):
        result = results.get(item.media_id)
        if result is None:
            continue
        context = payload["media_context"]
        title_index = _title_index(item.media_id)
        assignments = context.get("special_feature_assignments", [])
        if not isinstance(assignments, list):
            assignments = []
            context["special_feature_assignments"] = assignments
        if descriptive:
            if result.content_kind in {"menu", "unknown", "tv_episode"}:
                continue
            if tv_series_context and result.content_kind not in {"extra", "movie"}:
                continue
            related_movie = related_matches.get(item.media_id)
            feature_id = (
                f"tmdb-movie-{related_movie.candidate.tmdb_id}"
                if related_movie is not None
                else f"provisional-title-{title_index:03d}"
            )
            feature_title = result.suggested_title
            feature_summary = (result.summary or " ".join(result.evidence)).strip()[
                :320
            ]
            media_kind = result.content_kind
            feature_folder = (
                "Extras" if tv_series_context and media_kind != "movie" else "other"
            )
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
                match_summary=feature_summary,
                candidate_feature_ids=[feature_id],
                jellyfin_folder=feature_folder,
                fallback_name_policy="none",
                media_kind=media_kind,
                provisional_match=related_movie is None,
                gemini_confidence=result.confidence,
                library_kind=(
                    "movie"
                    if media_kind == "movie"
                    else "tv"
                    if tv_series_context
                    else "movie"
                ),
            )
            if related_movie is not None:
                existing.update(
                    tmdb_movie_id=related_movie.candidate.tmdb_id,
                    identification_method="tv-related-movie-opensubtitles",
                )
            if media_kind == "movie":
                context["special_feature_library_title"] = feature_title
                context["special_feature_library_year"] = result.year
            elif tv_series_context:
                context["special_feature_library_title"] = series_name
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
    if comparison_ids:
        comparison_applied = tuple(
            media_id for media_id in comparison_ids if media_id in applied
        )
        dossier.record_attempt(
            comparison_ids,
            branch="tv-bonus" if tv_series_context else "movie-bonus",
            disposition=(
                "matched"
                if len(comparison_applied) == len(comparison_ids)
                else "review"
            ),
            summary={
                "matched_count": len(comparison_applied),
                "item_count": len(comparison_ids),
                "catalogue_available": catalogue is not None,
            },
        )
    unresolved = tuple(media_id for media_id in media_ids if media_id not in applied)
    result_by_id = results
    should_try_tv = (
        descriptive
        and len(unresolved) >= 2
        and all(
            media_id in result_by_id
            and result_by_id[media_id].content_kind in {"tv_episode", "unknown"}
            for media_id in unresolved
        )
        and not any(dossier.attempted(media_id, "tv-local") for media_id in unresolved)
    )
    if should_try_tv:
        try:
            from mkv_episode_matcher.backend.unmatched_disc_analysis import (
                execute_unmatched_disc_analysis,
            )

            fingerprint = payloads[0].get("disc_fingerprint")
            if not isinstance(fingerprint, str):
                raise PipelineQueueError("Disc fingerprint is unavailable")
            for media_id in unresolved:
                store.choose_review_path(media_id, "all_season_analysis_running")
            tv_applied = execute_unmatched_disc_analysis(
                store,
                fingerprint,
                release_hint,
                config,
                asr,
                contract_root,
                allow_gemini=config.automatic_gemini_ambiguity_fallback,
                allow_content_fallback=False,
            )
            applied.extend(tv_applied)
            unresolved = tuple(
                media_id for media_id in media_ids if media_id not in applied
            )
        except Exception as exc:
            dossier.record_attempt(
                unresolved,
                branch="gemini-synthesis",
                disposition="failed",
                summary={"reason": type(exc).__name__},
            )
    if unresolved:
        dossier.record_attempt(
            unresolved,
            branch="gemini-synthesis",
            disposition="review",
            summary={"reason": "all_content_routes_exhausted"},
        )
        for media_id in unresolved:
            store.choose_review_path(media_id, "gemini_descriptive_review_required")
    handled = visually_held | set(applied)
    return tuple(media_id for media_id in requested_media_ids if media_id in handled)
