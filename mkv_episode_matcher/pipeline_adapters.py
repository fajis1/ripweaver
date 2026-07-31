"""Explicit production adapters for the serialized downstream pipeline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from mkv_episode_matcher.media.handbrake import (
    HandBrakeJob,
    HandBrakeProfile,
    execute_handbrake_job,
    partial_output_path,
)
from mkv_episode_matcher.media.organizer import build_episode_filename
from mkv_episode_matcher.pipeline_queue import (
    PipelineArtifact,
    PipelineQueueError,
    PipelineReviewRequiredError,
    QueuedPipelineItem,
    build_artifact,
)


class MatchEngine(Protocol):
    def process_path(self, path: Path, **kwargs): ...


def _load_contract(item: QueuedPipelineItem, expected_mode: str) -> dict[str, Any]:
    try:
        payload: Any = json.loads(
            item.artifact.contract_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineQueueError("Pipeline input contract is invalid") from exc
    if not isinstance(payload, dict) or payload.get("mode") != expected_mode:
        raise PipelineQueueError("Pipeline input contract mode is invalid")
    return payload


def _source_from_contract(
    payload: dict[str, Any], path_field: str, size_field: str
) -> Path:
    try:
        source = Path(str(payload[path_field])).resolve()
        size = int(payload[size_field])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineQueueError("Pipeline source contract fields are invalid") from exc
    if not source.is_file() or size <= 0 or source.stat().st_size != size:
        raise PipelineQueueError("Pipeline source is missing or changed")
    return source


def _write_contract(
    root: Path, media_id: str, stage: str, payload: dict[str, Any]
) -> PipelineArtifact:
    if not root.is_dir():
        raise PipelineQueueError("Pipeline contract root is unavailable")
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path = root / f"{media_id}.{stage}.json"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") != serialized:
                raise PipelineQueueError("Pipeline output contract collision")
        except OSError as exc:
            raise PipelineQueueError(
                "Pipeline output contract could not be read"
            ) from exc
    else:
        try:
            path.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            raise PipelineQueueError(
                "Pipeline output contract could not be written"
            ) from exc
    return build_artifact(stage, path)


class IdentifyStageAdapter:
    """Dry-run episode identification that never renames its verified rip."""

    def __init__(self, engine: MatchEngine, contract_root: Path):
        self.engine = engine
        self.contract_root = contract_root.resolve()

    def __call__(self, item: QueuedPipelineItem) -> PipelineArtifact:
        payload = _load_contract(item, "verified-rip-contract")
        source = _source_from_contract(payload, "source_path", "source_size_bytes")
        context = payload.get("media_context")
        if not isinstance(context, dict) or not context.get("series_name"):
            raise PipelineReviewRequiredError("missing_series_context")
        season = context.get("season")
        if not isinstance(season, int) or isinstance(season, bool):
            raise PipelineReviewRequiredError("missing_season_context")
        matches, failures = self.engine.process_path(
            path=source.parent,
            season_override=season,
            recursive=False,
            dry_run=True,
            json_output=True,
            tmdb_id=context.get("tmdb_id"),
            show_name_override=str(context["series_name"]),
            files_override=[source],
        )
        if len(matches) != 1 or failures:
            raise PipelineReviewRequiredError("episode_match_review")
        match = matches[0]
        episode = match.episode_info
        episode_id = f"S{episode.season:02d}E{episode.episode:02d}"
        title = episode.title or "Untitled"
        filename = build_episode_filename(episode.series_name, episode_id, title)
        relative = Path(episode.series_name) / f"Season {episode.season:02d}" / filename
        return _write_contract(
            self.contract_root,
            item.media_id,
            "identify",
            {
                "schema_version": 1,
                "mode": "identified-episode-contract",
                "media_id": item.media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "confidence": float(match.confidence),
                "episode_id": episode_id,
                "library_relative": relative.as_posix(),
            },
        )


class TranscodeStageAdapter:
    """Run one verified HandBrake encode; dispatcher serialization bounds resources."""

    def __init__(
        self,
        *,
        handbrake: Path,
        ffprobe: Path,
        output_root: Path,
        run_root: Path,
        contract_root: Path,
        profile: HandBrakeProfile,
        timeout_seconds: int = 21600,
    ):
        self.handbrake = handbrake.resolve()
        self.ffprobe = ffprobe.resolve()
        self.output_root = output_root.resolve()
        self.run_root = run_root.resolve()
        self.contract_root = contract_root.resolve()
        self.profile = profile
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: QueuedPipelineItem) -> PipelineArtifact:
        payload = _load_contract(item, "identified-episode-contract")
        source = _source_from_contract(payload, "source_path", "source_size_bytes")
        relative = PurePosixPath(str(payload.get("library_relative", "")))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.lower() != ".mkv"
        ):
            raise PipelineQueueError("Transcode destination contract is unsafe")
        if not self.output_root.is_dir() or not self.run_root.is_dir():
            raise PipelineQueueError("Transcode staging or run root is unavailable")
        destination = (
            self.output_root / "encoded-staging" / Path(*relative.parts)
        ).resolve()
        try:
            destination.relative_to(self.output_root)
        except ValueError as exc:
            raise PipelineQueueError("Transcode destination escapes staging") from exc
        if destination.exists():
            raise PipelineQueueError("Transcode destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)

        attempt = 1
        job = HandBrakeJob(
            item.media_id, source, destination, self.profile, attempt_number=attempt
        )
        while partial_output_path(job).exists():
            attempt += 1
            job = HandBrakeJob(
                item.media_id, source, destination, self.profile, attempt_number=attempt
            )
        run_dir = self.run_root / f"{item.media_id}-attempt-{attempt:03d}"
        if run_dir.exists():
            raise PipelineQueueError("Transcode run directory collision")
        run_dir.mkdir()
        result = execute_handbrake_job(
            self.handbrake,
            self.ffprobe,
            job,
            run_dir,
            confirm_transcode=True,
            timeout_seconds=self.timeout_seconds,
        )
        if (
            not destination.is_file()
            or destination.stat().st_size != result.output_bytes
        ):
            raise PipelineQueueError("Verified transcode output is missing or changed")
        return _write_contract(
            self.contract_root,
            item.media_id,
            "transcode",
            {
                "schema_version": 1,
                "mode": "verified-transcode-contract",
                "media_id": item.media_id,
                "encoded_path": str(destination),
                "encoded_size_bytes": result.output_bytes,
                "library_relative": relative.as_posix(),
            },
        )


class OrganizeStageAdapter:
    """Apply one collision-refusing, explicitly authorized library placement."""

    def __init__(
        self,
        *,
        library_root: Path,
        contract_root: Path,
        confirm_organize: bool,
        copy_only: bool = False,
    ):
        if not confirm_organize:
            raise PipelineQueueError(
                "Organization adapter requires explicit authorization"
            )
        self.library_root = library_root.resolve()
        self.contract_root = contract_root.resolve()
        self.copy_only = copy_only

    def __call__(self, item: QueuedPipelineItem) -> PipelineArtifact:
        payload = _load_contract(item, "verified-transcode-contract")
        source = _source_from_contract(payload, "encoded_path", "encoded_size_bytes")
        relative = PurePosixPath(str(payload.get("library_relative", "")))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.lower() != ".mkv"
        ):
            raise PipelineQueueError("Library destination contract is unsafe")
        if not self.library_root.is_dir():
            raise PipelineQueueError("Library root is unavailable")
        destination = (self.library_root / Path(*relative.parts)).resolve()
        try:
            destination.relative_to(self.library_root)
        except ValueError as exc:
            raise PipelineQueueError("Library destination escapes its root") from exc
        if destination.exists():
            raise PipelineReviewRequiredError("library_collision")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.copy_only:
                shutil.copy2(source, destination)
            else:
                source.rename(destination)
        except OSError as exc:
            raise PipelineQueueError(
                "Library placement failed; source was preserved when possible"
            ) from exc
        expected_size = int(payload["encoded_size_bytes"])
        if not destination.is_file() or destination.stat().st_size != expected_size:
            raise PipelineQueueError("Library placement verification failed")
        return _write_contract(
            self.contract_root,
            item.media_id,
            "organize",
            {
                "schema_version": 1,
                "mode": "organized-media-contract",
                "media_id": item.media_id,
                "library_relative": relative.as_posix(),
                "output_size_bytes": expected_size,
                "copy_only": self.copy_only,
            },
        )
