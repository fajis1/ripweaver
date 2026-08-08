"""Read-only, process-local optical-drive status discovery."""

from __future__ import annotations

import re
import sys
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    current_job_id: str | None = None
    current_disc_fingerprint: str | None = None


@dataclass(frozen=True)
class DriveStatusSnapshot:
    drives: tuple[PublicDriveStatus, ...]
    refreshed_at: str | None
    status: str
    error_type: str | None = None


DiscoveryRunner = Callable[..., CommandResult]
NativeDriveDiscovery = Callable[[], tuple[str, ...]]
NativeMediaDiscovery = Callable[[], dict[str, tuple[bool, str | None]]]


def discover_windows_optical_drives() -> tuple[str, ...]:
    """List Windows CD-ROM drive letters without testing media readiness."""

    if sys.platform != "win32":
        return ()
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetLogicalDrives.restype = ctypes.c_uint32
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    drive_mask = kernel32.GetLogicalDrives()
    optical: list[str] = []
    for ordinal in range(26):
        if not drive_mask & (1 << ordinal):
            continue
        letter = f"{chr(ord('A') + ordinal)}:"
        if kernel32.GetDriveTypeW(f"{letter}\\") == 5:  # DRIVE_CDROM
            optical.append(letter)
    return tuple(optical)


def discover_windows_optical_media() -> dict[str, tuple[bool, str | None]]:
    """Read Windows volume readiness/labels without invoking MakeMKV."""

    if sys.platform != "win32":
        return {}
    import ctypes

    kernel32 = ctypes.windll.kernel32
    media: dict[str, tuple[bool, str | None]] = {}
    for device_name in discover_windows_optical_drives():
        label = ctypes.create_unicode_buffer(261)
        ready = bool(
            kernel32.GetVolumeInformationW(
                f"{device_name}\\",
                label,
                len(label),
                None,
                None,
                None,
                None,
                0,
            )
        )
        media[device_name] = (
            ready,
            _public_disc_label(label.value) if ready else None,
        )
    return media


def _public_disc_label(value: str) -> str | None:
    """Return a short display label without allowing control/path characters."""

    cleaned = re.sub(r"[^A-Za-z0-9 ._()&'\-]+", " ", value).strip(" ._-")
    return cleaned[:80] or None


class DriveWatcher:
    """Cache explicit read-only refreshes; never inventory titles or mutate media."""

    def __init__(
        self,
        runner: DiscoveryRunner = run_info_command,
        native_discovery: NativeDriveDiscovery | None = None,
        native_media_discovery: NativeMediaDiscovery | None = None,
    ) -> None:
        self._runner = runner
        self._native_discovery = native_discovery or (
            discover_windows_optical_drives
            if runner is run_info_command
            else lambda: ()
        )
        self._native_media_discovery = native_media_discovery or (
            discover_windows_optical_media
            if runner is run_info_command
            else lambda: {}
        )
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

    def bind_current_job(
        self, drive_index: int, job_id: str, disc_fingerprint: str
    ) -> None:
        """Attach an inventoried disc identity to its current hardware location."""

        with self._lock:
            if not re.fullmatch(r"rip-[0-9a-f]{32}", job_id):
                raise ValueError("Current rip job ID is invalid")
            if not re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint):
                raise ValueError("Current disc fingerprint is invalid")
            if not any(
                drive.drive_index == drive_index and drive.has_disc
                for drive in self._snapshot.drives
            ):
                raise ValueError("Cannot bind a job to an empty optical drive")
            self._snapshot = replace(
                self._snapshot,
                drives=tuple(
                    replace(
                        drive,
                        current_job_id=job_id,
                        current_disc_fingerprint=disc_fingerprint,
                    )
                    if drive.drive_index == drive_index
                    else drive
                    for drive in self._snapshot.drives
                ),
            )

    def invalidate_current_disc_bindings(self) -> None:
        """Forget tray attachments after a Windows volume-change event."""

        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                drives=tuple(
                    replace(
                        drive,
                        current_job_id=None,
                        current_disc_fingerprint=None,
                    )
                    for drive in self._snapshot.drives
                ),
            )

    def clear_current_job(
        self, drive_index: int, *, expected_disc_fingerprint: str
    ) -> None:
        """Detach one exact cached disc identity without accessing hardware."""

        with self._lock:
            selected = next(
                (
                    drive
                    for drive in self._snapshot.drives
                    if drive.drive_index == drive_index
                ),
                None,
            )
            if (
                selected is None
                or selected.current_disc_fingerprint != expected_disc_fingerprint
            ):
                raise ValueError("Current disc identity changed before reset")
            self._snapshot = replace(
                self._snapshot,
                drives=tuple(
                    replace(
                        drive,
                        current_job_id=None,
                        current_disc_fingerprint=None,
                    )
                    if drive.drive_index == drive_index
                    else drive
                    for drive in self._snapshot.drives
                ),
            )

    def refresh_native_media(self) -> DriveStatusSnapshot:
        """Update newly inserted idle-drive media while MakeMKV is busy elsewhere."""

        with self._lock:
            native_media = {
                name.strip().upper().rstrip("\\/"): state
                for name, state in self._native_media_discovery().items()
                if re.fullmatch(r"[A-Z]:[\\/]?", name.strip().upper())
            }
            if not native_media:
                return self._snapshot
            by_index = {drive.drive_index: drive for drive in self._snapshot.drives}
            reverse_devices = {
                device.upper().rstrip("\\/"): index
                for index, device in self._device_names.items()
            }
            for device_name, (has_disc, label) in native_media.items():
                drive_index = reverse_devices.get(device_name)
                if drive_index is None:
                    continue
                previous = by_index.get(
                    drive_index,
                    PublicDriveStatus(drive_index, True, False),
                )
                if not has_disc and previous.has_disc:
                    # A busy ripping drive may not answer the Windows volume
                    # query. Never make active media disappear on that basis.
                    continue
                same_disc = previous.has_disc and previous.disc_label == label
                by_index[drive_index] = PublicDriveStatus(
                    drive_index=drive_index,
                    available=True,
                    has_disc=has_disc,
                    disc_label=label if has_disc else None,
                    current_job_id=previous.current_job_id if same_disc else None,
                    current_disc_fingerprint=(
                        previous.current_disc_fingerprint if same_disc else None
                    ),
                )
            self._snapshot = DriveStatusSnapshot(
                drives=tuple(by_index[index] for index in sorted(by_index)),
                refreshed_at=datetime.now(UTC).isoformat(),
                status="ready",
            )
            return self._snapshot

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
                        current_job_id=next(
                            (
                                previous.current_job_id
                                for previous in self._snapshot.drives
                                if previous.drive_index == drive.index
                                and previous.has_disc
                                and previous.disc_label
                                == _public_disc_label(drive.disc_name)
                            ),
                            None,
                        ),
                        current_disc_fingerprint=next(
                            (
                                previous.current_disc_fingerprint
                                for previous in self._snapshot.drives
                                if previous.drive_index == drive.index
                                and previous.has_disc
                                and previous.disc_label
                                == _public_disc_label(drive.disc_name)
                            ),
                            None,
                        ),
                    )
                    for drive in parsed_drives
                    if (drive.visible and drive.enabled and drive.device_name.strip())
                }
                if not refreshed:
                    raise PreflightError("MakeMKV returned no optical-drive records")
                parsed_device_names = {
                    drive.index: drive.device_name.strip().upper().rstrip("\\/")
                    for drive in parsed_drives
                    if drive.visible and drive.enabled and drive.device_name.strip()
                }
                native_names = tuple(
                    sorted({
                        name.strip().upper().rstrip("\\/")
                        for name in self._native_discovery()
                        if re.fullmatch(r"[A-Z]:[\\/]?", name.strip().upper())
                    })
                )
                native_positions = {
                    device_name: ordinal
                    for ordinal, device_name in enumerate(native_names)
                }
                offsets = [
                    drive_index - native_positions[device_name]
                    for drive_index, device_name in parsed_device_names.items()
                    if device_name in native_positions
                ]
                index_offset = Counter(offsets).most_common(1)[0][0] if offsets else 0
                used_indexes = set(refreshed)
                for ordinal, device_name in enumerate(native_names):
                    if device_name in parsed_device_names.values():
                        continue
                    drive_index = ordinal + index_offset
                    if drive_index < 0 or drive_index in used_indexes:
                        drive_index = next(
                            index for index in range(32) if index not in used_indexes
                        )
                    refreshed[drive_index] = PublicDriveStatus(
                        drive_index=drive_index,
                        available=True,
                        has_disc=False,
                        disc_label=None,
                        current_job_id=None,
                        current_disc_fingerprint=None,
                    )
                    parsed_device_names[drive_index] = device_name
                    used_indexes.add(drive_index)
                known = {
                    drive.drive_index: PublicDriveStatus(
                        drive_index=drive.drive_index,
                        available=drive.available,
                        has_disc=False,
                        disc_label=None,
                        current_job_id=None,
                        current_disc_fingerprint=None,
                    )
                    for drive in self._snapshot.drives
                }
                known.update(refreshed)
                drives = tuple(known[index] for index in sorted(known))
                self._device_names.update(parsed_device_names)
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
