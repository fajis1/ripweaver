"""Explicit production adapters for the serialized downstream pipeline."""

from __future__ import annotations

import json
import re
import shutil
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from mkv_episode_matcher.disc.content_policy import identification_order
from mkv_episode_matcher.media.handbrake import (
    HandBrakeJob,
    HandBrakeProfile,
    execute_handbrake_job,
    partial_output_path,
    source_video_height,
)
from mkv_episode_matcher.media.organizer import (
    OrganizationPlanError,
    add_jellyfin_version_label,
    build_episode_filename,
    inspect_episode_destination,
    jellyfin_resolution_label,
)
from mkv_episode_matcher.pipeline_queue import (
    PipelineArtifact,
    PipelineQueueError,
    PipelineReviewRequiredError,
    QueuedPipelineItem,
    StageOutcome,
    build_artifact,
)


class MatchEngine(Protocol):
    def process_path(self, path: Path, **kwargs): ...


def _uses_tv_library(payload: dict[str, Any]) -> bool:
    return bool(payload.get("episode_id")) or payload.get("library_kind") == "tv"


def _special_feature_uses_tv_library(
    context: dict[str, Any], assignment: dict[str, Any]
) -> bool:
    if assignment.get("library_kind") == "tv":
        return True
    series_name = context.get("series_name")
    return (
        assignment.get("media_kind", "extra") != "movie"
        and context.get("content_hint") not in {"movie", "extras"}
        and isinstance(series_name, str)
        and bool(series_name.strip())
        and not _placeholder_series(series_name)
    )


def _safe_feature_component(value: object) -> str:
    if not isinstance(value, str):
        raise PipelineReviewRequiredError("special_feature_name_review")
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        raise PipelineReviewRequiredError("special_feature_name_review")
    return cleaned[:160]


def _placeholder_series(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return normalized in {"unmatched", "unknown", "unknownseries"}


def _placeholder_episode_contract(
    payload: dict[str, Any], relative: PurePosixPath
) -> bool:
    """Recognize old synthetic episode labels that are not real matches."""

    if not isinstance(payload.get("episode_id"), str) or not relative.parts:
        return False
    if _placeholder_series(relative.parts[0]):
        return True
    return (
        re.fullmatch(
            r"(?:unmatched|unknown(?: series)?)\s*-\s*s\d{1,3}e\d{1,3}"
            r"\s*-\s*episode\s+\d+",
            relative.stem,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _organization_collision_status(destination: Path, episode_id: object) -> str:
    if episode_id:
        try:
            status, _conflicts = inspect_episode_destination(
                destination.parent, destination.name, str(episode_id)
            )
        except OrganizationPlanError as exc:
            raise PipelineQueueError("Episode destination contract is invalid") from exc
        return status
    existing_names = (
        {path.name.casefold() for path in destination.parent.iterdir()}
        if destination.parent.is_dir()
        else set()
    )
    return (
        "review-existing-destination"
        if destination.name.casefold() in existing_names
        else "clear"
    )


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
    root: Path,
    media_id: str,
    stage: str,
    payload: dict[str, Any],
    *,
    allow_revision: bool = False,
) -> PipelineArtifact:
    if not root.is_dir():
        raise PipelineQueueError("Pipeline contract root is unavailable")
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path = root / f"{media_id}.{stage}.json"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") != serialized:
                if not allow_revision:
                    raise PipelineQueueError("Pipeline output contract collision")
                revision = sha256(serialized.encode("utf-8")).hexdigest()[:16]
                path = root / f"{media_id}.{stage}-{revision}.json"
                if path.exists() and path.read_text(encoding="utf-8") != serialized:
                    raise PipelineQueueError("Pipeline output contract collision")
        except OSError as exc:
            raise PipelineQueueError(
                "Pipeline output contract could not be read"
            ) from exc
    if not path.exists():
        try:
            path.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            raise PipelineQueueError(
                "Pipeline output contract could not be written"
            ) from exc
    return build_artifact(stage, path)


class IdentifyStageAdapter:
    """Dry-run episode identification that never renames its verified rip."""

    def __init__(
        self,
        engine: MatchEngine,
        contract_root: Path,
        *,
        tv_library_root: Path | None = None,
        movie_library_root: Path | None = None,
    ):
        self.engine = engine
        self.contract_root = contract_root.resolve()
        self.tv_library_root = (
            tv_library_root.resolve() if tv_library_root is not None else None
        )
        self.movie_library_root = (
            movie_library_root.resolve() if movie_library_root is not None else None
        )

    def _identified_outcome(
        self, item: QueuedPipelineItem, payload: dict[str, Any]
    ) -> PipelineArtifact | StageOutcome:
        artifact = _write_contract(
            self.contract_root,
            item.media_id,
            "identify",
            payload,
            allow_revision=True,
        )
        if payload.get("existing_output_policy") == "replace-after-verification":
            return artifact
        root = self.tv_library_root if _uses_tv_library(payload) else self.movie_library_root
        if root is None or not root.is_dir():
            return artifact
        relative = PurePosixPath(str(payload.get("library_relative", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise PipelineQueueError("Library destination contract is unsafe")
        destination = (root / Path(*relative.parts)).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise PipelineQueueError("Library destination escapes its root") from exc
        if _organization_collision_status(destination, payload.get("episode_id")) in {
            "review-existing-destination",
            "review-existing-episode",
        }:
            return StageOutcome(artifact, "library_collision")
        return artifact

    def __call__(self, item: QueuedPipelineItem) -> PipelineArtifact:  # noqa: C901
        payload = _load_contract(item, "verified-rip-contract")
        source = _source_from_contract(payload, "source_path", "source_size_bytes")
        context = payload.get("media_context")
        if not isinstance(context, dict) or not context.get("series_name"):
            raise PipelineReviewRequiredError("missing_series_context")
        episode_assignments = context.get("episode_assignments")
        if isinstance(episode_assignments, list) and episode_assignments:
            if (
                ".all-season-" in item.artifact.contract_path.name
                and context.get("identification_policy_version") != 2
            ):
                raise PipelineReviewRequiredError("unmatched_disc_analysis_required")
            if _placeholder_series(context.get("series_name")):
                raise PipelineReviewRequiredError("unmatched_disc_analysis_required")
            title_index = payload.get("title_index")
            assignment = next(
                (
                    entry
                    for entry in episode_assignments
                    if isinstance(entry, dict)
                    and entry.get("title_index") == title_index
                ),
                None,
            )
            if assignment is None:
                raise PipelineReviewRequiredError("episode_assignment_review")
            try:
                season = int(assignment["season"])
                episode_number = int(assignment["episode"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PipelineQueueError(
                    "Reviewed episode assignment is invalid"
                ) from exc
            if not 0 <= season <= 99 or not 1 <= episode_number <= 999:
                raise PipelineQueueError("Reviewed episode assignment is invalid")
            series_name = _safe_feature_component(context.get("series_name"))
            title = _safe_feature_component(assignment.get("title"))
            episode_id = f"S{season:02d}E{episode_number:02d}"
            filename = build_episode_filename(series_name, episode_id, title)
            relative = Path(series_name) / f"Season {season:02d}" / filename
            return self._identified_outcome(
                item,
                {
                    "schema_version": 1,
                    "mode": "identified-episode-contract",
                    "media_id": item.media_id,
                    "source_path": str(source),
                    "source_size_bytes": source.stat().st_size,
                    "confidence": 1.0,
                    "episode_id": episode_id,
                    "library_kind": "tv",
                    "library_relative": relative.as_posix(),
                    "identification_order": ["reviewed-release-catalogue"],
                    "handbrake_profile_id": context.get("handbrake_profile_id"),
                    "existing_output_policy": context.get(
                        "existing_output_policy", "preserve"
                    ),
                },
            )
        assignments = context.get("special_feature_assignments")
        if isinstance(assignments, list) and assignments:
            title_index = payload.get("title_index")
            if not isinstance(title_index, int) or isinstance(title_index, bool):
                match = re.search(r"-title-(\d{3})(?:-|$)", item.media_id)
                if match is None:
                    raise PipelineQueueError(
                        "Special-feature media ID has no title index"
                    )
                title_index = int(match.group(1))
            assignment = next(
                (
                    entry
                    for entry in assignments
                    if isinstance(entry, dict)
                    and entry.get("title_index") == title_index
                ),
                None,
            )
            if assignment is None:
                raise PipelineReviewRequiredError("special_feature_assignment_review")
            if (
                assignment.get("classification") != "matched-feature"
                or assignment.get("fallback_name_policy") != "none"
            ):
                raise PipelineReviewRequiredError("special_feature_evidence_required")
            media_kind = assignment.get("media_kind", "extra")
            tv_library = _special_feature_uses_tv_library(context, assignment)
            library_title = _safe_feature_component(
                context.get("special_feature_library_title")
            )
            year = context.get("special_feature_library_year")
            feature_title = _safe_feature_component(assignment.get("matched_title"))
            if media_kind == "movie":
                movie_name = (
                    f"{feature_title} ({year})"
                    if isinstance(year, int) and not isinstance(year, bool)
                    else feature_title
                )
                relative = Path(movie_name) / f"{movie_name}.mkv"
                identification = ["gemini-descriptive-movie"]
            else:
                folder = (
                    "Extras"
                    if tv_library
                    else _safe_feature_component(assignment.get("jellyfin_folder"))
                )
                release_name = (
                    f"{library_title} ({year})"
                    if isinstance(year, int) and not isinstance(year, bool)
                    else library_title
                )
                relative = Path(release_name) / folder / f"{feature_title}.mkv"
                identification = ["extras"]
            return self._identified_outcome(
                item,
                {
                    "schema_version": 1,
                    "mode": "identified-episode-contract",
                    "media_id": item.media_id,
                    "source_path": str(source),
                    "source_size_bytes": source.stat().st_size,
                    "confidence": assignment.get("gemini_confidence", 1.0),
                    "episode_id": None,
                    "library_kind": "tv" if tv_library else "movie",
                    "library_relative": relative.as_posix(),
                    "identification_order": identification,
                    "handbrake_profile_id": context.get("handbrake_profile_id"),
                    "special_feature_catalog_id": context.get(
                        "special_feature_catalog_id"
                    ),
                    "existing_output_policy": context.get(
                        "existing_output_policy", "preserve"
                    ),
                    "provisional_match": bool(assignment.get("provisional_match")),
                    "gemini_confidence": assignment.get("gemini_confidence"),
                },
            )
        hint = context.get("content_hint")
        strategy_order = (
            ("tv", "movie", "extras") if hint is None else identification_order(hint)
        )
        if strategy_order[0] == "extras":
            raise PipelineReviewRequiredError("special_feature_evidence_required")
        if strategy_order[0] != "tv":
            raise PipelineReviewRequiredError(
                f"{strategy_order[0].replace('-', '_')}_identification_required"
            )
        season = context.get("season")
        if not isinstance(season, int) or isinstance(season, bool):
            raise PipelineReviewRequiredError("unmatched_disc_analysis_required")
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
        return self._identified_outcome(
            item,
            {
                "schema_version": 1,
                "mode": "identified-episode-contract",
                "media_id": item.media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "confidence": float(match.confidence),
                "episode_id": episode_id,
                "library_kind": "tv",
                "library_relative": relative.as_posix(),
                "identification_order": list(strategy_order),
                "handbrake_profile_id": context.get("handbrake_profile_id"),
                "existing_output_policy": context.get(
                    "existing_output_policy", "preserve"
                ),
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
        profiles: dict[str, HandBrakeProfile] | None = None,
        profile_override_id: str | None = None,
        resolution_profile_ids: dict[str, str] | None = None,
        tv_library_root: Path | None = None,
        movie_library_root: Path | None = None,
        timeout_seconds: int = 21600,
    ):
        self.handbrake = handbrake.resolve()
        self.ffprobe = ffprobe.resolve()
        self.output_root = output_root.resolve()
        self.run_root = run_root.resolve()
        self.contract_root = contract_root.resolve()
        self.profile = profile
        self.profiles = profiles or {}
        self.profile_override_id = profile_override_id
        self.resolution_profile_ids = resolution_profile_ids or {}
        self.tv_library_root = (
            tv_library_root.resolve() if tv_library_root is not None else None
        )
        self.movie_library_root = (
            movie_library_root.resolve() if movie_library_root is not None else None
        )
        self.timeout_seconds = timeout_seconds

    def _preflight_library_collision(
        self, payload: dict[str, Any], relative: PurePosixPath
    ) -> None:
        if payload.get("existing_output_policy") == "replace-after-verification":
            return
        library_root = (
            self.tv_library_root if _uses_tv_library(payload) else self.movie_library_root
        )
        if library_root is None:
            return
        if not library_root.is_dir():
            raise PipelineReviewRequiredError("missing_library_root")
        library_destination = (library_root / Path(*relative.parts)).resolve()
        try:
            library_destination.relative_to(library_root)
        except ValueError as exc:
            raise PipelineQueueError("Library destination escapes its root") from exc
        collision_status = _organization_collision_status(
            library_destination, payload.get("episode_id")
        )
        if collision_status in {
            "review-existing-destination",
            "review-existing-episode",
        }:
            raise PipelineReviewRequiredError("library_collision")

    def _select_profile(
        self, payload: dict[str, Any], source: Path
    ) -> tuple[str | None, HandBrakeProfile]:
        if self.profile_override_id is not None:
            return self.profile_override_id, self.profile
        profile_id = payload.get("handbrake_profile_id")
        if profile_id is not None and profile_id not in self.profiles:
            raise PipelineReviewRequiredError("handbrake_profile_unavailable")
        if profile_id is None and self.resolution_profile_ids:
            height = source_video_height(self.ffprobe, source)
            resolution = (
                "480p"
                if height <= 576
                else "720p"
                if height <= 800
                else "1080p"
                if height <= 1200
                else "2160p"
            )
            profile_id = self.resolution_profile_ids.get(resolution)
        return profile_id, self.profiles.get(str(profile_id), self.profile)

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
        if _placeholder_episode_contract(payload, relative):
            raise PipelineReviewRequiredError("placeholder_identification_required")
        self._preflight_library_collision(payload, relative)
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

        profile_id, selected_profile = self._select_profile(payload, source)
        attempt = 1
        while True:
            job = HandBrakeJob(
                item.media_id,
                source,
                destination,
                selected_profile,
                attempt_number=attempt,
            )
            run_dir = self.run_root / f"{item.media_id}-attempt-{attempt:03d}"
            if not partial_output_path(job).exists() and not run_dir.exists():
                break
            attempt += 1
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
                "original_source_path": str(source),
                "original_source_size_bytes": source.stat().st_size,
                "library_relative": relative.as_posix(),
                "episode_id": payload.get("episode_id"),
                "library_kind": payload.get("library_kind"),
                "existing_output_policy": payload.get(
                    "existing_output_policy", "preserve"
                ),
                "encoded_width": result.width,
                "encoded_height": result.height,
                "encoded_field_order": result.field_order,
                "handbrake_profile_id": profile_id,
                "provisional_match": bool(payload.get("provisional_match")),
                "gemini_confidence": payload.get("gemini_confidence"),
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
        allow_version_coexistence: bool = False,
        deletion_staging_root: Path | None = None,
    ):
        if not confirm_organize:
            raise PipelineQueueError(
                "Organization adapter requires explicit authorization"
            )
        self.library_root = library_root.resolve()
        self.contract_root = contract_root.resolve()
        self.copy_only = copy_only
        self.allow_version_coexistence = allow_version_coexistence
        self.deletion_staging_root = (
            deletion_staging_root.resolve()
            if deletion_staging_root is not None
            else None
        )

    def __call__(self, item: QueuedPipelineItem) -> PipelineArtifact:  # noqa: C901
        payload = _load_contract(item, "verified-transcode-contract")
        source = _source_from_contract(payload, "encoded_path", "encoded_size_bytes")
        relative = PurePosixPath(str(payload.get("library_relative", "")))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.lower() != ".mkv"
        ):
            raise PipelineQueueError("Library destination contract is unsafe")
        if _placeholder_episode_contract(payload, relative):
            raise PipelineReviewRequiredError("placeholder_identification_required")
        if not self.library_root.is_dir():
            raise PipelineQueueError("Library root is unavailable")
        try:
            resolution_label = jellyfin_resolution_label(
                payload.get("encoded_height"), payload.get("encoded_field_order")
            )
            relative = add_jellyfin_version_label(relative, resolution_label)
        except OrganizationPlanError as exc:
            raise PipelineQueueError("Encoded resolution contract is invalid") from exc
        destination = (self.library_root / Path(*relative.parts)).resolve()
        try:
            destination.relative_to(self.library_root)
        except ValueError as exc:
            raise PipelineQueueError("Library destination escapes its root") from exc
        episode_id = payload.get("episode_id")
        status = _organization_collision_status(destination, episode_id)
        if status == "review-existing-destination" or (
            status == "review-existing-episode" and not self.allow_version_coexistence
        ):
            raise PipelineReviewRequiredError("library_collision")
        archived_source = None
        original_source = None
        original_size = None
        if self.deletion_staging_root is not None and {
            "original_source_path",
            "original_source_size_bytes",
        }.issubset(payload):
            if not self.deletion_staging_root.is_dir():
                raise PipelineQueueError("Deletion staging root is unavailable")
            original_source = _source_from_contract(
                payload, "original_source_path", "original_source_size_bytes"
            )
            original_size = int(payload["original_source_size_bytes"])
            archived_source = (
                self.deletion_staging_root / item.media_id / original_source.name
            ).resolve()
            try:
                archived_source.relative_to(self.deletion_staging_root)
            except ValueError as exc:
                raise PipelineQueueError(
                    "Deletion staging destination is unsafe"
                ) from exc
            if archived_source.exists() or archived_source.parent.exists():
                raise PipelineReviewRequiredError("deletion_staging_collision")
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
        if archived_source is not None and original_source is not None:
            archived_source.parent.mkdir(parents=True, exist_ok=False)
            try:
                shutil.move(str(original_source), str(archived_source))
            except OSError as exc:
                raise PipelineQueueError(
                    "Source archival failed after verified library placement"
                ) from exc
            if (
                not archived_source.is_file()
                or archived_source.stat().st_size != original_size
            ):
                raise PipelineQueueError("Archived source verification failed")
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
                "archived_source_path": (
                    str(archived_source) if archived_source is not None else None
                ),
                "archived_source_size_bytes": original_size,
                "provisional_match": bool(payload.get("provisional_match")),
                "gemini_confidence": payload.get("gemini_confidence"),
            },
        )
