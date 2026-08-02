"""Read-only, process-local optical-drive status discovery."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mkv_episode_matcher.disc.preflight import (
    CommandResult,
    PreflightError,
    parse_drives,
    run_info_command,
)


@dataclass(frozen=True)
class PublicDriveStatus:
    """Path-, label-, and hardware-redacted drive state for the web UI."""

    drive_index: int
    available: bool
    has_disc: bool
    disc_label: str | None = None


@dataclass(frozen=True)
class DriveStatusSnapshot:
    drives: tuple[PublicDriveStatus, ...]
    refreshed_at: str | None
    status: str
    error_type: str | None = None


DiscoveryRunner = Callable[..., CommandResult]


def _public_disc_label(value: str) -> str | None:
    """Return a short display label without allowing control/path characters."""

    cleaned = re.sub(r"[^A-Za-z0-9 ._()&'\-]+", " ", value).strip(" ._-")
    return cleaned[:80] or None


class DriveWatcher:
    """Cache explicit read-only refreshes; never inventory titles or mutate media."""

    def __init__(self, runner: DiscoveryRunner = run_info_command) -> None:
        self._runner = runner
        self._lock = threading.Lock()
        self._snapshot = DriveStatusSnapshot((), None, "not_scanned")
        self._device_names: dict[int, str] = {}

    def snapshot(self) -> DriveStatusSnapshot:
        with self._lock:
            return self._snapshot

    def device_name(self, drive_index: int) -> str | None:
        """Return one process-private cached drive letter without hardware access."""

        with self._lock:
            return self._device_names.get(drive_index)

    def refresh(
        self,
        executable: Path,
        *,
        timeout_seconds: int = 30,
    ) -> DriveStatusSnapshot:
        if timeout_seconds < 5 or timeout_seconds > 120:
            raise PreflightError("Drive discovery timeout is outside the safe range")
        with self._lock:
            try:
                result = self._runner(
                    executable,
                    "disc:9999",
                    timeout_seconds=timeout_seconds,
                )
                parsed_drives = sorted(
                    parse_drives(result.stdout), key=lambda item: item.index
                )
                refreshed = {
                    drive.index: PublicDriveStatus(
                        drive_index=drive.index,
                        available=bool(drive.visible and drive.enabled),
                        has_disc=drive.has_disc,
                        disc_label=(
                            _public_disc_label(drive.disc_name)
                            if drive.has_disc
                            else None
                        ),
                    )
                    for drive in parsed_drives
                    if (drive.visible and drive.enabled and drive.device_name.strip())
                }
                if not refreshed:
                    raise PreflightError("MakeMKV returned no optical-drive records")
                known = {
                    drive.drive_index: PublicDriveStatus(
                        drive_index=drive.drive_index,
                        available=drive.available,
                        has_disc=False,
                        disc_label=None,
                    )
                    for drive in self._snapshot.drives
                }
                known.update(refreshed)
                drives = tuple(known[index] for index in sorted(known))
                self._device_names.update({
                    drive.index: drive.device_name
                    for drive in parsed_drives
                    if drive.visible and drive.enabled and drive.device_name.strip()
                })
                self._snapshot = DriveStatusSnapshot(
                    drives=drives,
                    refreshed_at=datetime.now(UTC).isoformat(),
                    status="ready",
                )
            except PreflightError as error:
                self._snapshot = DriveStatusSnapshot(
                    drives=self._snapshot.drives,
                    refreshed_at=self._snapshot.refreshed_at,
                    status="error",
                    error_type=type(error).__name__,
                )
                raise
            return self._snapshot
