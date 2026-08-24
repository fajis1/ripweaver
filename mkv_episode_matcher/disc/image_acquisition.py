"""Fail-closed whole-disc acquisition before any title-level MakeMKV work.

Planning is saved-data-only. Execution is a distinct, explicitly confirmed
boundary and performs exactly one physical acquisition call. The resulting
local image can then be inventoried and ripped without reopening the drive.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from mkv_episode_matcher.disc.makemkv_process_control import (
    MakeMKVJobSupervisor,
    get_makemkv_startup_control,
    run_makemkv_command,
)
from mkv_episode_matcher.disc.ripper import RipError

_RESERVE_BYTES = 1024**3


@dataclass(frozen=True)
class DiscImagePlan:
    """Path-redacted proposal for one whole-disc acquisition."""

    schema_version: int
    mode: str
    acquisition_id: str
    drive_index: int
    media_kind: Literal["bluray", "dvd"]
    image_format: Literal["makemkv-backup-folder", "dvd-iso"]
    relative_destination: str
    estimated_bytes: int
    physical_process_count: int
    execution_authorized: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerifiedLocalSource:
    """Private handoff produced only after structural backup verification."""

    acquisition_id: str
    source: Path
    source_specifier: str
    output_bytes: int


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def plan_disc_image(
    *, drive_index: int, media_kind: str, estimated_bytes: int
) -> DiscImagePlan:
    """Create an immutable proposal without accessing a drive or filesystem."""

    if not 0 <= drive_index <= 99:
        raise RipError("Disc-image drive index is invalid")
    if media_kind not in {"bluray", "dvd"}:
        raise RipError("Disc-image media kind is unsupported")
    if estimated_bytes <= 0:
        raise RipError("Disc-image estimated size must be positive")
    identity = {
        "schema_version": 1,
        "drive_index": drive_index,
        "media_kind": media_kind,
        "image_format": (
            "makemkv-backup-folder" if media_kind == "bluray" else "dvd-iso"
        ),
        "estimated_bytes": estimated_bytes,
    }
    acquisition_id = _canonical_digest(identity)[:24]
    return DiscImagePlan(
        schema_version=1,
        mode="whole-disc-image-plan",
        acquisition_id=acquisition_id,
        drive_index=drive_index,
        media_kind=media_kind,
        image_format=("makemkv-backup-folder" if media_kind == "bluray" else "dvd-iso"),
        relative_destination=(
            f"disc-images/{acquisition_id}"
            if media_kind == "bluray"
            else f"disc-images/{acquisition_id}.iso"
        ),
        estimated_bytes=estimated_bytes,
        physical_process_count=1,
        execution_authorized=False,
    )


def plan_sha256(plan: DiscImagePlan) -> str:
    return _canonical_digest(plan.to_dict())


def build_backup_command(
    executable: Path, plan: DiscImagePlan, destination: Path
) -> tuple[str, ...]:
    """Build the sole physical command; ``--noscan`` prevents all-drive probing."""

    return (
        str(executable),
        "-r",
        "--noscan",
        "--messages=-stdout",
        "--progress=-same",
        "backup",
        f"disc:{plan.drive_index}",
        str(destination),
    )


def build_dvd_image_command(
    executable: Path,
    *,
    drive_letter: str,
    destination: Path,
    drive_speed: int = 0,
) -> tuple[str, ...]:
    """Build DiscImageCreator's single DVD-dump command."""

    normalized = drive_letter.strip().upper().rstrip(":")
    if len(normalized) != 1 or not "A" <= normalized <= "Z":
        raise RipError("DVD acquisition drive letter is invalid")
    if not 0 <= drive_speed <= 16:
        raise RipError("DVD acquisition drive speed is invalid")
    return (
        str(executable),
        "dvd",
        normalized,
        str(destination),
        str(drive_speed),
    )


def local_info_command(
    executable: Path, source: VerifiedLocalSource
) -> tuple[str, ...]:
    """Build a later inventory command that cannot touch an optical drive."""

    return (
        str(executable),
        "-r",
        "--noscan",
        "--messages=-stdout",
        "info",
        source.source_specifier,
    )


def _contained_destination(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RipError("Disc-image destination is unsafe")
    resolved_root = root.resolve()
    destination = (resolved_root / relative_path).resolve()
    try:
        destination.relative_to(resolved_root)
    except ValueError as exc:
        raise RipError("Disc-image destination escaped its staging root") from exc
    return destination


def verify_bluray_backup(plan: DiscImagePlan, destination: Path) -> VerifiedLocalSource:
    """Verify the minimum structure needed for a local MakeMKV handoff."""

    index = destination / "BDMV" / "index.bdmv"
    if not index.is_file() or index.stat().st_size <= 0:
        raise RipError("Whole-disc backup is incomplete; BDMV index is missing")
    files = tuple(path for path in destination.rglob("*") if path.is_file())
    output_bytes = sum(path.stat().st_size for path in files)
    if output_bytes <= 0:
        raise RipError("Whole-disc backup contains no verified data")
    return VerifiedLocalSource(
        acquisition_id=plan.acquisition_id,
        source=destination,
        source_specifier=f"file:{destination}",
        output_bytes=output_bytes,
    )


def verify_dvd_iso(plan: DiscImagePlan, destination: Path) -> VerifiedLocalSource:
    """Verify a nonempty sector-aligned ISO before local MakeMKV handoff."""

    if not destination.is_file():
        raise RipError("Whole-disc DVD image is missing")
    output_bytes = destination.stat().st_size
    if output_bytes <= 0 or output_bytes % 2048 != 0:
        raise RipError("Whole-disc DVD image is empty or not sector aligned")
    return VerifiedLocalSource(
        acquisition_id=plan.acquisition_id,
        source=destination,
        source_specifier=f"iso:{destination}",
        output_bytes=output_bytes,
    )


CommandRunner = Callable[..., Any]


def run_contained_image_command(
    command: tuple[str, ...],
    *,
    timeout_seconds: int,
    supervisor_factory: Callable[[], MakeMKVJobSupervisor] = MakeMKVJobSupervisor,
) -> subprocess.CompletedProcess[str]:
    """Run a non-MakeMKV imager under a kill-on-close Windows Job Object."""

    # Use the same machine-wide lease as MakeMKV so a second RipWeaver backend
    # cannot start optical work concurrently. This does not broaden startup's
    # narrowly scoped stale-process cleanup.
    get_makemkv_startup_control().start()
    supervisor = supervisor_factory()
    process = supervisor.start(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    finally:
        supervisor.close()
    return subprocess.CompletedProcess(
        args=command,
        returncode=int(process.returncode),
        stdout=stdout,
        stderr=stderr,
    )


def _validate_execution_authority(
    plan: DiscImagePlan,
    *,
    authorized_plan_sha256: str,
    authorized_acquisition_count: int,
    confirm_acquisition: bool,
) -> None:
    if confirm_acquisition is not True:
        raise RipError("Explicit whole-disc acquisition confirmation is required")
    if not hmac.compare_digest(authorized_plan_sha256, plan_sha256(plan)):
        raise RipError("Authorized disc-image plan digest does not match")
    if authorized_acquisition_count != 1 or plan.physical_process_count != 1:
        raise RipError("Authorization must cover exactly one physical acquisition")
    if plan.execution_authorized is not False:
        raise RipError("Disc-image plan authority flag is invalid")


def execute_disc_image(
    plan: DiscImagePlan,
    *,
    executable: Path,
    image_root: Path,
    authorized_plan_sha256: str,
    authorized_acquisition_count: int,
    confirm_acquisition: bool,
    timeout_seconds: int = 14400,
    drive_letter: str | None = None,
    command_runner: CommandRunner | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> VerifiedLocalSource:
    """Run exactly one whole-disc acquisition and preserve any failed partial."""

    _validate_execution_authority(
        plan,
        authorized_plan_sha256=authorized_plan_sha256,
        authorized_acquisition_count=authorized_acquisition_count,
        confirm_acquisition=confirm_acquisition,
    )
    if not executable.is_file():
        raise RipError("Disc-image acquisition executable was not found")
    if not image_root.is_dir():
        raise RipError("Disc-image staging root does not exist")
    if timeout_seconds < 60:
        raise RipError("Disc-image timeout must be at least 60 seconds")

    destination = _contained_destination(image_root, plan.relative_destination)
    if destination.exists():
        raise RipError("Disc-image destination collision; nothing was started")
    if disk_usage(image_root).free < plan.estimated_bytes + _RESERVE_BYTES:
        raise RipError("Insufficient conservative free space for disc image")

    runner = command_runner or (
        run_makemkv_command
        if plan.media_kind == "bluray"
        else run_contained_image_command
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = (
        build_backup_command(executable, plan, destination)
        if plan.media_kind == "bluray"
        else build_dvd_image_command(
            executable,
            drive_letter=drive_letter or "",
            destination=destination,
        )
    )
    result = runner(command, timeout_seconds=timeout_seconds)
    if int(result.returncode) != 0:
        raise RipError("Whole-disc acquisition failed; partial output was preserved")
    if plan.media_kind == "bluray":
        return verify_bluray_backup(plan, destination)
    return verify_dvd_iso(plan, destination)
