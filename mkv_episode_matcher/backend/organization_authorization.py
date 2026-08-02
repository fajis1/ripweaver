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
    jellyfin_resolution_label,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueError, PipelineQueueStore


@dataclass(frozen=True)
class OrganizationAuthorizationPlan:
    media_ids: tuple[str, ...]
    artifact_sha256s: tuple[str, ...]
    tv_count: int
    movie_count: int
    collision_media_ids: tuple[str, ...]
    plan_sha256: str

    def public_dict(self) -> dict[str, object]:
        return {
            "media_ids": list(self.media_ids),
            "item_count": len(self.media_ids),
            "tv_count": self.tv_count,
            "movie_count": self.movie_count,
            "collision_media_ids": list(self.collision_media_ids),
            "collision_count": len(self.collision_media_ids),
            "plan_sha256": self.plan_sha256,
            "operation": "move-verified-encode",
            "overwrite_authorized": False,
        }


def _destination(payload: dict, config: Config) -> tuple[Path, str]:
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
    return destination, kind


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
        destination, kind = _destination(payload, config)
        tv_count += kind == "tv"
        movie_count += kind == "movie"
        if destination.exists():
            collisions.append(item.media_id)
        rows.append((item.media_id, item.artifact.contract_sha256, kind))
    serialized = json.dumps(rows, separators=(",", ":"), sort_keys=True)
    return OrganizationAuthorizationPlan(
        media_ids=tuple(row[0] for row in rows),
        artifact_sha256s=tuple(row[1] for row in rows),
        tv_count=tv_count,
        movie_count=movie_count,
        collision_media_ids=tuple(collisions),
        plan_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )
