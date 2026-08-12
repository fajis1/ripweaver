"""Separate, review-gated repair channel for named Jellyfin TV episodes.

Discovery reads names and file metadata only.  Audio/subtitle verification and
in-place generic renaming are separate, explicit operations.  This module is
deliberately independent of the normal identification pipeline: a claimed
episode is either supported by its own subtitle evidence or left for review.
It never assigns a different episode and never uses title/disc sequence order.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
    collect_dossier_evidence,
)
from mkv_episode_matcher.backend.unmatched_disc_analysis import _score_subtitle
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.core.providers.subtitles import OpenSubtitlesProvider
from mkv_episode_matcher.core.utils import SubtitleReader

SCHEMA_VERSION = 1
_EPISODE_TOKEN = re.compile(r"(?i)(?<![A-Z0-9])S(\d{1,3})E(\d{1,3})(?!\d)")
_MULTI_EPISODE_TOKEN = re.compile(
    r"(?i)(?<![A-Z0-9])S\d{1,3}E\d{1,3}(?:[ ._-]*E\d{1,3})+"
)
_SEASON_FOLDER = re.compile(r"(?i)^Season\s*0*(\d{1,3})$")
_SAFE_JOB_ID = re.compile(r"^[a-f0-9]{24}$")
_SAFE_FILE_ID = re.compile(r"^episode-[a-f0-9]{20}$")
_RUNNABLE_STATES = {"discovered", "running", "completed", "failed", "applied"}


class LibraryEpisodeRepairError(RuntimeError):
    """A path-free error safe to return from the local control API."""


@dataclass(frozen=True)
class EpisodeClaim:
    file_id: str
    source: Path
    relative_path: str
    series_name: str
    season: int
    episode: int
    size_bytes: int
    mtime_ns: int
    generic_name: str

    @property
    def episode_id(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _relative_text(path: Path) -> str:
    return path.as_posix()


def _generic_name(file_id: str) -> str:
    return f"RipWeaver Unmatched - {file_id.removeprefix('episode-')}.mkv"


def _claim_id(relative_path: str, size_bytes: int, mtime_ns: int) -> str:
    digest = hashlib.sha256(
        f"{relative_path.casefold()}\0{size_bytes}\0{mtime_ns}".encode()
    ).hexdigest()[:20]
    return f"episode-{digest}"


def _series_and_episode(root: Path, source: Path) -> tuple[str, int, int] | None:
    try:
        relative = source.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 3:
        return None
    matches = tuple(_EPISODE_TOKEN.finditer(source.stem))
    if len(matches) != 1 or _MULTI_EPISODE_TOKEN.search(source.stem):
        # Multi-episode and ambiguous names require their own reviewed rules.
        return None
    season = int(matches[0].group(1))
    episode = int(matches[0].group(2))
    if season < 0 or episode <= 0:
        return None
    season_folder = next(
        (
            int(match.group(1))
            for part in reversed(parts[:-1])
            if (match := _SEASON_FOLDER.fullmatch(part)) is not None
        ),
        None,
    )
    if season_folder is not None and season_folder != season:
        return None
    # A Jellyfin TV root is expected to contain Series/Season/file.  Keep the
    # full series-directory name because that is the user's canonical context.
    series_name = " ".join(parts[0].replace("_", " ").split())
    if not series_name:
        return None
    return series_name, season, episode


def discover_episode_claims(
    root: Path,
    *,
    episode_keys: frozenset[tuple[str, int, int]] | None = None,
    maximum_files: int = 999,
) -> tuple[EpisodeClaim, ...]:
    """Inventory canonical episode filenames without opening their media."""

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise LibraryEpisodeRepairError("The Jellyfin TV root is unavailable")
    claims: list[EpisodeClaim] = []
    try:
        candidates = sorted(
            root.rglob("*.mkv"), key=lambda value: str(value).casefold()
        )
    except OSError as exc:
        raise LibraryEpisodeRepairError(
            "The Jellyfin TV root could not be inventoried"
        ) from exc
    for source in candidates:
        context = _series_and_episode(root, source)
        if context is None or not source.is_file():
            continue
        try:
            stat = source.stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        relative = _relative_text(source.relative_to(root))
        file_id = _claim_id(relative, stat.st_size, stat.st_mtime_ns)
        series_name, season, episode = context
        if (
            episode_keys is not None
            and (series_name.casefold(), season, episode) not in episode_keys
        ):
            continue
        claims.append(
            EpisodeClaim(
                file_id=file_id,
                source=source,
                relative_path=relative,
                series_name=series_name,
                season=season,
                episode=episode,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                generic_name=_generic_name(file_id),
            )
        )
        if len(claims) > maximum_files:
            raise LibraryEpisodeRepairError(
                "The repair inventory is too large; select a smaller Jellyfin folder"
            )
    return tuple(claims)


def sequence_derived_episode_keys(
    pipeline_items: tuple[object, ...], dossier_root: Path
) -> frozenset[tuple[str, int, int]]:
    """Return Jellyfin episode claims that have a matched tv-local attempt."""

    dossier = IdentificationDossierStore(dossier_root)
    episode_keys: set[tuple[str, int, int]] = set()
    for item in pipeline_items:
        media_id = getattr(item, "media_id", None)
        artifact = getattr(item, "artifact", None)
        contract_path = getattr(artifact, "contract_path", None)
        if not isinstance(media_id, str) or not isinstance(contract_path, Path):
            continue
        try:
            attempts = dossier.safe_attempts(media_id)
        except Exception:
            continue
        if not any(
            attempt.get("branch") == "tv-local"
            and attempt.get("disposition") == "matched"
            for attempt in attempts
        ):
            continue
        try:
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        relative = payload.get("library_relative")
        if not isinstance(relative, str):
            continue
        path = Path(relative.replace("/", os.sep))
        matches = tuple(_EPISODE_TOKEN.finditer(path.stem))
        if len(path.parts) < 3 or len(matches) != 1:
            continue
        series_name = " ".join(path.parts[0].replace("_", " ").split())
        episode_keys.add((
            series_name.casefold(),
            int(matches[0].group(1)),
            int(matches[0].group(2)),
        ))
    return frozenset(episode_keys)


def _candidate_digest(candidates: list[dict]) -> str:
    public_identity = [
        {
            "file_id": item["file_id"],
            "relative_path": item["relative_path"],
            "size_bytes": item["size_bytes"],
            "mtime_ns": item["mtime_ns"],
            "episode_id": item["episode_id"],
            "generic_name": item["generic_name"],
        }
        for item in candidates
    ]
    return hashlib.sha256(
        json.dumps(public_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _result_digest(candidates: list[dict]) -> str:
    outcomes = [
        {
            "file_id": item["file_id"],
            "status": item.get("status"),
            "generic_name": item["generic_name"],
            "size_bytes": item["size_bytes"],
            "mtime_ns": item["mtime_ns"],
        }
        for item in candidates
    ]
    return hashlib.sha256(
        json.dumps(outcomes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class LibraryEpisodeRepairStore:
    """Private restart-safe audit plans; absolute paths never leave the store."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        self._reconcile_interrupted()

    def _reconcile_interrupted(self) -> None:
        """Surface an interrupted worker after restart instead of feigning progress."""

        if not self.root.is_dir():
            return
        with self._lock:
            for path in self.root.glob("*.private.json"):
                job_id = path.name.removesuffix(".private.json")
                if _SAFE_JOB_ID.fullmatch(job_id) is None:
                    continue
                try:
                    payload = self._read(job_id)
                except LibraryEpisodeRepairError:
                    continue
                if payload["status"] != "running":
                    continue
                payload["status"] = "failed"
                payload["error_code"] = "audit_interrupted"
                payload["updated_at"] = _now()
                self._write(payload)

    def _path(self, job_id: str) -> Path:
        if _SAFE_JOB_ID.fullmatch(job_id) is None:
            raise LibraryEpisodeRepairError("The repair job ID is invalid")
        return self.root / f"{job_id}.private.json"

    def _read(self, job_id: str) -> dict:
        path = self._path(job_id)
        if not path.is_file():
            raise LibraryEpisodeRepairError("The repair job was not found")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LibraryEpisodeRepairError("The repair job is unreadable") from exc
        if (
            payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("status") not in _RUNNABLE_STATES
            or not isinstance(payload.get("candidates"), list)
        ):
            raise LibraryEpisodeRepairError("The repair job is invalid")
        return payload

    def _write(self, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(payload["job_id"])
        temporary = self.root / f".{target.name}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def create(
        self, root: Path, claims: tuple[EpisodeClaim, ...], *, scope: str
    ) -> dict:
        job_id = uuid4().hex[:24]
        candidates = [
            {
                "file_id": claim.file_id,
                "source_path": str(claim.source),
                "relative_path": claim.relative_path,
                "series_name": claim.series_name,
                "season": claim.season,
                "episode": claim.episode,
                "episode_id": claim.episode_id,
                "size_bytes": claim.size_bytes,
                "mtime_ns": claim.mtime_ns,
                "generic_name": claim.generic_name,
                "status": "pending",
                "score": None,
                "qualifying_window_count": 0,
                "evidence_window_count": 0,
                "reason": "not_run",
                "renamed": False,
            }
            for claim in claims
        ]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": "library-episode-repair",
            "scope": scope,
            "job_id": job_id,
            "library_root": str(root.resolve()),
            "status": "discovered",
            "candidate_digest": _candidate_digest(candidates),
            "result_digest": None,
            "progress": {"current": 0, "total": len(candidates), "file_id": None},
            "candidates": candidates,
            "created_at": _now(),
            "updated_at": _now(),
            "error_code": None,
        }
        with self._lock:
            self._write(payload)
        return self.public(job_id)

    def private(self, job_id: str) -> dict:
        with self._lock:
            return self._read(job_id)

    def start(self, job_id: str, candidate_digest: str) -> dict:
        with self._lock:
            payload = self._read(job_id)
            if payload["status"] != "discovered":
                raise LibraryEpisodeRepairError(
                    "The repair job cannot be started again"
                )
            if payload["candidate_digest"] != candidate_digest:
                raise LibraryEpisodeRepairError("The repair inventory changed")
            payload["status"] = "running"
            payload["updated_at"] = _now()
            self._write(payload)
        return self.public(job_id)

    def record_result(self, job_id: str, file_id: str, result: dict) -> None:
        with self._lock:
            payload = self._read(job_id)
            if payload["status"] != "running":
                raise LibraryEpisodeRepairError("The repair job is not running")
            candidate = next(
                (item for item in payload["candidates"] if item["file_id"] == file_id),
                None,
            )
            if candidate is None:
                raise LibraryEpisodeRepairError("The repair candidate is invalid")
            candidate.update(result)
            completed = sum(
                item["status"] != "pending" for item in payload["candidates"]
            )
            payload["progress"] = {
                "current": completed,
                "total": len(payload["candidates"]),
                "file_id": file_id,
            }
            payload["updated_at"] = _now()
            self._write(payload)

    def finish(self, job_id: str, *, error_code: str | None = None) -> dict:
        with self._lock:
            payload = self._read(job_id)
            if payload["status"] != "running":
                raise LibraryEpisodeRepairError("The repair job is not running")
            if error_code is None:
                payload["status"] = "completed"
                payload["result_digest"] = _result_digest(payload["candidates"])
            else:
                payload["status"] = "failed"
                payload["error_code"] = error_code
            payload["updated_at"] = _now()
            self._write(payload)
        return self.public(job_id)

    def public(self, job_id: str) -> dict:
        with self._lock:
            payload = self._read(job_id)
        candidates = []
        for item in payload["candidates"]:
            candidates.append({
                key: item.get(key)
                for key in (
                    "file_id",
                    "relative_path",
                    "series_name",
                    "season",
                    "episode",
                    "episode_id",
                    "size_bytes",
                    "generic_name",
                    "status",
                    "score",
                    "qualifying_window_count",
                    "evidence_window_count",
                    "reason",
                    "renamed",
                )
            })
        current_id = payload["progress"].get("file_id")
        current = next(
            (
                item["relative_path"]
                for item in candidates
                if item["file_id"] == current_id
            ),
            None,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": payload["mode"],
            "scope": payload.get("scope", "all-named"),
            "job_id": job_id,
            "status": payload["status"],
            "candidate_digest": payload["candidate_digest"],
            "result_digest": payload.get("result_digest"),
            "candidate_count": len(candidates),
            "progress": {
                "current": payload["progress"]["current"],
                "total": payload["progress"]["total"],
                "relative_path": current,
            },
            "candidates": candidates,
            "error_code": payload.get("error_code"),
        }

    def mark_applied(self, job_id: str, file_ids: tuple[str, ...]) -> dict:
        with self._lock:
            payload = self._read(job_id)
            for item in payload["candidates"]:
                if item["file_id"] in file_ids:
                    item["renamed"] = True
            payload["status"] = "applied"
            payload["updated_at"] = _now()
            self._write(payload)
        return self.public(job_id)


def _source_identity_payload(candidate: dict) -> dict:
    relative = candidate["relative_path"]
    fingerprint = hashlib.sha256(relative.casefold().encode()).hexdigest()[:16]
    return {
        "disc_fingerprint": fingerprint,
        "title_index": 0,
        "source_size_bytes": candidate["size_bytes"],
        "source_path": candidate["source_path"],
    }


def _verification_result(
    *,
    scores: tuple[float, ...],
    min_confidence: float,
    reference_found: bool,
) -> dict:
    qualifying = tuple(score for score in scores if score >= min_confidence)
    best = max(scores, default=0.0)
    if not reference_found:
        status, reason = "inconclusive", "claimed_subtitle_unavailable"
    elif len(qualifying) >= 2 or best >= 0.92:
        status, reason = "confirmed", "independent_subtitle_evidence"
    elif len(scores) >= 2 and not qualifying:
        status, reason = "mismatch", "claimed_episode_not_supported"
    else:
        status, reason = "inconclusive", "insufficient_independent_evidence"
    return {
        "status": status,
        "score": round(best, 6) if scores else None,
        "qualifying_window_count": len(qualifying),
        "evidence_window_count": len(scores),
        "reason": reason,
    }


def execute_library_episode_audit(  # noqa: C901 - guarded evidence/provider workflow
    store: LibraryEpisodeRepairStore,
    job_id: str,
    config: Config,
    asr,
    contract_root: Path,
    *,
    provider_factory=OpenSubtitlesProvider,
    evidence_collector=collect_dossier_evidence,
) -> dict:
    """Read the exact discovered MKVs and verify each claimed episode only."""

    payload = store.private(job_id)
    if payload["status"] != "running":
        raise LibraryEpisodeRepairError("The repair job is not running")
    candidates = payload["candidates"]
    if not candidates:
        return store.finish(job_id)

    try:
        try:
            provider = provider_factory()
        except Exception:
            provider = None
        references: dict[tuple[str, int], tuple] = {}
        for candidate in candidates:
            source = Path(candidate["source_path"])
            try:
                stat = source.stat()
                if (
                    not source.is_file()
                    or stat.st_size != candidate["size_bytes"]
                    or stat.st_mtime_ns != candidate["mtime_ns"]
                ):
                    raise LibraryEpisodeRepairError("source_changed")
                evidence_items = (
                    (
                        SimpleNamespace(media_id=candidate["file_id"]),
                        _source_identity_payload(candidate),
                    ),
                )
                evidence_batch, _dossier = evidence_collector(
                    evidence_items, config, asr, contract_root
                )
                evidence = next(
                    (
                        item
                        for item in evidence_batch
                        if item.file_id == candidate["file_id"]
                    ),
                    None,
                )
                key = (candidate["series_name"], candidate["season"])
                if key not in references and provider is not None:
                    references[key] = tuple(
                        provider.get_subtitles(key[0], key[1], [], None)
                    )
                claimed = tuple(
                    subtitle
                    for subtitle in references.get(key, ())
                    if subtitle.episode_info is not None
                    and subtitle.episode_info.season == candidate["season"]
                    and subtitle.episode_info.episode == candidate["episode"]
                )
                if evidence is None or not evidence.transcript_excerpts:
                    result = _verification_result(
                        scores=(),
                        min_confidence=config.min_confidence,
                        reference_found=bool(claimed),
                    )
                    result["reason"] = "audio_evidence_unavailable"
                else:
                    ranked_scores = []
                    for subtitle in claimed:
                        try:
                            content = subtitle.content or SubtitleReader.read_srt_file(
                                subtitle.path
                            )
                        except (OSError, ValueError):
                            continue
                        _average, scores = _score_subtitle(
                            asr,
                            evidence.transcript_excerpts,
                            content,
                            evidence.duration_seconds,
                        )
                        ranked_scores.append(scores)
                    best_scores = max(
                        ranked_scores,
                        key=lambda values: (
                            len(
                                tuple(
                                    score
                                    for score in values
                                    if score >= config.min_confidence
                                )
                            ),
                            max(values, default=0.0),
                        ),
                        default=(),
                    )
                    result = _verification_result(
                        scores=best_scores,
                        min_confidence=config.min_confidence,
                        reference_found=bool(ranked_scores),
                    )
            except LibraryEpisodeRepairError:
                result = {
                    "status": "inconclusive",
                    "score": None,
                    "qualifying_window_count": 0,
                    "evidence_window_count": 0,
                    "reason": "source_changed",
                }
            except Exception as exc:
                result = {
                    "status": "inconclusive",
                    "score": None,
                    "qualifying_window_count": 0,
                    "evidence_window_count": 0,
                    "reason": f"verification_failed:{type(exc).__name__}",
                }
            store.record_result(job_id, candidate["file_id"], result)
        return store.finish(job_id)
    except Exception as exc:
        return store.finish(job_id, error_code=f"audit_failed:{type(exc).__name__}")


def _validate_apply_selection(
    payload: dict, result_digest: str, file_ids: tuple[str, ...]
) -> tuple[dict, ...]:
    if payload["status"] != "completed":
        raise LibraryEpisodeRepairError("The repair audit is not ready to apply")
    if payload.get("result_digest") != result_digest:
        raise LibraryEpisodeRepairError("The reviewed repair result changed")
    if not file_ids or len(set(file_ids)) != len(file_ids):
        raise LibraryEpisodeRepairError("Select at least one unique repair item")
    if any(_SAFE_FILE_ID.fullmatch(file_id) is None for file_id in file_ids):
        raise LibraryEpisodeRepairError("The repair selection is invalid")
    selected = tuple(
        item for item in payload["candidates"] if item["file_id"] in file_ids
    )
    if len(selected) != len(file_ids):
        raise LibraryEpisodeRepairError("The repair selection changed")
    if any(item["status"] not in {"mismatch", "inconclusive"} for item in selected):
        raise LibraryEpisodeRepairError("Confirmed episodes cannot be made generic")
    return selected


def apply_generic_repairs(
    store: LibraryEpisodeRepairStore,
    job_id: str,
    *,
    result_digest: str,
    file_ids: tuple[str, ...],
) -> dict:
    """Rename only the exact reviewed non-confirmed set, refusing collisions."""

    payload = store.private(job_id)
    selected = _validate_apply_selection(payload, result_digest, file_ids)
    planned: list[tuple[Path, Path]] = []
    for item in selected:
        source = Path(item["source_path"])
        target = source.with_name(item["generic_name"])
        try:
            stat = source.stat()
        except OSError as exc:
            raise LibraryEpisodeRepairError("A reviewed source is unavailable") from exc
        if (
            not source.is_file()
            or stat.st_size != item["size_bytes"]
            or stat.st_mtime_ns != item["mtime_ns"]
        ):
            raise LibraryEpisodeRepairError("A reviewed source changed")
        if target.exists() or target.parent != source.parent:
            raise LibraryEpisodeRepairError("A generic destination is unavailable")
        if _EPISODE_TOKEN.search(target.stem):
            raise LibraryEpisodeRepairError("A generic destination is invalid")
        planned.append((source, target))

    applied: list[str] = []
    for (source, target), item in zip(planned, selected, strict=True):
        try:
            if os.name == "nt":
                # Windows rename refuses an existing destination, including on
                # mapped/network drives where hard links may be unavailable.
                os.rename(source, target)
            else:
                # Creating a new hard-link name is atomic and refuses an
                # existing destination. Removing the old name then completes
                # an in-directory rename without risking overwrite.
                os.link(source, target)
                try:
                    source.unlink()
                except OSError:
                    target.unlink(missing_ok=True)
                    raise
        except OSError as exc:
            raise LibraryEpisodeRepairError(
                "A generic rename could not be completed; completed names were preserved"
            ) from exc
        applied.append(item["file_id"])
        store.mark_applied(job_id, tuple(applied))
    return store.public(job_id)
