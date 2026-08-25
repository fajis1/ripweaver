"""Exact, path-redacted authorization identity for queued transcodes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.ffprobe_runner import FFprobeError, resolve_ffprobe_path
from mkv_episode_matcher.media.handbrake import HandBrakeError, resolve_handbrake_path
from mkv_episode_matcher.media.handbrake_profiles import HandBrakeProfileStore
from mkv_episode_matcher.pipeline_queue import PipelineQueueError, PipelineQueueStore


@dataclass(frozen=True)
class TranscodeAuthorizationPlan:
    media_ids: tuple[str, ...]
    plan_sha256: str
    default_profile_id: str
    profile_display_name: str
    handbrake: Path
    ffprobe: Path
    output_root: Path
    profile_override_id: str | None
    resolution_profile_ids: dict[str, str]

    def public_dict(self) -> dict[str, object]:
        return {
            "media_ids": list(self.media_ids),
            "item_count": len(self.media_ids),
            "plan_sha256": self.plan_sha256,
            "default_profile_id": self.default_profile_id,
            "profile_display_name": self.profile_display_name,
            "profile_selection": (
                "explicit" if self.profile_override_id is not None else "source-resolution"
            ),
            "resolution_profile_ids": self.resolution_profile_ids,
            "output_destination": "configured encoded staging root",
            "organization_authorized": False,
            "execution_authorized": False,
        }


def build_transcode_authorization_plan(
    store: PipelineQueueStore,
    profiles: HandBrakeProfileStore,
    config: Config,
    *,
    profile_id: str | None = None,
) -> TranscodeAuthorizationPlan:
    items = tuple(
        item
        for item in store.list_items()
        if item.stage == "transcode" and item.state == "queued"
    )
    if not items:
        raise PipelineQueueError("No queued transcode jobs are ready")
    available = {item.profile_id: item for item in profiles.list()}
    selected_profile_id = profile_id or config.default_handbrake_profile
    selected = available.get(selected_profile_id)
    if selected is None:
        raise PipelineQueueError("Selected HandBrake profile is unavailable")
    resolution_profile_ids = {
        resolution: configured
        for resolution, configured in {
            "480p": config.default_handbrake_profile_480p,
            "720p": config.default_handbrake_profile_720p,
            "1080p": config.default_handbrake_profile_1080p,
            "2160p": config.default_handbrake_profile_2160p,
        }.items()
        if configured is not None
    }
    if any(configured not in available for configured in resolution_profile_ids.values()):
        raise PipelineQueueError("A resolution-specific HandBrake profile is unavailable")
    if (
        config.transcode_output_root is None
        or not config.transcode_output_root.is_dir()
    ):
        raise PipelineQueueError("Configured encoded staging root is unavailable")
    try:
        handbrake = resolve_handbrake_path(config.handbrake_path)
    except HandBrakeError as exc:
        raise PipelineQueueError(
            "Configured HandBrakeCLI executable is unavailable; choose it in Settings"
        ) from exc
    try:
        ffprobe = resolve_ffprobe_path(config.ffprobe_path)
    except FFprobeError as exc:
        raise PipelineQueueError(
            "Configured FFprobe executable is unavailable; choose it in Settings"
        ) from exc
    identity = {
        "items": [
            {
                "media_id": item.media_id,
                "contract_sha256": item.artifact.contract_sha256,
            }
            for item in items
        ],
        "default_profile_id": selected.profile_id,
        "default_profile": asdict(selected.profile),
        "profile_override_id": profile_id,
        "resolution_profiles": {
            resolution: asdict(available[configured].profile)
            for resolution, configured in resolution_profile_ids.items()
        },
        "handbrake": str(handbrake),
        "ffprobe": str(ffprobe),
        "output_root": str(config.transcode_output_root.resolve()),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TranscodeAuthorizationPlan(
        media_ids=tuple(item.media_id for item in items),
        plan_sha256=digest,
        default_profile_id=selected.profile_id,
        profile_display_name=(
            selected.display_name
            if profile_id is not None
            else f"Resolution defaults (fallback: {selected.display_name})"
        ),
        handbrake=handbrake,
        ffprobe=ffprobe,
        output_root=config.transcode_output_root.resolve(),
        profile_override_id=profile_id,
        resolution_profile_ids=resolution_profile_ids,
    )
