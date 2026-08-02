"""Exact, reviewable cleanup of isolated incomplete MakeMKV attempts."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from mkv_episode_matcher.disc.ripper import RipError, RipJob, resolve_final_output

_BASENAME = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,81}--)?"
    r"disc-\d{2}-(?P<fingerprint>[0-9a-f]{16})-title-(?P<title>\d{3})\.mkv"
)


@dataclass(frozen=True)
class FailedRipCleanupPlan:
    relative_directories: tuple[str, ...]
    file_count: int
    total_bytes: int
    plan_sha256: str

    def public_dict(self) -> dict[str, object]:
        return {
            "attempt_directory_count": len(self.relative_directories),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "plan_sha256": self.plan_sha256,
            "execution_authorized": False,
        }


def plan_failed_rip_cleanup(  # noqa: C901
    output_root: Path, jobs: tuple[RipJob, ...]
) -> FailedRipCleanupPlan:
    """Find incomplete isolated attempts for the exact disc/title identities."""

    root = output_root.resolve()
    staging = (root / ".staging").resolve()
    if not root.is_dir():
        raise RipError("Rip staging root is unavailable")
    candidates: set[Path] = set()
    for job in jobs:
        match = _BASENAME.fullmatch(job.output_basename or "")
        if match is None:
            continue
        final_output = resolve_final_output(root, job)
        if final_output is not None and final_output.exists():
            continue
        fingerprint = match.group("fingerprint")
        title = match.group("title")
        for candidate in staging.glob(f"disc-*/**/{fingerprint}/title-{title}"):
            resolved = candidate.resolve()
            try:
                resolved.relative_to(staging)
            except ValueError as exc:
                raise RipError("Failed-attempt directory escapes staging") from exc
            if candidate.is_symlink() or not resolved.is_dir():
                raise RipError("Failed-attempt candidate is not a regular directory")
            entries = tuple(resolved.iterdir())
            if any(
                entry.is_symlink()
                or not entry.is_file()
                or entry.suffix.casefold() != ".mkv"
                for entry in entries
            ):
                raise RipError("Failed-attempt directory contains unknown output")
            if entries:
                candidates.add(resolved)

    records = []
    file_count = 0
    total_bytes = 0
    for directory in sorted(candidates):
        files = sorted(directory.iterdir())
        metadata = [
            {"size": item.stat().st_size, "modified_ns": item.stat().st_mtime_ns}
            for item in files
        ]
        relative = directory.relative_to(root).as_posix()
        records.append({"relative_directory": relative, "files": metadata})
        file_count += len(files)
        total_bytes += sum(item["size"] for item in metadata)
    digest = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FailedRipCleanupPlan(
        relative_directories=tuple(item["relative_directory"] for item in records),
        file_count=file_count,
        total_bytes=total_bytes,
        plan_sha256=digest,
    )


def apply_failed_rip_cleanup(
    output_root: Path,
    jobs: tuple[RipJob, ...],
    *,
    expected_plan_sha256: str,
) -> FailedRipCleanupPlan:
    """Delete only the unchanged, exact incomplete-attempt plan."""

    plan = plan_failed_rip_cleanup(output_root, jobs)
    if plan.plan_sha256 != expected_plan_sha256:
        raise RipError("Failed-attempt cleanup changed; review it again")
    root = output_root.resolve()
    for relative in plan.relative_directories:
        shutil.rmtree(root / relative)
    return plan
