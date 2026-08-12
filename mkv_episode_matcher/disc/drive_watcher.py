"""Read-only, process-local optical-drive status discovery."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from mkv_episode_matcher.disc.drive_mapping import (
    DriveMappingError,
    DriveMappingStore,
    NativeOpticalDevice,
)
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
    mapping_id: str | None = None
    display_name: str | None = None
    connection_type: str | None = None
    mapping_status: str = "trusted"
    mapping_warning: str | None = None
    prior_similar_mapping_count: int = 0
    makemkv_confirmed: bool = True


@dataclass(frozen=True)
class DriveStatusSnapshot:
    drives: tuple[PublicDriveStatus, ...]
    refreshed_at: str | None
    status: str
    error_type: str | None = None
    error_code: str | None = None


DiscoveryRunner = Callable[..., CommandResult]
NativeDriveDiscovery = Callable[[], tuple[str, ...]]
NativeMediaDiscovery = Callable[[], dict[str, tuple[bool, str | None]]]
NativeIdentityDiscovery = Callable[[], tuple[NativeOpticalDevice, ...]]


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
        native_identity_discovery: NativeIdentityDiscovery | None = None,
        mapping_store: DriveMappingStore | None = None,
    ) -> None:
        self._runner = runner
        self._native_discovery = native_discovery or (
            discover_windows_optical_drives
            if runner is run_info_command
            else lambda: ()
        )
        self._native_media_discovery = native_media_discovery or (
            discover_windows_optical_media if runner is run_info_command else lambda: {}
        )
        self._native_identity_discovery = native_identity_discovery or (lambda: ())
        self._mapping_store = mapping_store
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._snapshot = DriveStatusSnapshot((), None, "not_scanned")
        self._device_names: dict[int, str] = {}
        self._mapping_keys: dict[str, str] = {}
        self._mapping_devices: dict[str, NativeOpticalDevice] = {}

    def snapshot(self) -> DriveStatusSnapshot:
        with self._lock:
            return self._snapshot

    def device_name(self, drive_index: int) -> str | None:
        """Return one process-private cached drive letter without hardware access."""

        with self._lock:
            return self._device_names.get(drive_index)

    def set_mapping_status(self, mapping_id: str, status: str) -> DriveStatusSnapshot:
        """Persist one reviewed device decision without accessing hardware."""

        if self._mapping_store is None:
            raise DriveMappingError("Durable optical-drive mapping is unavailable")
        if status not in {"trusted", "ignored"}:
            raise DriveMappingError("Optical-device mapping decision is invalid")
        with self._lock:
            device_key = self._mapping_keys.get(mapping_id)
            device = self._mapping_devices.get(mapping_id)
        if device_key is None:
            raise DriveMappingError(
                "Optical-device mapping identity is no longer current"
            )
        self._mapping_store.set_status(
            device_key,
            status,
            display_name=device.display_name if device else None,
            connection_type=device.connection_type if device else None,
        )
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                drives=tuple(
                    replace(
                        drive,
                        mapping_status=status,
                        mapping_warning=None,
                        prior_similar_mapping_count=0,
                    )
                    if drive.mapping_id == mapping_id
                    else drive
                    for drive in self._snapshot.drives
                ),
            )
            return self._snapshot

    @staticmethod
    def _mapping_plan_digest(snapshot: DriveStatusSnapshot) -> str:
        devices = [
            {
                "connection_type": drive.connection_type,
                "display_name": drive.display_name,
                "drive_index": drive.drive_index,
                "makemkv_confirmed": drive.makemkv_confirmed,
                "mapping_id": drive.mapping_id,
                "mapping_status": drive.mapping_status,
            }
            for drive in snapshot.drives
            if drive.mapping_id is not None
        ]
        encoded = json.dumps(
            sorted(devices, key=lambda item: str(item["mapping_id"])),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def mapping_plan_sha256(self) -> str | None:
        """Return a path-free digest binding the current mapping wizard snapshot."""

        if self._mapping_store is None:
            return None
        with self._lock:
            return self._mapping_plan_digest(self._snapshot)

    def apply_mapping_plan(
        self,
        expected_plan_sha256: str,
        decisions: dict[str, str],
        *,
        retire_absent_trusted: bool = True,
    ) -> tuple[DriveStatusSnapshot, int]:
        """Atomically apply a reviewed decision for every current identity."""

        if self._mapping_store is None:
            raise DriveMappingError("Durable optical-drive mapping is unavailable")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
            raise DriveMappingError("Optical-drive mapping plan digest is invalid")
        with self._lock:
            if self._mapping_plan_digest(self._snapshot) != expected_plan_sha256:
                raise DriveMappingError(
                    "The detected optical-device set changed; review the refreshed list"
                )
            current_ids = {
                drive.mapping_id
                for drive in self._snapshot.drives
                if drive.mapping_id is not None
            }
            if current_ids != set(decisions):
                raise DriveMappingError(
                    "Every currently detected optical device must have one decision"
                )
            devices = tuple(
                self._mapping_devices[mapping_id]
                for mapping_id in sorted(current_ids)
                if mapping_id in self._mapping_devices
            )
            if len(devices) != len(current_ids):
                raise DriveMappingError(
                    "The detected optical-device identity changed; refresh and review it"
                )
            by_key = {
                device.device_key: decisions[device.mapping_id] for device in devices
            }
            retired = self._mapping_store.replace_current(
                devices,
                by_key,
                retire_absent_trusted=retire_absent_trusted,
            )
            self._snapshot = replace(
                self._snapshot,
                drives=tuple(
                    replace(
                        drive,
                        mapping_status=decisions[drive.mapping_id],
                        mapping_warning=None,
                        prior_similar_mapping_count=0,
                    )
                    if drive.mapping_id is not None
                    else drive
                    for drive in self._snapshot.drives
                ),
            )
            return self._snapshot, retired

    def _mapping_fields(self, native: NativeOpticalDevice | None) -> dict[str, object]:
        if native is None:
            return {
                "mapping_id": None,
                "display_name": None,
                "connection_type": None,
                "mapping_status": "unmapped" if self._mapping_store else "trusted",
                "mapping_warning": "identity_unavailable"
                if self._mapping_store
                else None,
                "prior_similar_mapping_count": 0,
            }
        status = "trusted"
        prior_similar_count = 0
        if self._mapping_store is not None:
            try:
                status = self._mapping_store.status(native.device_key)
                if status == "unmapped":
                    prior_similar_count = self._mapping_store.similar_prior_count(
                        native
                    )
            except DriveMappingError:
                status = "unmapped"
        return {
            "mapping_id": native.mapping_id,
            "display_name": native.display_name,
            "connection_type": native.connection_type,
            "mapping_status": status,
            "mapping_warning": (
                "possible_identity_change"
                if status == "unmapped" and prior_similar_count
                else "new_device"
                if status == "unmapped"
                else None
            ),
            "prior_similar_mapping_count": prior_similar_count,
        }

    def refresh_in_progress(self) -> bool:
        """Report whether one read-only MakeMKV discovery currently owns the runner."""

        acquired = self._refresh_lock.acquire(blocking=False)
        if acquired:
            self._refresh_lock.release()
        return not acquired

    def mapping_required(self) -> bool:
        """Return whether this watcher enforces the durable device allowlist."""

        return self._mapping_store is not None

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
                drive.drive_index == drive_index
                and drive.has_disc
                and drive.mapping_status == "trusted"
                and drive.makemkv_confirmed
                for drive in self._snapshot.drives
            ):
                raise ValueError(
                    "Cannot bind a job to an untrusted, unconfirmed, or empty optical drive"
                )
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
                by_index[drive_index] = replace(
                    previous,
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

    def refresh(  # noqa: C901
        self,
        executable: Path,
        *,
        timeout_seconds: int = 30,
    ) -> DriveStatusSnapshot:
        if timeout_seconds < 5 or timeout_seconds > 120:
            raise PreflightError("Drive discovery timeout is outside the safe range")
        if not self._refresh_lock.acquire(blocking=False):
            raise PreflightError("MakeMKV drive discovery is already in progress")
        try:
            result = self._runner(
                executable,
                "disc:9999",
                timeout_seconds=timeout_seconds,
            )
            parsed_drives = sorted(
                parse_drives(result.stdout), key=lambda item: item.index
            )
            try:
                native_identities = self._native_identity_discovery()
            except DriveMappingError:
                # A transient Windows/CIM timeout must not demote every stable
                # drive after a prior successful identity refresh. Reuse only
                # the process-local, previously verified bindings; a first
                # refresh or genuinely new device remains unmapped.
                with self._lock:
                    native_identities = tuple(self._mapping_devices.values())
            native_by_name = {
                item.device_name.upper().rstrip("\\/"): item
                for item in native_identities
            }
            native_names = tuple(
                sorted({
                    name.strip().upper().rstrip("\\/")
                    for name in (
                        *self._native_discovery(),
                        *(item.device_name for item in native_identities),
                    )
                    if re.fullmatch(r"[A-Z]:[\\/]?", name.strip().upper())
                })
            )
            with self._lock:
                refreshed: dict[int, PublicDriveStatus] = {}
                parsed_device_names: dict[int, str] = {}
                mapping_keys = {
                    native.mapping_id: native.device_key for native in native_identities
                }
                for drive in parsed_drives:
                    if not (
                        drive.visible and drive.enabled and drive.device_name.strip()
                    ):
                        continue
                    device_name = drive.device_name.strip().upper().rstrip("\\/")
                    native = native_by_name.get(device_name)
                    mapping_fields = self._mapping_fields(native)
                    previous = next(
                        (
                            item
                            for item in self._snapshot.drives
                            if (
                                native is not None
                                and item.mapping_id == native.mapping_id
                            )
                            or (native is None and item.drive_index == drive.index)
                        ),
                        None,
                    )
                    public_label = (
                        _public_disc_label(drive.disc_name) if drive.has_disc else None
                    )
                    same_disc = bool(
                        previous
                        and previous.has_disc
                        and previous.disc_label == public_label
                    )
                    refreshed[drive.index] = PublicDriveStatus(
                        drive_index=drive.index,
                        available=bool(drive.visible and drive.enabled),
                        has_disc=drive.has_disc,
                        disc_label=public_label,
                        current_job_id=(
                            previous.current_job_id if same_disc and previous else None
                        ),
                        current_disc_fingerprint=(
                            previous.current_disc_fingerprint
                            if same_disc and previous
                            else None
                        ),
                        makemkv_confirmed=True,
                        **mapping_fields,
                    )
                    parsed_device_names[drive.index] = device_name
                if not refreshed:
                    raise PreflightError("MakeMKV returned no optical-drive records")
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
                        makemkv_confirmed=False,
                        **self._mapping_fields(native_by_name.get(device_name)),
                    )
                    parsed_device_names[drive_index] = device_name
                    used_indexes.add(drive_index)
                known = (
                    {
                        drive.drive_index: replace(
                            drive,
                            has_disc=False,
                            disc_label=None,
                            current_job_id=None,
                            current_disc_fingerprint=None,
                        )
                        for drive in self._snapshot.drives
                    }
                    if not native_names
                    else {}
                )
                known.update(refreshed)
                drives = tuple(known[index] for index in sorted(known))
                if native_names:
                    self._device_names = parsed_device_names
                else:
                    self._device_names.update(parsed_device_names)
                self._mapping_keys = mapping_keys
                self._mapping_devices = {
                    native.mapping_id: native for native in native_identities
                }
                self._snapshot = DriveStatusSnapshot(
                    drives=drives,
                    refreshed_at=datetime.now(UTC).isoformat(),
                    status="ready",
                )
                return self._snapshot
        except PreflightError as error:
            message = str(error).casefold()
            error_code = (
                "timeout"
                if "timed out" in message
                else "executable_missing"
                if "executable not found" in message
                else "no_drives"
                if "no optical-drive records" in message
                else "discovery_failed"
            )
            with self._lock:
                self._snapshot = DriveStatusSnapshot(
                    drives=self._snapshot.drives,
                    refreshed_at=self._snapshot.refreshed_at,
                    status="error",
                    error_type=type(error).__name__,
                    error_code=error_code,
                )
            raise
        finally:
            self._refresh_lock.release()
