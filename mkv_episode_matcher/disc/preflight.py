"""Read-only MakeMKV drive discovery and disc inventory.

This module intentionally supports MakeMKV's ``info`` action only. It does not
contain ripping, ejection, renaming, moving, or deletion operations.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.disc.makemkv_process_control import run_makemkv_command

DEFAULT_MAKEMKV_PATH = Path(r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe")
SAFE_ACTION = "info"
DISC_SOURCE_PATTERN = re.compile(r"^disc:(\d+)$")


class PreflightError(RuntimeError):
    """Raised when a read-only preflight operation cannot proceed safely."""


@dataclass(frozen=True)
class MakeMKVDrive:
    """One optical drive reported by MakeMKV robot mode."""

    index: int
    visible: int
    enabled: int
    flags: int
    drive_name: str
    disc_name: str
    device_name: str

    @property
    def has_disc(self) -> bool:
        return bool(self.disc_name.strip())


@dataclass
class MakeMKVStream:
    """Generic stream metadata, retained by MakeMKV metadata code."""

    stream_id: int
    attributes: dict[int, str] = field(default_factory=dict)

    @property
    def stream_type(self) -> str | None:
        return self.attributes.get(1)

    @property
    def name(self) -> str | None:
        return self.attributes.get(2)

    @property
    def language(self) -> str | None:
        return self.attributes.get(3)

    @property
    def codec(self) -> str | None:
        return self.attributes.get(6)

    @property
    def channels(self) -> int | None:
        value = self.attributes.get(14)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @property
    def channel_layout(self) -> str | None:
        return self.attributes.get(40)

    @property
    def is_default(self) -> bool:
        return "d" in self.attributes.get(38, "")


@dataclass
class MakeMKVTitle:
    """Generic title metadata, retained by MakeMKV metadata code."""

    index: int
    attributes: dict[int, str] = field(default_factory=dict)
    streams: dict[int, MakeMKVStream] = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        return self.attributes.get(2)

    @property
    def duration(self) -> str | None:
        return self.attributes.get(9)

    @property
    def chapters(self) -> str | None:
        return self.attributes.get(8)

    @property
    def source_file(self) -> str | None:
        """Return MakeMKV's source playlist or stream filename (TINFO 16)."""

        return self.attributes.get(16)

    @property
    def segment_map(self) -> str | None:
        """Return MakeMKV's ordered Blu-ray segment map (TINFO 26)."""

        return self.attributes.get(26)

    @property
    def output_name(self) -> str | None:
        return (
            self.attributes.get(27)
            or self.attributes.get(16)
            or self.attributes.get(32)
        )


@dataclass
class DiscInventory:
    """Parsed, non-mutating inventory for one loaded disc."""

    drive: MakeMKVDrive
    disc_attributes: dict[int, str]
    titles: list[MakeMKVTitle]
    return_code: int
    started_at: str
    finished_at: str
    warnings: list[str]


@dataclass(frozen=True)
class CommandResult:
    """Captured output from one MakeMKV info command."""

    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str


def resolve_makemkv_path(explicit_path: Path | None = None) -> Path:
    """Resolve MakeMKV from a CLI option, environment, or Windows default."""

    candidate = explicit_path
    if candidate is None:
        environment_path = load_environment_settings().makemkv_path
        candidate = environment_path or DEFAULT_MAKEMKV_PATH

    candidate = candidate.expanduser()
    if not candidate.is_file():
        raise PreflightError(f"MakeMKV executable not found: {candidate}")
    return candidate


def build_info_command(
    executable: Path, source: str, minimum_length: int | None = None
) -> tuple[str, ...]:
    """Build and validate a MakeMKV command that can only inspect a disc."""

    if DISC_SOURCE_PATTERN.fullmatch(source) is None:
        raise PreflightError(f"Unsafe or invalid MakeMKV source: {source!r}")

    command = [str(executable), "-r"]
    if source == "disc:9999":
        # MakeMKV documents this exact cache-backed command shape for listing
        # available drive slots. It avoids turning dashboard discovery into a
        # full inventory of every inserted disc.
        command.append("--cache=1")
    else:
        command.extend(("--messages=-stdout", "--progress=-same"))
        # A targeted inventory must not perform MakeMKV's normal media scan
        # against every other optical drive before opening the selected one.
        command.append("--noscan")
    if minimum_length is not None:
        if minimum_length < 0:
            raise PreflightError("Minimum title length cannot be negative")
        command.append(f"--minlength={minimum_length}")
    command.extend((SAFE_ACTION, source))

    if "mkv" in command or "backup" in command:
        raise PreflightError("Preflight commands may not rip or back up a disc")
    return tuple(command)


def run_info_command(
    executable: Path,
    source: str,
    *,
    minimum_length: int | None = None,
    timeout_seconds: int = 300,
) -> CommandResult:
    """Run one validated MakeMKV info command and capture its robot output."""

    command = build_info_command(executable, source, minimum_length)
    started = datetime.now(UTC)
    try:
        completed = run_makemkv_command(
            command,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreflightError(
            f"MakeMKV info timed out after {timeout_seconds}s for {source}"
        ) from exc
    except OSError as exc:
        raise PreflightError(
            f"MakeMKV info could not be started: {type(exc).__name__}"
        ) from exc

    finished = datetime.now(UTC)
    return CommandResult(
        command=command,
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
    )


def _robot_row(line: str) -> list[str] | None:
    if ":" not in line:
        return None
    try:
        return next(csv.reader([line]))
    except (csv.Error, StopIteration):
        return None


def parse_drives(robot_output: str) -> list[MakeMKVDrive]:
    """Parse all ``DRV`` records from MakeMKV robot output."""

    drives: list[MakeMKVDrive] = []
    for line in robot_output.splitlines():
        if not line.startswith("DRV:"):
            continue
        row = _robot_row(line)
        if row is None or len(row) < 7:
            continue
        try:
            drives.append(
                MakeMKVDrive(
                    index=int(row[0].split(":", 1)[1]),
                    visible=int(row[1]),
                    enabled=int(row[2]),
                    flags=int(row[3]),
                    drive_name=row[4],
                    disc_name=row[5],
                    device_name=row[6],
                )
            )
        except (IndexError, ValueError):
            continue
    return drives


def targeted_inventory_drive(
    robot_output: str,
    *,
    requested_index: int,
    cached_device_name: str | None,
    cached_drive_name: str | None,
    cached_disc_name: str | None,
) -> MakeMKVDrive | None:
    """Bind valid targeted inventory to its already trusted private drive slot.

    With noscan, MakeMKV may omit the global drive row or report the opened
    drive under a local ordinal even while returning its title rows. The
    command and process-control claim already bind the read to one exact disc
    source, so valid title metadata may reuse the cached private device
    identity. Empty output is still treated as a disappeared disc.
    """

    drives = parse_drives(robot_output)
    exact = next((drive for drive in drives if drive.index == requested_index), None)
    if exact is not None and exact.has_disc:
        return exact
    if not any(line.startswith("TINFO:") for line in robot_output.splitlines()):
        return None
    if not cached_device_name or not (cached_disc_name or "").strip():
        return None

    normalized_device = cached_device_name.strip().casefold().rstrip("\\/")
    matching_device = next(
        (
            drive
            for drive in drives
            if drive.device_name.strip().casefold().rstrip("\\/") == normalized_device
        ),
        None,
    )
    template = exact or matching_device
    if template is not None:
        return replace(
            template,
            index=requested_index,
            disc_name=template.disc_name or str(cached_disc_name),
        )
    return MakeMKVDrive(
        index=requested_index,
        visible=1,
        enabled=1,
        flags=0,
        drive_name=cached_drive_name or "",
        disc_name=str(cached_disc_name),
        device_name=cached_device_name,
    )


def parse_disc_inventory(result: CommandResult, drive: MakeMKVDrive) -> DiscInventory:
    """Parse CINFO, TINFO, and SINFO records for one disc."""

    disc_attributes: dict[int, str] = {}
    titles: dict[int, MakeMKVTitle] = {}
    warnings: list[str] = []

    for line in result.stdout.splitlines():
        row = _robot_row(line)
        if row is None:
            continue

        try:
            if row[0].startswith("CINFO:") and len(row) >= 3:
                code = int(row[0].split(":", 1)[1])
                disc_attributes[code] = row[2]
            elif row[0].startswith("TINFO:") and len(row) >= 4:
                title_index = int(row[0].split(":", 1)[1])
                code = int(row[1])
                title = titles.setdefault(title_index, MakeMKVTitle(index=title_index))
                title.attributes[code] = row[3]
            elif row[0].startswith("SINFO:") and len(row) >= 5:
                title_index = int(row[0].split(":", 1)[1])
                stream_id = int(row[1])
                code = int(row[2])
                title = titles.setdefault(title_index, MakeMKVTitle(index=title_index))
                stream = title.streams.setdefault(
                    stream_id, MakeMKVStream(stream_id=stream_id)
                )
                stream.attributes[code] = row[4]
            elif row[0].startswith("MSG:") and len(row) >= 4:
                message = row[3]
                lowered = message.lower()
                if any(
                    word in lowered for word in ("error", "fail", "skip", "corrupt")
                ):
                    warnings.append(message)
        except (IndexError, ValueError):
            continue

    return DiscInventory(
        drive=drive,
        disc_attributes=disc_attributes,
        titles=[titles[index] for index in sorted(titles)],
        return_code=result.return_code,
        started_at=result.started_at,
        finished_at=result.finished_at,
        warnings=warnings,
    )


def inventory_to_dict(
    inventory: DiscInventory,
    *,
    minimum_length_seconds: int = 0,
) -> dict[str, Any]:
    """Convert an inventory to JSON without persisting hardware identity."""

    if minimum_length_seconds < 0:
        raise PreflightError("Minimum title length cannot be negative")
    payload = asdict(inventory)
    payload["drive"]["drive_name"] = "<hardware-redacted>"
    payload["drive"]["device_name"] = "<device-redacted>"
    payload["minimum_length_seconds"] = minimum_length_seconds
    return payload


def sanitize_robot_output(robot_output: str) -> str:
    """Redact optical-drive identity from persisted robot output."""

    sanitized: list[str] = []
    for line in robot_output.splitlines():
        if not line.startswith("DRV:"):
            sanitized.append(line)
            continue
        row = _robot_row(line)
        if row is None or len(row) < 7:
            sanitized.append("DRV:<malformed-hardware-redacted>")
            continue
        row[4] = "<hardware-redacted>"
        row[6] = "<device-redacted>"
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(row)
        sanitized.append(buffer.getvalue())
    return "\n".join(sanitized) + ("\n" if robot_output.endswith("\n") else "")


def safe_report_stem(drive: MakeMKVDrive) -> str:
    """Create a Windows-safe report name without trusting the volume label."""

    label = re.sub(r"[^A-Za-z0-9._-]+", "_", drive.disc_name).strip("._")
    label = label[:80] or "unknown_disc"
    return f"drive_{drive.index}_{label}"


def write_inventory_report(
    output_dir: Path,
    inventory: DiscInventory,
    result: CommandResult,
    *,
    minimum_length_seconds: int = 0,
) -> tuple[Path, Path]:
    """Write parsed JSON and raw robot output for later parser regression tests."""

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_report_stem(inventory.drive)
    json_path = output_dir / f"{stem}.json"
    robot_path = output_dir / f"{stem}.robot.log"

    json_path.write_text(
        json.dumps(
            inventory_to_dict(
                inventory,
                minimum_length_seconds=minimum_length_seconds,
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    robot_path.write_text(
        sanitize_robot_output(result.stdout)
        + (
            "\n--- STDERR ---\n" + sanitize_robot_output(result.stderr)
            if result.stderr
            else ""
        ),
        encoding="utf-8",
    )
    return json_path, robot_path
