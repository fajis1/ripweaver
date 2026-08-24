"""Exact, path-redacted authorization planning for queued library placement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.organizer import (
    OrganizationPlanError,
    add_jellyfin_version_label,
    inspect_episode_destination,
    inspect_episode_version_destination,
    jellyfin_resolution_label,
    omit_placeholder_episode_title,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueError, PipelineQueueStore


@dataclass(frozen=True)
class OrganizationAuthorizationItem:
    media_id: str
    destination_relative: str
    kind: str
    collision: bool


@dataclass(frozen=True)
class OrganizationAuthorizationPlan:
    media_ids: tuple[str, ...]
    artifact_sha256s: tuple[str, ...]
    tv_count: int
    movie_count: int
    collision_media_ids: tuple[str, ...]
    items: tuple[OrganizationAuthorizationItem, ...]
    plan_sha256: str

    def public_dict(self) -> dict[str, object]:
        return {
            "media_ids": list(self.media_ids),
            "item_count": len(self.media_ids),
            "tv_count": self.tv_count,
            "movie_count": self.movie_count,
            "collision_media_ids": list(self.collision_media_ids),
            "collision_count": len(self.collision_media_ids),
            "items": [
                {
                    "media_id": item.media_id,
                    "destination_relative": item.destination_relative,
                    "kind": item.kind,
                    "collision": item.collision,
                }
                for item in self.items
            ],
            "plan_sha256": self.plan_sha256,
            "operation": "move-verified-encode",
            "overwrite_authorized": False,
        }


def _destination(payload: dict, config: Config) -> tuple[Path, PurePosixPath, str]:
    relative = PurePosixPath(str(payload.get("library_relative", "")))
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() != ".mkv"
    ):
        raise PipelineQueueError("Library destination contract is unsafe")
    root = (
        config.jellyfin_tv_root
        if payload.get("episode_id")
        else config.jellyfin_movie_root
    )
    kind = "tv" if payload.get("episode_id") else "movie"
    if root is None or not root.is_dir():
        raise PipelineQueueError(f"Configured Jellyfin {kind} root is unavailable")
    try:
        relative = omit_placeholder_episode_title(relative, payload.get("episode_id"))
        relative = add_jellyfin_version_label(
            relative,
            jellyfin_resolution_label(
                payload.get("encoded_height"), payload.get("encoded_field_order")
            ),
        )
    except OrganizationPlanError as exc:
        raise PipelineQueueError("Encoded resolution contract is invalid") from exc
    destination = (root.resolve() / Path(*relative.parts)).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise PipelineQueueError("Library destination escapes its root") from exc
    return destination, relative, kind


def organization_item_has_collision(payload: dict, config: Config) -> bool:
    """Recheck one verified transcode against its exact versioned destination."""

    destination, _relative, _kind = _destination(payload, config)
    episode_id = payload.get("episode_id")
    if not episode_id:
        return destination.exists()
    try:
        inspector = (
            inspect_episode_version_destination
            if config.automatic_organization_enabled
            else inspect_episode_destination
        )
        status, _conflicts = inspector(
            destination.parent, destination.name, str(episode_id)
        )
    except OrganizationPlanError as exc:
        raise PipelineQueueError("Episode destination contract is invalid") from exc
    return status in {
        "review-existing-destination",
        "review-existing-episode",
        "review-existing-episode-version",
    }


def build_organization_authorization_plan(
    store: PipelineQueueStore, config: Config
) -> OrganizationAuthorizationPlan:
    selected = tuple(
        item
        for item in store.list_items()
        if item.stage == "organize" and item.state == "queued"
    )
    if not selected:
        raise PipelineQueueError("No verified transcodes are queued for organization")
    rows = []
    collisions = []
    public_items = []
    tv_count = 0
    movie_count = 0
    for item in selected:
        try:
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError(
                "Transcode contract is unavailable or invalid"
            ) from exc
        if payload.get("mode") != "verified-transcode-contract":
            raise PipelineQueueError(
                "Organization requires a verified transcode contract"
            )
        _destination_path, relative, kind = _destination(payload, config)
        tv_count += kind == "tv"
        movie_count += kind == "movie"
        collision = organization_item_has_collision(payload, config)
        if collision:
            collisions.append(item.media_id)
        public_items.append(
            OrganizationAuthorizationItem(
                media_id=item.media_id,
                destination_relative=relative.as_posix(),
                kind=kind,
                collision=collision,
            )
        )
        rows.append((item.media_id, item.artifact.contract_sha256, kind))
    serialized = json.dumps(rows, separators=(",", ":"), sort_keys=True)
    return OrganizationAuthorizationPlan(
        media_ids=tuple(row[0] for row in rows),
        artifact_sha256s=tuple(row[1] for row in rows),
        tv_count=tv_count,
        movie_count=movie_count,
        collision_media_ids=tuple(collisions),
        items=tuple(public_items),
        plan_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )
