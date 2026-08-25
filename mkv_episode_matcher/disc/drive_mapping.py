"""Private Windows optical-device identity and durable trust decisions.

The Windows query reads Plug-and-Play metadata only. It does not test media
readiness, open a disc, invoke MakeMKV, or expose raw device instance IDs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


class DriveMappingError(RuntimeError):
    """Raised when a drive mapping cannot be loaded or changed safely."""


@dataclass(frozen=True)
class NativeOpticalDevice:
    """One path-redacted Windows optical device."""

    device_name: str
    device_key: str
    display_name: str
    connection_type: str

    @property
    def mapping_id(self) -> str:
        """Return a short opaque identifier suitable for the loopback UI."""

        return self.device_key[:16]


@dataclass(frozen=True)
class DriveMappingRecord:
    """One path-free durable decision and its sanitized device descriptor."""

    status: str
    display_name: str | None = None
    connection_type: str | None = None


_WINDOWS_OPTICAL_QUERY = r"""
$ErrorActionPreference = 'Stop'
$sha = [System.Security.Cryptography.SHA256]::Create()
$rows = foreach ($drive in Get-CimInstance Win32_CDROMDrive) {
    if (-not $drive.Drive -or -not $drive.PNPDeviceID) { continue }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$drive.PNPDeviceID)
    $key = [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    $enumerator = ([string]$drive.PNPDeviceID -split '\\', 2)[0].ToLowerInvariant()
    [pscustomobject]@{
        device_name = ([string]$drive.Drive).ToUpperInvariant()
        device_key = $key
        display_name = [string]$drive.Name
        connection_type = if ($enumerator -eq 'usbstor') { 'usb' } elseif ($enumerator -eq 'scsi') { 'sata' } else { 'unknown' }
    }
}
@($rows) | ConvertTo-Json -Compress
""".strip()


def _public_drive_name(value: object, connection_type: str) -> str:
    """Return a model-level name while removing likely serial-like tokens."""

    cleaned = re.sub(r"[^A-Za-z0-9 ._()&'\-]+", " ", str(value)).strip(" ._-")
    if not cleaned:
        return "Optical drive"
    if connection_type == "usb" and (
        cleaned.casefold().startswith("000000 ") or re.search(r"\b\d{12,}\b", cleaned)
    ):
        return "Generic USB optical device"
    tokens = [
        "[device-id]" if re.fullmatch(r"[A-Za-z0-9]{12,}", token) else token
        for token in cleaned.split()
    ]
    return " ".join(tokens)[:80] or "Optical drive"


def parse_windows_optical_devices(payload: str) -> tuple[NativeOpticalDevice, ...]:
    """Parse the path-redacted JSON returned by the fixed Windows query."""

    try:
        decoded = json.loads(payload or "[]")
    except json.JSONDecodeError as exc:
        raise DriveMappingError(
            "Windows optical-device metadata was malformed"
        ) from exc
    if isinstance(decoded, dict):
        decoded = [decoded]
    if not isinstance(decoded, list):
        raise DriveMappingError("Windows optical-device metadata was malformed")

    devices: list[NativeOpticalDevice] = []
    seen_names: set[str] = set()
    seen_keys: set[str] = set()
    seen_mapping_ids: set[str] = set()
    for item in decoded:
        if not isinstance(item, dict):
            continue
        device_name = str(item.get("device_name", "")).strip().upper().rstrip("\\/")
        device_key = str(item.get("device_key", "")).strip().casefold()
        connection_type = str(item.get("connection_type", "unknown")).casefold()
        if connection_type not in {"usb", "sata", "unknown"}:
            connection_type = "unknown"
        if not re.fullmatch(r"[A-Z]:", device_name):
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", device_key):
            continue
        if (
            device_name in seen_names
            or device_key in seen_keys
            or device_key[:16] in seen_mapping_ids
        ):
            raise DriveMappingError(
                "Windows reported an ambiguous optical-device identity"
            )
        seen_names.add(device_name)
        seen_keys.add(device_key)
        seen_mapping_ids.add(device_key[:16])
        devices.append(
            NativeOpticalDevice(
                device_name=device_name,
                device_key=device_key,
                display_name=_public_drive_name(
                    item.get("display_name", ""), connection_type
                ),
                connection_type=connection_type,
            )
        )
    return tuple(sorted(devices, key=lambda item: item.device_name))


def discover_windows_optical_devices(
    *, timeout_seconds: int = 30
) -> tuple[NativeOpticalDevice, ...]:
    """Return hashed Windows optical identities without reading disc media."""

    if sys.platform != "win32":
        return ()
    if timeout_seconds < 1 or timeout_seconds > 30:
        raise DriveMappingError("Windows optical-device timeout is invalid")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_OPTICAL_QUERY,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriveMappingError(
            "Windows optical-device discovery failed safely"
        ) from exc
    if completed.returncode != 0:
        raise DriveMappingError("Windows optical-device discovery failed safely")
    return parse_windows_optical_devices(completed.stdout)


class DriveMappingStore:
    """Persist only hashed device identities and explicit trust decisions."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def _validate_key(device_key: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", device_key):
            raise DriveMappingError("Optical-device identity is invalid")

    @staticmethod
    def _validate_descriptor(
        display_name: str | None, connection_type: str | None
    ) -> tuple[str | None, str | None]:
        public_name = (
            _public_drive_name(display_name, connection_type or "unknown")
            if display_name
            else None
        )
        public_connection = (
            connection_type
            if connection_type
            in {
                "usb",
                "sata",
                "unknown",
            }
            else None
        )
        return public_name, public_connection

    def _record_from_payload(
        self, value: object, *, schema_version: int
    ) -> DriveMappingRecord:
        if schema_version == 1:
            status = value
            display_name = None
            connection_type = None
        elif isinstance(value, dict):
            status = value.get("status")
            display_name = value.get("display_name")
            connection_type = value.get("connection_type")
        else:
            raise DriveMappingError("The private drive map is invalid")
        if not isinstance(status, str) or status not in {
            "trusted",
            "ignored",
            "retired",
        }:
            raise DriveMappingError(
                "The private drive map contains an invalid decision"
            )
        if display_name is not None and not isinstance(display_name, str):
            raise DriveMappingError("The private drive map is invalid")
        if connection_type is not None and connection_type not in {
            "usb",
            "sata",
            "unknown",
        }:
            raise DriveMappingError("The private drive map is invalid")
        public_name, public_connection = self._validate_descriptor(
            display_name, connection_type
        )
        return DriveMappingRecord(
            status=status,
            display_name=public_name,
            connection_type=public_connection,
        )

    def _load_unlocked(self) -> dict[str, DriveMappingRecord]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DriveMappingError(
                "The private drive map could not be loaded"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            1,
            2,
        }:
            raise DriveMappingError("The private drive map schema is invalid")
        schema_version = payload["schema_version"]
        decisions = payload.get("decisions" if schema_version == 1 else "devices", {})
        if not isinstance(decisions, dict):
            raise DriveMappingError("The private drive map is invalid")
        validated: dict[str, DriveMappingRecord] = {}
        for key, value in decisions.items():
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise DriveMappingError(
                    "The private drive map contains an invalid identity"
                )
            validated[key] = self._record_from_payload(
                value, schema_version=schema_version
            )
        return validated

    def _write_unlocked(self, records: dict[str, DriveMappingRecord]) -> None:
        payload = {
            "schema_version": 2,
            "devices": {
                key: {
                    "connection_type": record.connection_type,
                    "display_name": record.display_name,
                    "status": record.status,
                }
                for key, record in sorted(records.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise DriveMappingError("The private drive map could not be saved") from exc

    def status(self, device_key: str) -> str:
        """Return trusted, ignored, or unmapped for one hashed identity."""

        self._validate_key(device_key)
        with self._lock:
            record = self._load_unlocked().get(device_key)
            return (
                record.status
                if record is not None and record.status in {"trusted", "ignored"}
                else "unmapped"
            )

    def similar_prior_count(self, device: NativeOpticalDevice) -> int:
        """Count older trusted identities with the same safe USB/model descriptor."""

        self._validate_key(device.device_key)
        with self._lock:
            return sum(
                1
                for key, record in self._load_unlocked().items()
                if key != device.device_key
                and record.status in {"trusted", "retired"}
                and record.display_name == device.display_name
                and record.connection_type == device.connection_type
            )

    def set_status(
        self,
        device_key: str,
        status: str,
        *,
        display_name: str | None = None,
        connection_type: str | None = None,
    ) -> None:
        """Atomically save one explicit trusted/ignored decision."""

        self._validate_key(device_key)
        if status not in {"trusted", "ignored"}:
            raise DriveMappingError("Optical-device mapping decision is invalid")
        public_name, public_connection = self._validate_descriptor(
            display_name, connection_type
        )
        with self._lock:
            records = self._load_unlocked()
            existing = records.get(device_key)
            records[device_key] = DriveMappingRecord(
                status=status,
                display_name=public_name
                or (existing.display_name if existing else None),
                connection_type=(
                    public_connection
                    or (existing.connection_type if existing else None)
                ),
            )
            self._write_unlocked(records)

    def replace_current(
        self,
        devices: tuple[NativeOpticalDevice, ...],
        decisions: dict[str, str],
        *,
        retire_absent_trusted: bool = True,
    ) -> int:
        """Atomically replace decisions for one exact current-device snapshot."""

        current = {device.device_key: device for device in devices}
        if set(current) != set(decisions):
            raise DriveMappingError(
                "Every currently detected optical device must have one decision"
            )
        if any(status not in {"trusted", "ignored"} for status in decisions.values()):
            raise DriveMappingError("Optical-device mapping decision is invalid")
        with self._lock:
            records = self._load_unlocked()
            retired = 0
            if retire_absent_trusted:
                for key, record in tuple(records.items()):
                    if key not in current and record.status == "trusted":
                        records[key] = DriveMappingRecord(
                            status="retired",
                            display_name=record.display_name,
                            connection_type=record.connection_type,
                        )
                        retired += 1
            for key, device in current.items():
                records[key] = DriveMappingRecord(
                    status=decisions[key],
                    display_name=device.display_name,
                    connection_type=device.connection_type,
                )
            self._write_unlocked(records)
            return retired
