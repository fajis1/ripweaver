"""Durable, non-secret HandBrake profile library."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from mkv_episode_matcher.media.handbrake import (
    HandBrakeError,
    HandBrakeProfile,
    validate_handbrake_profile,
)

_PROFILE_ID = re.compile(r"[a-z][a-z0-9-]{1,47}")


class HandBrakeProfileStoreError(RuntimeError):
    """Raised when a profile library operation fails safely."""


@dataclass(frozen=True)
class StoredHandBrakeProfile:
    profile_id: str
    display_name: str
    built_in: bool
    profile: HandBrakeProfile


BUILT_IN_PROFILES = (
    StoredHandBrakeProfile("balanced", "AMD VCN Balanced", True, HandBrakeProfile()),
    StoredHandBrakeProfile(
        "nvidia-balanced",
        "NVIDIA NVENC Balanced",
        True,
        HandBrakeProfile(encoder="nvenc_h265", encoder_preset="medium", quality=24),
    ),
    StoredHandBrakeProfile(
        "intel-balanced",
        "Intel Quick Sync Balanced",
        True,
        HandBrakeProfile(encoder="qsv_h265", encoder_preset="balanced", quality=24),
    ),
    StoredHandBrakeProfile(
        "cpu-balanced",
        "CPU x265 Balanced",
        True,
        HandBrakeProfile(encoder="x265", encoder_preset="medium", quality=22),
    ),
)


class HandBrakeProfileStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def _validate_identity(profile_id: str, display_name: str) -> None:
        if _PROFILE_ID.fullmatch(profile_id) is None:
            raise HandBrakeProfileStoreError("Profile ID is invalid")
        if not 1 <= len(display_name.strip()) <= 80:
            raise HandBrakeProfileStoreError("Profile display name is invalid")

    def _load_custom(self) -> tuple[StoredHandBrakeProfile, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError
            profiles = []
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError
                profile_id = str(item["profile_id"])
                display_name = str(item["display_name"])
                self._validate_identity(profile_id, display_name)
                profile = HandBrakeProfile(**item["profile"])
                validate_handbrake_profile(profile)
                profiles.append(
                    StoredHandBrakeProfile(
                        profile_id, display_name.strip(), False, profile
                    )
                )
        except (OSError, KeyError, TypeError, ValueError, HandBrakeError) as exc:
            raise HandBrakeProfileStoreError("Profile library is invalid") from exc
        if len({item.profile_id for item in profiles}) != len(profiles):
            raise HandBrakeProfileStoreError("Profile library contains duplicate IDs")
        return tuple(profiles)

    def list(self) -> tuple[StoredHandBrakeProfile, ...]:
        with self._lock:
            return (*BUILT_IN_PROFILES, *self._load_custom())

    def save_custom(
        self, profile_id: str, display_name: str, profile: HandBrakeProfile
    ) -> StoredHandBrakeProfile:
        self._validate_identity(profile_id, display_name)
        if any(item.profile_id == profile_id for item in BUILT_IN_PROFILES):
            raise HandBrakeProfileStoreError("Built-in profiles cannot be replaced")
        try:
            validate_handbrake_profile(profile)
        except HandBrakeError as exc:
            raise HandBrakeProfileStoreError("Profile settings are invalid") from exc
        with self._lock:
            custom = {item.profile_id: item for item in self._load_custom()}
            stored = StoredHandBrakeProfile(
                profile_id, display_name.strip(), False, profile
            )
            custom[profile_id] = stored
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = [
                {
                    "profile_id": item.profile_id,
                    "display_name": item.display_name,
                    "profile": asdict(item.profile),
                }
                for item in sorted(custom.values(), key=lambda value: value.profile_id)
            ]
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, sort_keys=True)
                    stream.write("\n")
                os.replace(temporary, self.path)
            except OSError as exc:
                Path(temporary).unlink(missing_ok=True)
                raise HandBrakeProfileStoreError(
                    "Profile library could not be saved"
                ) from exc
            return stored
