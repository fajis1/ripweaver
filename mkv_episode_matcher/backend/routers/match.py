import asyncio
import hashlib
import json
import threading
from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from mkv_episode_matcher.backend.dependencies import (
    get_engine,
    get_ffprobe_inspector,
    get_pipeline_contract_root,
    get_pipeline_queue_store,
)
from mkv_episode_matcher.backend.gemini_fallback import execute_gemini_fallback
from mkv_episode_matcher.backend.socket_manager import get_manager
from mkv_episode_matcher.backend.unmatched_disc_analysis import (
    GeminiAnalysisError,
    execute_unmatched_disc_analysis,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.core.engine import MatchEngineV2
from mkv_episode_matcher.media.ffprobe_runner import FFprobeError, resolve_ffprobe_path
from mkv_episode_matcher.media.play_all_detection import (
    MatchedEpisodeEvidence,
    detect_play_all,
)
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
    build_artifact,
)

router = APIRouter(prefix="/match", tags=["match"])

class MatchRequest(BaseModel):
    files: List[str]
    series_name: Optional[str] = None
    season: Optional[int] = None

class MatchResponse(BaseModel):
    status: str
    job_id: str


class UnmatchedMatchRequest(BaseModel):
    files: list[str] = Field(min_length=1, max_length=999)
    series_name: str = Field(min_length=1, max_length=160)
    season: int | None = Field(default=None, ge=0, le=99)
    confirm_media_read: bool = False
    confirm_provider_lookup: bool = False
    confirm_external_fallback: bool = False


class SmartFolderMatchRequest(BaseModel):
    files: list[str] = Field(min_length=1, max_length=999)
    series_name: str | None = Field(default=None, max_length=160)
    content_hint: str | None = Field(default=None, pattern="^(tv|movie|extras|mixed)$")
    confirm_media_read: bool = False
    confirm_provider_lookup: bool = False
    confirm_external_fallback: bool = False


def _directory_batch_fingerprint(files: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for index, path in enumerate(files):
        stat = path.stat()
        digest.update(
            f"{index}\0{path.name}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode()
        )
    return digest.hexdigest()[:16]


def _directory_source_paths(values: list[str]) -> tuple[Path, ...]:
    paths = tuple(Path(value).expanduser().resolve() for value in values)
    if len(set(paths)) != len(paths):
        raise PipelineQueueError("Directory selection contains duplicate files")
    for path in paths:
        if path.suffix.casefold() != ".mkv" or not path.is_file():
            raise PipelineQueueError("Directory selection contains an invalid MKV")
        if path.stat().st_size <= 0:
            raise PipelineQueueError("Directory selection contains an empty MKV")
    return paths


def _queue_unmatched_directory(
    *,
    store: PipelineQueueStore,
    contract_root: Path,
    files: tuple[Path, ...],
    series_name: str,
    season: int | None,
    inspections: tuple[object, ...],
) -> tuple[str, str]:
    fingerprint = _directory_batch_fingerprint(files)
    contract_root = contract_root.resolve()
    contract_root.mkdir(parents=True, exist_ok=True)
    normalized_series = " ".join(series_name.split())
    expected_indexes = list(range(len(files)))
    for index, (source, inspection) in enumerate(zip(files, inspections, strict=True)):
        media_id = f"directory-{fingerprint}-title-{index:03d}"
        source_size = source.stat().st_size
        duration = inspection.media.duration_seconds
        payload = {
            "schema_version": 1,
            "mode": "verified-directory-unmatched-contract",
            "media_id": media_id,
            "source_path": str(source),
            "source_size_bytes": source_size,
            "duration_seconds": duration,
            "warning_count": 0,
            "disc_fingerprint": fingerprint,
            "title_index": index,
            "disc_expected_title_indexes": expected_indexes,
            "media_context": {
                "series_name": normalized_series,
                "season": season,
                "content_hint": "tv",
                "handbrake_profile_id": None,
                "episode_assignments": [],
                "existing_output_policy": "preserve",
            },
        }
        contract = contract_root / f"{media_id}.verified-rip.json"
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if contract.exists():
            if contract.read_text(encoding="utf-8") != serialized:
                raise PipelineQueueError("Directory batch contract already differs")
        else:
            contract.write_text(serialized, encoding="utf-8")
        item = store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        if item.state == "queued":
            store.hold_for_review(media_id, "unmatched_disc_analysis_required")
    return fingerprint, normalized_series


def _infer_folder_series(files: tuple[Path, ...]) -> str | None:
    parent = files[0].parent
    if any(path.parent != parent for path in files):
        return None
    if parent.name.casefold() in {"unmatched", "unknown", "season 00"}:
        parent = parent.parent
    name = " ".join(parent.name.replace("_", " ").replace("-", " ").split())
    return name or None


def _classify_folder_durations(
    durations: tuple[float, ...], explicit_hint: str | None, series_name: str | None
) -> tuple[str, str]:
    if explicit_hint is not None:
        return explicit_hint, "reviewed hint"
    episode_like = [value for value in durations if 8 * 60 <= value <= 75 * 60]
    if len(episode_like) >= max(2, round(len(durations) * 0.6)):
        ordered = sorted(episode_like)
        median = ordered[len(ordered) // 2]
        clustered = [value for value in episode_like if abs(value - median) <= median * 0.25]
        if len(clustered) >= max(2, round(len(durations) * 0.5)):
            reason = "dominant episode-length runtime cluster"
            if series_name:
                reason += " with canonical series context"
            return "tv", reason
    longest = max(durations)
    others = sum(value for value in durations if value != longest)
    if longest >= 60 * 60 and (len(durations) == 1 or longest >= others * 0.7):
        return "movie", "one dominant feature-length title"
    if any(value >= 40 * 60 for value in durations):
        return "mixed", "feature-length and shorter titles coexist"
    return "extras", "short non-episodic title set"


def _queue_descriptive_directory(
    *, store: PipelineQueueStore, contract_root: Path, files: tuple[Path, ...],
    inspections: tuple[object, ...], content_hint: str, release_title: str | None,
) -> tuple[str, tuple[str, ...]]:
    fingerprint = _directory_batch_fingerprint(files)
    contract_root.mkdir(parents=True, exist_ok=True)
    media_ids = []
    for index, (source, inspection) in enumerate(zip(files, inspections, strict=True)):
        media_id = f"directory-{fingerprint}-title-{index:03d}"
        payload = {
            "schema_version": 1,
            "mode": "verified-directory-smart-contract",
            "media_id": media_id,
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "duration_seconds": inspection.media.duration_seconds,
            "disc_fingerprint": fingerprint,
            "title_index": index,
            "disc_expected_title_indexes": list(range(len(files))),
            "media_context": {
                "series_name": release_title,
                "season": None,
                "content_hint": content_hint,
                "handbrake_profile_id": None,
                "special_feature_catalog_id": None,
                "special_feature_assignments": [],
                "special_feature_library_title": release_title,
                "existing_output_policy": "preserve",
            },
        }
        contract = contract_root / f"{media_id}.verified-rip.json"
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if contract.exists() and contract.read_text(encoding="utf-8") != serialized:
            raise PipelineQueueError("Directory batch contract already differs")
        if not contract.exists():
            contract.write_text(serialized, encoding="utf-8")
        item = store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        if item.state == "queued":
            store.hold_for_review(media_id, "gemini_evidence_required")
        media_ids.append(media_id)
    return fingerprint, tuple(media_ids)


def _classify_legacy_play_all(matches, failures) -> list[dict[str, object]]:
    """Classify failed legacy files after ordinary matches establish coverage."""

    if not matches or not failures:
        return []
    try:
        config = get_config_manager().load()
        executable = resolve_ffprobe_path(config.ffprobe_path)
        inspector = get_ffprobe_inspector()
        matched = []
        for result in matches:
            source = Path(result.matched_file)
            inspection = inspector(executable, source, timeout_seconds=60)
            matched.append(
                MatchedEpisodeEvidence(
                    file_id=str(source),
                    season=result.episode_info.season,
                    episode=result.episode_info.episode,
                    duration_seconds=inspection.media.duration_seconds,
                    size_bytes=source.stat().st_size,
                )
            )
        detected = []
        for failure in failures:
            source = Path(failure.original_file)
            inspection = inspector(executable, source, timeout_seconds=60)
            evidence = detect_play_all(
                candidate_file_id=str(source),
                candidate_duration_seconds=inspection.media.duration_seconds,
                candidate_size_bytes=source.stat().st_size,
                matched_episodes=tuple(matched),
            )
            if evidence is not None:
                detected.append(
                    {
                        "file": str(source),
                        "component_episode_ids": list(evidence.component_episode_ids),
                        "duration_ratio": round(evidence.duration_ratio, 6),
                        "size_ratio": (
                            round(evidence.size_ratio, 6)
                            if evidence.size_ratio is not None
                            else None
                        ),
                    }
                )
        return detected
    except (FFprobeError, OSError, AttributeError, TypeError, ValueError):
        return []

# Simple in-memory job store for demo purposes
# In production, use Redis or database
jobs = {}

@router.post("/start")
async def start_match(
    request: MatchRequest, 
    background_tasks: BackgroundTasks,
    engine: MatchEngineV2 = Depends(get_engine)
):
    job_id = f"job_{len(jobs) + 1}"
    jobs[job_id] = {"status": "pending", "results": [], "logs": []}
    
    background_tasks.add_task(process_matching_job, job_id, request, engine)
    
    return {"status": "started", "job_id": job_id}


@router.post("/start-unmatched")
def start_unmatched_match(
    request: UnmatchedMatchRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
    inspector=Depends(get_ffprobe_inspector),
) -> dict[str, object]:
    """Verify selected MKVs and start the durable unmatched TV workflow."""

    if not request.confirm_media_read or not request.confirm_provider_lookup:
        raise HTTPException(
            status_code=400,
            detail="Media-read and episode-catalogue lookup confirmations are required",
        )
    try:
        files = _directory_source_paths(request.files)
        config = get_config_manager().load()
        executable = resolve_ffprobe_path(config.ffprobe_path)
        inspections = tuple(
            inspector(executable, source, timeout_seconds=60) for source in files
        )
        if any(item.media.duration_seconds <= 0 for item in inspections):
            raise PipelineQueueError("Directory selection contains an unreadable MKV")
        fingerprint, normalized_series = _queue_unmatched_directory(
            store=store,
            contract_root=contract_root,
            files=files,
            series_name=request.series_name,
            season=request.season,
            inspections=inspections,
        )
    except (FFprobeError, OSError, PipelineQueueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    media_ids = tuple(
        f"directory-{fingerprint}-title-{index:03d}"
        for index in range(len(files))
    )

    def run() -> None:
        try:
            execute_unmatched_disc_analysis(
                store,
                fingerprint,
                normalized_series,
                config,
                get_engine().asr,
                contract_root,
                season=request.season,
                allow_gemini=request.confirm_external_fallback,
            )
        except Exception as exc:
            code = (
                exc.review_code
                if isinstance(exc, GeminiAnalysisError)
                else (
                    "independent_episode_evidence_required"
                    if str(exc) == "Independent episode evidence requires review"
                    else "all_season_analysis_failed"
                )
            )
            for media_id in media_ids:
                try:
                    if store.get(media_id).state == "review_required":
                        store.choose_review_path(media_id, code)
                except PipelineQueueError:
                    pass

    threading.Thread(target=run, name="directory-unmatched-analysis", daemon=True).start()
    return {
        "status": "started",
        "disc_fingerprint": fingerprint,
        "item_count": len(files),
        "all_season": True,
    }


@router.post("/start-smart-folder")
def start_smart_folder_match(  # noqa: C901 - guarded classification workflow
    request: SmartFolderMatchRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
    inspector=Depends(get_ffprobe_inspector),
) -> dict[str, object]:
    """Classify verified existing MKVs and admit them to the durable pipeline."""

    if not request.confirm_media_read or not request.confirm_provider_lookup:
        raise HTTPException(
            status_code=400,
            detail="Media-read and metadata-provider confirmations are required",
        )
    try:
        files = _directory_source_paths(request.files)
        config = get_config_manager().load()
        executable = resolve_ffprobe_path(config.ffprobe_path)
        inspections = tuple(
            inspector(executable, source, timeout_seconds=60) for source in files
        )
        durations = tuple(item.media.duration_seconds for item in inspections)
        if any(value <= 0 for value in durations):
            raise PipelineQueueError("Directory selection contains an unreadable MKV")
        reviewed_series = (
            " ".join(request.series_name.split())
            if request.series_name and request.series_name.strip()
            else _infer_folder_series(files)
        )
        classification, reason = _classify_folder_durations(
            durations, request.content_hint, reviewed_series
        )
        if classification == "tv":
            if not reviewed_series:
                raise PipelineQueueError(
                    "TV-like files require a canonical series name"
                )
            fingerprint, reviewed_series = _queue_unmatched_directory(
                store=store,
                contract_root=contract_root,
                files=files,
                series_name=reviewed_series,
                season=None,
                inspections=inspections,
            )
            media_ids = tuple(
                f"directory-{fingerprint}-title-{index:03d}"
                for index in range(len(files))
            )

            def run_tv() -> None:
                try:
                    execute_unmatched_disc_analysis(
                        store,
                        fingerprint,
                        reviewed_series,
                        config,
                        get_engine().asr,
                        contract_root,
                        allow_gemini=request.confirm_external_fallback,
                    )
                except Exception as exc:
                    code = (
                        exc.review_code
                        if isinstance(exc, GeminiAnalysisError)
                        else "all_season_analysis_failed"
                    )
                    for media_id in media_ids:
                        try:
                            if store.get(media_id).state == "review_required":
                                store.choose_review_path(media_id, code)
                        except PipelineQueueError:
                            pass

            threading.Thread(
                target=run_tv, name="directory-smart-tv-analysis", daemon=True
            ).start()
        else:
            fingerprint, media_ids = _queue_descriptive_directory(
                store=store,
                contract_root=contract_root,
                files=files,
                inspections=inspections,
                content_hint=classification,
                release_title=reviewed_series,
            )
            if request.confirm_external_fallback:
                for media_id in media_ids:
                    store.choose_review_path(media_id, "gemini_analysis_running")

                def run_descriptive() -> None:
                    try:
                        applied = set(execute_gemini_fallback(
                            store, media_ids, config, get_engine().asr, contract_root
                        ))
                        for media_id in set(media_ids) - applied:
                            store.choose_review_path(
                                media_id, "gemini_descriptive_review_required"
                            )
                    except Exception:
                        for media_id in media_ids:
                            try:
                                if store.get(media_id).state == "review_required":
                                    store.choose_review_path(
                                        media_id, "gemini_analysis_failed"
                                    )
                            except PipelineQueueError:
                                pass

                threading.Thread(
                    target=run_descriptive,
                    name="directory-smart-descriptive-analysis",
                    daemon=True,
                ).start()
    except (FFprobeError, OSError, PipelineQueueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": "started",
        "classification": classification,
        "classification_reason": reason,
        "disc_fingerprint": fingerprint,
        "item_count": len(files),
        "series_name": reviewed_series,
        "gemini_enabled": request.confirm_external_fallback,
    }

@router.get("/status/{job_id}")
async def get_job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})

async def process_matching_job(job_id: str, request: MatchRequest, engine: MatchEngineV2):
    manager = get_manager()
    loop = asyncio.get_event_loop()
    
    def progress_callback(current, total, filename):
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({
                "type": "progress",
                "job_id": job_id,
                "current": current,
                "total": total,
                "filename": str(filename)
            }),
            loop
        )

    def phase_callback(phase, message):
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({
                "type": "phase_update",
                "job_id": job_id,
                "phase": phase,
                "message": message
            }),
            loop
        )

    try:
        jobs[job_id]["status"] = "processing"
        await manager.broadcast({"type": "job_update", "job_id": job_id, "status": "processing"})
        
        # Determine strict or auto season
        season_override = request.season
        
        paths = [Path(f) for f in request.files]
        parent_dir = paths[0].parent if paths else Path(".")
        
        # Run blocking engine call in thread pool
        matches, failures = await asyncio.to_thread(
            engine.process_path,
            path=parent_dir,
            season_override=season_override,
            files_override=paths,
            json_output=True,
            progress_callback=progress_callback,
            phase_callback=phase_callback
        )

        play_all_aggregates = await asyncio.to_thread(
            _classify_legacy_play_all, matches, failures
        )
        play_all_files = {
            item["file"]
            for item in play_all_aggregates
            if isinstance(item.get("file"), str)
        }
        failures = [
            failure
            for failure in failures
            if str(failure.original_file) not in play_all_files
        ]
        
        # Serialize results
        serialized_matches = []
        for m in matches:
             serialized_matches.append({
                 "original_file": str(m.matched_file),
                 "series": m.episode_info.series_name,
                 "season": m.episode_info.season,
                 "episode": m.episode_info.episode,
                 "title": m.episode_info.title,
                 "confidence": m.confidence
             })
             
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["results"] = serialized_matches
        jobs[job_id]["failures"] = [str(f.original_file) for f in failures]
        jobs[job_id]["play_all_aggregates"] = play_all_aggregates
        
        await manager.broadcast({
            "type": "job_complete",
            "job_id": job_id,
            "status": "completed",
            "results": serialized_matches,
            "failures": jobs[job_id]["failures"],
            "play_all_aggregates": play_all_aggregates,
        })
        
    except Exception as e:
        from mkv_episode_matcher.core.credentials import ApiCredentialError

        credential_url = None
        error_type = "service"
        if isinstance(e, ApiCredentialError):
            error_message = str(e)
            credential_url = e.management_url
            error_type = "credential"
        else:
            error_message = f"Matching failed ({type(e).__name__})."
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = error_message
        jobs[job_id]["error_type"] = error_type
        jobs[job_id]["credential_url"] = credential_url
        await manager.broadcast({
            "type": "job_failed",
            "job_id": job_id,
            "error": error_message,
            "error_type": error_type,
            "credential_url": credential_url,
        })
