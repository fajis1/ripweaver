import json
import subprocess

from fastapi import APIRouter, Depends, HTTPException


def is_video_encoded(path: str) -> bool:
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return False

        data = json.loads(result.stdout)

        format_tags = data.get("format", {}).get("tags", {})
        encoder = format_tags.get("ENCODER", "") or format_tags.get("encoder", "")

        if "MakeMKV" in encoder:
            return False

        if "HandBrake" in encoder or "Lavf" in encoder:
            return True

        for stream in data.get("streams", []):
            tags = stream.get("tags", {})
            title = tags.get("title", "") or tags.get("TITLE", "")
            if "MakeMKV" in title:
                return False

        bitrate = int(data.get("format", {}).get("bit_rate", 0))
        if bitrate > 0 and bitrate < 15000000:
            return True

        return False

    except Exception:
        return False


import functools
import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mkv_episode_matcher.backend.dependencies import (
    get_pipeline_contract_root,
    get_pipeline_queue_store,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore

router = APIRouter(prefix="/triage", tags=["triage"])

WRAPPER_FOLDERS = {
    "mkv_matcher_staging",
    "staging",
    "readytoencode",
    "encoded",
    "tobeconverted",
    "to be placed",
    "tobeplaced",
    "to_be_placed",
    "unmatched",
    "temp",
    "queue",
    "rips",
    "rip",
}


@functools.lru_cache(maxsize=128)
def resolve_canonical_series_name(raw_name: str) -> str:
    """Resolve a raw folder or disc name to canonical TMDb / Gemini show name."""
    if not raw_name or raw_name.lower().replace(" ", "_") in WRAPPER_FOLDERS:
        return raw_name

    cleaned = re.sub(
        r"(?i)\b(?:three movies|movie collection|collection|vol(?:ume)?\s*\d+|disc\s*\d+)\b",
        "",
        raw_name,
    ).strip(" ._-")
    query_target = cleaned if cleaned else raw_name

    # 1. Direct TMDb TV search
    try:
        from mkv_episode_matcher.tmdb_client import _tmdb_get_json

        results = _tmdb_get_json("/search/tv", query=query_target).get("results", [])
        if results:
            return results[0]["name"]
    except Exception:
        pass

    # 2. Gemini fallback if configured
    try:
        from mkv_episode_matcher.core.environment import load_environment_settings
        from mkv_episode_matcher.media.gemini_series_resolver import (
            GeminiSeriesResolver,
        )
        from mkv_episode_matcher.tmdb_client import search_tv_show_candidates

        env = load_environment_settings()
        key = env.gemini_primary_api_key or env.gemini_paid_api_key
        if key:
            candidates = tuple(search_tv_show_candidates(query_target)[:5])
            resolver = GeminiSeriesResolver(model="gemini-2.5-flash")
            cred = "gemini-primary" if env.gemini_primary_api_key else "gemini-paid"
            res = resolver.resolve_with_key(
                raw_name, candidates, api_key=key, credential=cred
            )
            if res and res.series_name:
                return res.series_name
    except Exception:
        pass

    return query_target


def parse_triage_metadata(  # noqa: C901
    file_path: Path, triage_root: Path, config: Any = None
) -> dict[str, Any]:
    """Extract series, season, disc, episode, and content type from path & siblings."""
    try:
        rel = file_path.relative_to(triage_root)
        parts = rel.parts
    except ValueError:
        parts = file_path.parts
    folder_parts = parts[:-1]

    season_num = None
    season_idx = None
    disc_num = 1
    for i, part in enumerate(folder_parts):
        sm = re.search(r"(?:Season|S)\s*(\d+)", part, re.IGNORECASE)
        if sm:
            season_num = int(sm.group(1))
            season_idx = i
        dm = re.search(r"(?:Disc|D)\s*(\d+)", part, re.IGNORECASE)
        if dm:
            disc_num = int(dm.group(1))

    # Rule 1: Ancestor of Season Folder
    # Rule 2: Skip Wrapper Folders
    series_candidate = None
    if season_idx is not None and season_idx > 0:
        parent_candidate = folder_parts[season_idx - 1]
        if parent_candidate.lower().replace(" ", "_") not in WRAPPER_FOLDERS:
            series_candidate = parent_candidate

    if not series_candidate:
        non_wrappers = [
            pt
            for pt in folder_parts
            if pt.lower().replace(" ", "_") not in WRAPPER_FOLDERS
            and not re.search(r"^(?:Season|S)\s*\d+$", pt, re.IGNORECASE)
            and not re.search(r"^(?:Disc|D)\s*\d+$", pt, re.IGNORECASE)
        ]
        if non_wrappers:
            series_candidate = non_wrappers[-1]

    fname = file_path.name
    fname_stem = file_path.stem
    name_lower = fname.lower()

    # If still no series candidate, inspect the file's own name or sibling files
    if not series_candidate:
        m_ep = re.search(r"^(.+?)(?:[\s._-]*[sS]\d+[\s._-]*[eE]\d+)", fname)
        if m_ep:
            series_candidate = m_ep.group(1).replace(".", " ").replace("_", " ").strip()
        else:
            m_disc = re.match(r"^(.+?)_t\d+$", fname_stem, re.IGNORECASE)
            if m_disc and m_disc.group(1).lower() not in {"title", "disc"}:
                series_candidate = (
                    m_disc.group(1).replace(".", " ").replace("_", " ").strip()
                )
            elif file_path.parent.exists():
                for sib in file_path.parent.iterdir():
                    if sib.is_file() and sib.suffix.lower() == ".mkv":
                        ms = re.match(r"^(.+?)_t\d+$", sib.stem, re.IGNORECASE)
                        if ms and ms.group(1).lower() not in {"title", "disc"}:
                            series_candidate = (
                                ms.group(1).replace(".", " ").replace("_", " ").strip()
                            )
                            break

    if not series_candidate:
        series_candidate = fname_stem

    # Detect extras / bonus material
    is_extra = (
        any(
            k in name_lower
            for k in [
                "extra",
                "extras",
                "trailer",
                "deleted",
                "featurette",
                "behind the scenes",
                "gag reel",
                "bonus",
                "making of",
            ]
        )
        or "Extras" in folder_parts
    )
    # Sibling size cohort heuristic for extras
    file_size = file_path.stat().st_size if file_path.exists() else 0
    if (
        not is_extra
        and file_size > 0
        and file_size < 1_200_000_000
        and file_path.parent.exists()
    ):
        if any(
            s.is_file() and s.stat().st_size > 4_000_000_000
            for s in file_path.parent.iterdir()
            if s.suffix.lower() in {".mkv", ".mp4", ".avi", ".mov"} and s != file_path
        ):
            is_extra = True

    # Detect episode match
    ep_match = None
    m_ep = re.search(r"[sS](\d+)[\s._-]*[eE](\d+)(?:-?[eE]?(\d+))?", fname)
    if m_ep:
        s_int = int(m_ep.group(1))
        e_s = int(m_ep.group(2))
        ep_match = (
            f"S{s_int:02d}E{e_s:02d}-E{int(m_ep.group(3)):02d}"
            if m_ep.group(3)
            else f"S{s_int:02d}E{e_s:02d}"
        )
        if season_num is None:
            season_num = s_int

    # Detect movie vs TV
    is_movie = (season_num is None) and (ep_match is None) and (not is_extra)
    if any(
        k in series_candidate.lower()
        for k in ["three movies", "movie collection", "movies"]
    ):
        is_movie = True

    # Canonical series resolution
    canonical_series = resolve_canonical_series_name(series_candidate)

    # Determine recommended action
    encoded = is_video_encoded(str(file_path.absolute()))
    if "pxl_" in name_lower:
        action = "exclude"
        reason = "Home video (Pixel) - excluded from TV matcher"
    elif is_extra:
        action = "organize" if encoded else "transcode"
        reason = "Detected as Special Feature / Extra."
    elif "Encoded" in folder_parts:
        action = "organize"
        reason = "Located in Encoded directory; ready for Jellyfin"
    elif "ReadyToEncode" in folder_parts:
        action = "transcode"
        reason = "Located in ReadyToEncode directory; bypass Identify"
    elif is_movie:
        action = "organize" if encoded else "identify"
        reason = (
            "Pre-encoded Movie."
            if encoded
            else "Raw Movie, requires audio/TMDb matching."
        )
    elif ep_match is not None:
        action = "organize" if encoded else "transcode"
        reason = f"Identified episode {ep_match} ({'Encoded' if encoded else 'Raw'})."
    else:
        # Rule 3: Raw / unmatched disc title without confirmed SxxExx
        # NEVER assign S01E01 or bypass to organize! Must go to identify!
        action = "identify"
        reason = (
            "Raw or unmatched disc title without episode tag, requires audio matching"
        )

    # Collision Detection (Jellyfin Check)
    if action == "organize" and config:
        try:
            if is_movie:
                lib = getattr(config, "movie_library_folder", None) or getattr(
                    config, "jellyfin_movie_root", None
                )
                if lib and lib.exists() and any(lib.rglob(f"*{file_path.stem}*.mkv")):
                    action = "exclude"
                    reason = "File already exists in Jellyfin Movie Library!"
            elif ep_match:
                lib = getattr(config, "tv_library_folder", None) or getattr(
                    config, "jellyfin_tv_root", None
                )
                if lib and lib.exists() and canonical_series:
                    series_dir = lib / canonical_series
                    if series_dir.exists() and any(
                        series_dir.rglob(f"*{ep_match.lower()}*.mkv")
                    ):
                        action = "exclude"
                        reason = f"Episode {ep_match.upper()} already exists in Jellyfin TV Library!"
        except Exception:
            pass

    return {
        "series_name": canonical_series,
        "season_num": season_num,
        "disc_num": disc_num,
        "ep_match": ep_match,
        "is_extra": is_extra,
        "is_movie": is_movie,
        "encoded": encoded,
        "action": action,
        "reason": reason,
    }


class TriageStatusResponse(BaseModel):
    configured: bool
    folder_path: str | None = None


class TriageItem(BaseModel):
    filename: str
    absolute_path: str
    size_bytes: int
    recommended_action: str  # "identify", "transcode", "organize", "exclude"
    reason: str


class TriageScanResponse(BaseModel):
    items: list[TriageItem]


@router.get("/status", response_model=TriageStatusResponse)
def get_triage_status():
    config = get_config_manager().load()
    folder = config.media_triage_folder
    return TriageStatusResponse(
        configured=folder is not None, folder_path=str(folder) if folder else None
    )


@router.get("/scan", response_model=TriageScanResponse)
def scan_triage_folder(
    store: PipelineQueueStore = Depends(get_pipeline_queue_store),  # noqa: B008
):
    config = get_config_manager().load()
    folder = config.media_triage_folder

    if not folder:
        raise HTTPException(
            status_code=400, detail="Media triage folder is not configured in Settings."
        )

    if not folder.exists() or not folder.is_dir():
        raise HTTPException(
            status_code=404, detail=f"Triage folder {folder} does not exist"
        )

    items = []
    video_exts = {".mkv", ".mp4", ".avi", ".mov"}

    pipeline_items = store.list_items()
    active_media_ids = {
        item.media_id
        for item in pipeline_items
        if item.state not in ("dismissed", "completed")
    }

    for file_path in folder.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in video_exts:
            continue

        meta = parse_triage_metadata(file_path, folder, config)
        action = meta["action"]
        reason = meta["reason"]

        hash_in = f"{str(file_path.absolute())}|{meta['series_name']}|{meta['season_num']}|{action}"
        media_id = f"triage-{hashlib.sha256(hash_in.encode('utf-8')).hexdigest()[:16]}"

        if media_id in active_media_ids:
            action = "in_pipeline"
            reason = "File is currently queued or running in the background pipeline"

        size = file_path.stat().st_size
        items.append(
            TriageItem(
                filename=file_path.name,
                absolute_path=str(file_path.absolute()),
                size_bytes=size,
                recommended_action=action,
                reason=reason,
            )
        )

    return TriageScanResponse(items=items)


class QueueRequest(BaseModel):
    items: list[TriageItem]


class QueueResponse(BaseModel):
    queued_count: int
    failed_count: int
    errors: list[str]


@router.post("/queue", response_model=QueueResponse)
def queue_triage_items(  # noqa: C901
    request: QueueRequest,
    store: PipelineQueueStore = Depends(get_pipeline_queue_store),  # noqa: B008
    contract_root: Path = Depends(get_pipeline_contract_root),  # noqa: B008
):
    queued = 0
    failed = 0
    errors = []

    config = get_config_manager().load()
    triage_folder = config.media_triage_folder

    for item in request.items:
        if (
            item.recommended_action == "exclude"
            or item.recommended_action == "in_pipeline"
        ):
            failed += 1
            errors.append(
                f"Skipped {item.filename}: Cannot queue excluded or already-running items."
            )
            continue

        try:
            file_path = Path(item.absolute_path)
            meta = parse_triage_metadata(file_path, triage_folder, config)
            series_name = meta["series_name"]
            season_num = meta["season_num"]
            disc_num = meta["disc_num"]
            ep_match = meta["ep_match"]
            is_extra = meta["is_extra"]
            is_movie = meta["is_movie"]
            action = item.recommended_action

            # Rule 3 safety check when queuing:
            # If an item has no confirmed episode tag and is not a movie or extra,
            # NEVER assign S01E01 or send to organize! Divert to identify!
            if (
                action == "organize"
                and not is_movie
                and not is_extra
                and ep_match is None
            ):
                action = "identify"

            hash_input = f"{item.absolute_path}|{series_name}|{season_num}|{action}"
            media_id = (
                f"triage-{hashlib.sha256(hash_input.encode('utf-8')).hexdigest()[:16]}"
            )

            if action == "organize":
                height = 1080
                field_order = "progressive"
                try:
                    from mkv_episode_matcher.media.ffprobe_runner import (
                        inspect_mkv,
                        resolve_ffprobe_path,
                    )

                    exe = resolve_ffprobe_path()
                    probe_res = inspect_mkv(exe, file_path)
                    if probe_res.media.video_height:
                        height = probe_res.media.video_height
                    if probe_res.media.video_field_order:
                        field_order = probe_res.media.video_field_order
                except Exception as e:
                    print(f"Failed to probe {item.absolute_path}: {e}")

                payload = {
                    "schema_version": 1,
                    "mode": "verified-transcode-contract",
                    "media_id": media_id,
                    "encoded_path": item.absolute_path,
                    "encoded_size_bytes": item.size_bytes,
                    "encoded_height": height,
                    "encoded_field_order": field_order,
                    "library_relative": (
                        f"{series_name}/Extras/{file_path.name}"
                        if is_extra
                        else (
                            f"{series_name}/{series_name}.mkv"
                            if is_movie
                            else f"{series_name}/Season {season_num or 1:02d}/{file_path.name}"
                        )
                    ),
                    "episode_id": ep_match if not is_movie and not is_extra else None,
                    "library_kind": "movie" if is_movie and not is_extra else "tv",
                    "existing_output_policy": "preserve",
                }
                stage = "organize"

            elif action == "transcode":
                payload = {
                    "schema_version": 1,
                    "mode": "identified-episode-contract",
                    "media_id": media_id,
                    "source_path": item.absolute_path,
                    "source_size_bytes": item.size_bytes,
                    "confidence": 1.0,
                    "episode_id": ep_match if not is_movie and not is_extra else None,
                    "library_kind": "movie" if is_movie and not is_extra else "tv",
                    "library_relative": (
                        f"{series_name}/Extras/{file_path.name}"
                        if is_extra
                        else (
                            f"{series_name}/{series_name}.mkv"
                            if is_movie
                            else f"{series_name}/Season {season_num or 1:02d}/{file_path.name}"
                        )
                    ),
                    "identification_order": ["manual-triage"],
                    "handbrake_profile_id": None,
                    "special_feature_catalog_id": None,
                    "existing_output_policy": "preserve",
                    "provisional_match": False,
                }
                stage = "transcode"

            else:
                disc_id_str = f"triage_{re.sub(r'[^a-zA-Z0-9]', '', series_name)}"
                payload = {
                    "schema_version": 1,
                    "mode": "verified-rip-contract",
                    "media_id": media_id,
                    "source_path": item.absolute_path,
                    "source_size_bytes": item.size_bytes,
                    "duration_seconds": None,
                    "short_title_review_threshold_seconds": 0,
                    "warning_count": 0,
                    "media_context": {
                        "disc_id": disc_id_str,
                        "series_name": series_name,
                        "season": season_num,
                        "disc_number": disc_num,
                        "content_hint": "movie" if is_movie else "tv",
                    },
                }
                stage = "identify"

            serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            contract_root.mkdir(parents=True, exist_ok=True)
            contract_path = (
                contract_root
                / f"{media_id}.verified-{stage if stage != 'organize' else 'transcode'}.json"
            )
            contract_path.write_text(serialized, encoding="utf-8")

            file_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
            with store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                now = store._now()
                row = connection.execute(
                    "SELECT 1 FROM pipeline_items WHERE media_id = ?", (media_id,)
                ).fetchone()
                if not row:
                    connection.execute(
                        """INSERT INTO pipeline_items 
                        (media_id, state, stage, artifact_path, artifact_sha256, artifact_count, created_at, updated_at, rip_artifact_path, rip_artifact_sha256, rip_artifact_count) 
                        VALUES (?, 'queued', ?, ?, ?, 1, ?, ?, ?, ?, 1)""",
                        (
                            media_id,
                            stage,
                            str(contract_path),
                            file_hash,
                            now,
                            now,
                            str(contract_path),
                            file_hash,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE pipeline_items 
                        SET state = 'queued', stage = ?, artifact_path = ?, artifact_sha256 = ?, updated_at = ? 
                        WHERE media_id = ?""",
                        (stage, str(contract_path), file_hash, now, media_id),
                    )
                store._append_event(
                    connection,
                    media_id=media_id,
                    event_type=f"triage_injected_{stage}",
                    stage=stage,
                    state="queued",
                    details={},
                )
                connection.commit()

            queued += 1

        except Exception as e:
            failed += 1
            errors.append(f"Failed to queue {item.filename}: {str(e)}")

    return QueueResponse(queued_count=queued, failed_count=failed, errors=errors)
