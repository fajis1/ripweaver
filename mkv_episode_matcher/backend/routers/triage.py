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


import hashlib
import re
from pathlib import Path

from pydantic import BaseModel

from mkv_episode_matcher.backend.dependencies import (
    get_pipeline_contract_root,
    get_pipeline_queue_store,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore

router = APIRouter(prefix="/triage", tags=["triage"])


class TriageStatusResponse(BaseModel):
    configured: bool
    folder_path: str | None = None


class TriageItem(BaseModel):
    filename: str
    absolute_path: str
    size_bytes: int
    recommended_action: str  # "identify", "transcode", "organize"
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
def scan_triage_folder(store: PipelineQueueStore = Depends(get_pipeline_queue_store)):
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

        rel_path = file_path.relative_to(folder).as_posix()
        action = "identify"
        reason = "Unrecognized pattern or location, requires audio matching"

        # Parse context to match queueing logic
        rel_parts = file_path.relative_to(folder).parts
        s_name = None
        s_num = None
        if len(rel_parts) > 1:
            s_name = rel_parts[0]
            for p in rel_parts[1:-1]:
                sm = re.search(r"(?:Season|S)\s*(\d+)", p, re.IGNORECASE)
                if sm:
                    s_num = int(sm.group(1))
        if not s_name:
            fm = re.match(r"^(.+?)(?:[\s._-]*[sS]\d+[\s._-]*[eE]\d+)", rel_parts[-1])
            if fm:
                s_name = fm.group(1).replace(".", " ").replace("_", " ").strip()

        encoded = is_video_encoded(str(file_path.absolute()))
        name_lower = file_path.name.lower()
        is_extra = (
            any(
                k in name_lower
                for k in [
                    "extra",
                    "trailer",
                    "deleted",
                    "featurette",
                    "behind the scenes",
                ]
            )
            or "Extras" in file_path.parts
        )
        has_multi_ep = re.search(r"s\d{2}e\d{2}-e\d{2}", name_lower) is not None
        has_ep = re.search(r"s\d{2}e\d{2}", name_lower) is not None

        # 2. Heuristics based on your folder structure and ffprobe!
        if "pxl_" in name_lower:
            action = "exclude"
            reason = "Home video (Pixel) - excluded from TV matcher"
        elif is_extra:
            action = "organize" if encoded else "transcode"
            reason = "Detected as Special Feature/Extra. Bypassing Identify."
        elif "Encoded" in file_path.parts:
            action = "organize"
            reason = "Located in Encoded directory; ready for Jellyfin"
        elif "ReadyToEncode" in file_path.parts:
            action = "transcode"
            reason = "Located in ReadyToEncode directory; bypass Identify"
        else:
            if has_multi_ep:
                action = "organize" if encoded else "transcode"
                reason = f"Multi-episode detected + {'Encoded' if encoded else 'Raw'}."
            elif has_ep:
                action = "organize" if encoded else "transcode"
                reason = f"Standard episode format + {'Encoded' if encoded else 'Raw'}."
            else:
                action = "organize" if encoded else "identify"
                reason = (
                    f"{'Pre-encoded Movie' if encoded else 'Raw MakeMKV Movie/Show'}."
                )

        # 3. Collision Detection (Jellyfin Check)
        if action == "organize":
            is_movie = not (has_ep or has_multi_ep) and not is_extra
            try:
                if is_movie:
                    lib = config.movie_library_folder
                    if lib and lib.exists():
                        if any(lib.rglob(f"*{file_path.stem}*.mkv")):
                            action = "exclude"
                            reason = "File already exists in Jellyfin Movie Library!"
                else:
                    lib = config.tv_library_folder
                    if lib and lib.exists() and s_name:
                        series_dir = lib / s_name
                        if series_dir.exists():
                            ep_match = re.search(
                                r"(s\d{2}e\d{2}(?:-e\d{2})?)", name_lower
                            )
                            if ep_match:
                                ep_str = ep_match.group(1)
                                if any(series_dir.rglob(f"*{ep_str}*.mkv")):
                                    action = "exclude"
                                    reason = f"Episode {ep_str.upper()} already exists in Jellyfin TV Library!"
            except Exception:
                pass

        # NOW compute media_id with the REAL action included
        hash_in = f"{str(file_path.absolute())}|{s_name}|{s_num}|{action}"
        media_id = f"triage-{hashlib.sha256(hash_in.encode('utf-8')).hexdigest()[:16]}"

        if media_id in active_media_ids:
            action = "in_pipeline"
            reason = "File is currently queued or running in the background pipeline"

            name_lower = file_path.name.lower()
            is_extra = (
                any(
                    k in name_lower
                    for k in [
                        "extra",
                        "trailer",
                        "deleted",
                        "featurette",
                        "behind the scenes",
                    ]
                )
                or "Extras" in file_path.parts
            )
            has_multi_ep = re.search(r"s\d{2}e\d{2}-e\d{2}", name_lower) is not None
            has_ep = re.search(r"s\d{2}e\d{2}", name_lower) is not None

            # 2. Heuristics based on your folder structure and ffprobe!
            if "pxl_" in name_lower:
                action = "exclude"
                reason = "Home video (Pixel) - excluded from TV matcher"
            elif is_extra:
                action = "organize" if encoded else "transcode"
                reason = "Detected as Special Feature/Extra. Bypassing Identify."
            elif "Encoded" in file_path.parts:
                action = "organize"
                reason = "Located in Encoded directory; ready for Jellyfin"
            elif "ReadyToEncode" in file_path.parts:
                action = "transcode"
                reason = "Located in ReadyToEncode directory; bypass Identify"
            else:
                if has_multi_ep:
                    action = "organize" if encoded else "transcode"
                    reason = (
                        f"Multi-episode detected + {'Encoded' if encoded else 'Raw'}."
                    )
                elif has_ep:
                    action = "organize" if encoded else "transcode"
                    reason = (
                        f"Standard episode format + {'Encoded' if encoded else 'Raw'}."
                    )
                else:
                    action = "organize" if encoded else "identify"
                    reason = f"{'Pre-encoded Movie' if encoded else 'Raw MakeMKV Movie/Show'}."

            # 3. Collision Detection (Jellyfin Check)
            if action == "organize":
                # Predict destination to see if it exists
                # This is a basic predictive check, exact destination depends on TMDB validation during Queue step.
                is_movie = not (has_ep or has_multi_ep) and not is_extra
                try:
                    if is_movie:
                        lib = config.movie_library_folder
                        if lib and lib.exists():
                            # If any file exists with this stem
                            if any(lib.rglob(f"*{file_path.stem}*.mkv")):
                                action = "exclude"
                                reason = (
                                    "File already exists in Jellyfin Movie Library!"
                                )
                    else:
                        lib = config.tv_library_folder
                        if lib and lib.exists() and s_name:
                            # Search inside the series folder
                            series_dir = lib / s_name
                            if series_dir.exists():
                                ep_match = re.search(
                                    r"(s\d{2}e\d{2}(?:-e\d{2})?)", name_lower
                                )
                                if ep_match:
                                    ep_str = ep_match.group(1)
                                    if any(series_dir.rglob(f"*{ep_str}*.mkv")):
                                        action = "exclude"
                                        reason = f"Episode {ep_str.upper()} already exists in Jellyfin TV Library!"
                except Exception:
                    pass

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
def queue_triage_items(
    request: QueueRequest,
    store: PipelineQueueStore = Depends(get_pipeline_queue_store),
    contract_root: Path = Depends(get_pipeline_contract_root),
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
            try:
                rel = Path(item.absolute_path).relative_to(triage_folder)
                parts = rel.parts
            except ValueError:
                parts = [Path(item.absolute_path).name]

            series_name = None
            season_num = None
            disc_num = 1

            is_movie = False
            is_extra = False

            if len(parts) > 1:
                series_name = parts[0]
                for p in parts[1:-1]:
                    s_match = re.search(r"(?:Season|S)\s*(\d+)", p, re.IGNORECASE)
                    if s_match:
                        season_num = int(s_match.group(1))
                    d_match = re.search(r"(?:Disc|D)\s*(\d+)", p, re.IGNORECASE)
                    if d_match:
                        disc_num = int(d_match.group(1))

            if not series_name:
                fname = parts[-1]
                ep_match = None
                m = re.match(
                    r"^(.+?)(?:[\s._-]*[sS](\d+)[\s._-]*[eE](\d+)(?:-[eE]?(\d+))?)",
                    fname,
                )
                is_extra = (
                    any(
                        k in fname.lower()
                        for k in [
                            "extra",
                            "trailer",
                            "deleted",
                            "featurette",
                            "behind the scenes",
                        ]
                    )
                    or "Extras" in parts
                )
                if m and not is_extra:
                    if m.group(4):
                        ep_match = f"S{int(m.group(2)):02d}E{int(m.group(3)):02d}-E{int(m.group(4)):02d}"
                    else:
                        ep_match = f"S{int(m.group(2)):02d}E{int(m.group(3)):02d}"
                    if season_num is None:
                        season_num = int(m.group(2))
                if m:
                    series_name = m.group(1).replace(".", " ").replace("_", " ").strip()
                else:
                    series_name = Path(fname).stem
                    is_movie = True

            if series_name == "Encoded" or series_name == "ReadyToEncode":
                if len(parts) > 2:
                    series_name = parts[1]
                else:
                    series_name = Path(parts[-1]).stem
                    is_movie = True

            # --- TMDB TV LOOKUP INJECTION ---
            if (
                not is_movie
                and not is_extra
                and series_name
                and series_name not in ["Encoded", "ReadyToEncode"]
            ):
                try:
                    from mkv_episode_matcher.tmdb_client import _tmdb_get_json

                    results = _tmdb_get_json("/search/tv", query=series_name).get(
                        "results", []
                    )
                    if results:
                        best = results[0]
                        series_name = best["name"]  # Canonical Name
                except Exception as e:
                    print(f"TMDB TV search failed: {e}")
            # -----------------------------------

            # --- TMDB MOVIE LOOKUP INJECTION ---
            if is_movie:
                try:
                    from mkv_episode_matcher.tmdb_client import _tmdb_get_json

                    results = _tmdb_get_json("/search/movie", query=series_name).get(
                        "results", []
                    )
                    if results:
                        best = results[0]
                        year = best.get("release_date", "").split("-")[0]
                        if year:
                            series_name = f"{best['title']} ({year})"
                        else:
                            series_name = best["title"]
                except Exception as e:
                    print(f"TMDB movie search failed: {e}")
            # -----------------------------------

            hash_input = f"{item.absolute_path}|{series_name}|{season_num}|{item.recommended_action}"
            media_id = (
                f"triage-{hashlib.sha256(hash_input.encode('utf-8')).hexdigest()[:16]}"
            )

            action = item.recommended_action

            if action == "organize":
                height = 1080
                field_order = "progressive"
                try:
                    from mkv_episode_matcher.media.ffprobe_runner import (
                        inspect_mkv,
                        resolve_ffprobe_path,
                    )

                    exe = resolve_ffprobe_path()
                    probe_res = inspect_mkv(exe, Path(item.absolute_path))
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
                    "library_relative": f"{series_name}/Extras/{Path(item.absolute_path).name}"
                    if is_extra
                    else (
                        f"{series_name}/{series_name}.mkv"
                        if is_movie
                        else f"{series_name}/Season {season_num or 1:02d}/{Path(item.absolute_path).name}"
                    ),
                    "episode_id": None
                    if is_movie
                    else (
                        ep_match
                        if "ep_match" in locals() and ep_match
                        else f"S{season_num or 1:02d}E01"
                    ),
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
                    "episode_id": None
                    if is_movie
                    else (
                        ep_match
                        if "ep_match" in locals() and ep_match
                        else f"S{season_num or 1:02d}E01"
                    ),
                    "library_kind": "movie" if is_movie and not is_extra else "tv",
                    "library_relative": f"{series_name}/Extras/{Path(item.absolute_path).name}"
                    if is_extra
                    else (
                        f"{series_name}/{series_name}.mkv"
                        if is_movie
                        else f"{series_name}/Season {season_num or 1:02d}/{Path(item.absolute_path).name}"
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
