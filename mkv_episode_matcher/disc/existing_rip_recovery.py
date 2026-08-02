"""Path-redacted recovery planning for previously ripped staging MKVs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from mkv_episode_matcher.disc.ripper import RipError, RipJob

_STAGING_BASENAME = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,81}--)?"
    r"disc-\d{2}-(?P<fingerprint>[0-9a-f]{16})-title-(?P<title>\d{3})\.mkv"
)
_SPECIAL_BASENAME = re.compile(
    r"special-(?P<token>[0-9a-f]{16})-title-(?P<title>\d{3})\.mkv"
)
_SPECIAL_SIZE_TOLERANCE = 0.05


def _matches_inventory_size(actual: int, estimated: int) -> bool:
    """Allow MakeMKV's bounded inventory-estimate versus final-size variance."""

    if actual <= 0 or estimated <= 0:
        return False
    return abs(actual - estimated) <= max(
        1024 * 1024, int(estimated * _SPECIAL_SIZE_TOLERANCE)
    )


@dataclass(frozen=True)
class ExistingRipCandidate:
    job_id: str
    title_index: int
    basename: str
    size_bytes: int
    relative_parent: str

    @property
    def candidate_id(self) -> str:
        """Return a path-redacted identity binding one exact staged file."""
        identity = {
            "job_id": self.job_id,
            "title_index": self.title_index,
            "basename": self.basename,
            "size_bytes": self.size_bytes,
            "relative_parent": self.relative_parent,
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class ExistingRipRecoveryPlan:
    candidates: tuple[ExistingRipCandidate, ...]
    missing_title_indexes: tuple[int, ...]
    ambiguous_title_indexes: tuple[int, ...]
    plan_sha256: str

    def public_dict(self) -> dict[str, object]:
        return {
            "plan_sha256": self.plan_sha256,
            "candidates": [
                {
                    "job_id": item.job_id,
                    "title_index": item.title_index,
                    "basename": item.basename,
                    "size_bytes": item.size_bytes,
                    "candidate_id": item.candidate_id,
                }
                for item in self.candidates
            ],
            "missing_title_indexes": list(self.missing_title_indexes),
            "ambiguous_title_indexes": list(self.ambiguous_title_indexes),
            "execution_authorized": False,
        }


def _complete_special_cohorts(
    root: Path, jobs: tuple[RipJob, ...]
) -> tuple[dict[int, Path], ...]:
    """Return complete, size-bound legacy special-feature output cohorts."""

    expected = {job.title_index: job.estimated_bytes for job in jobs}
    grouped: dict[str, dict[int, list[Path]]] = {}
    for path in root.rglob("special-*-title-???.mkv"):
        if not path.is_file():
            continue
        match = _SPECIAL_BASENAME.fullmatch(path.name)
        if match is None:
            continue
        title_index = int(match.group("title"))
        if title_index not in expected or not _matches_inventory_size(
            path.stat().st_size, expected[title_index]
        ):
            continue
        grouped.setdefault(match.group("token"), {}).setdefault(title_index, []).append(
            path.resolve()
        )
    complete = []
    for title_paths in grouped.values():
        if set(title_paths) != set(expected):
            continue
        if any(len(paths) != 1 for paths in title_paths.values()):
            continue
        complete.append({index: paths[0] for index, paths in title_paths.items()})
    return tuple(complete)


def _normal_matches(root: Path, basename: str) -> list[Path]:
    identity = _STAGING_BASENAME.fullmatch(basename)
    if identity is None:
        search_names = (basename,)
    else:
        # The ordinal disc ID is session-local. An older attempt can have a
        # different ordinal while retaining the durable inventory fingerprint
        # and MakeMKV title index.
        search_names = (
            f"disc-??-{identity.group('fingerprint')}-"
            f"title-{identity.group('title')}.mkv",
            f"*--disc-??-{identity.group('fingerprint')}-"
            f"title-{identity.group('title')}.mkv",
        )
    return sorted({
        path.resolve()
        for search_name in search_names
        for path in root.rglob(search_name)
        if path.is_file()
    })


def _candidate_records(
    root: Path, job: RipJob, matches: list[Path]
) -> list[ExistingRipCandidate]:
    records = []
    for path in matches:
        path = path.resolve()
        try:
            parent = path.parent.relative_to(root).as_posix()
        except ValueError as exc:
            raise RipError("Existing rip candidate escapes the staging root") from exc
        size = path.stat().st_size
        if size > 0:
            records.append(
                ExistingRipCandidate(
                    job_id=job.job_id,
                    title_index=job.title_index,
                    basename=path.name,
                    size_bytes=size,
                    relative_parent=parent,
                )
            )
    return records


def discover_existing_rips(
    output_root: Path, jobs: tuple[RipJob, ...]
) -> ExistingRipRecoveryPlan:
    """Find exact collision-safe basenames without opening media content."""

    root = output_root.resolve()
    if not root.is_dir():
        raise RipError("Rip staging root is unavailable")
    candidates: list[ExistingRipCandidate] = []
    missing: list[int] = []
    ambiguous: list[int] = []
    special_cohorts = _complete_special_cohorts(root, jobs)
    for job in jobs:
        if not job.output_basename:
            missing.append(job.title_index)
            continue
        matches = _normal_matches(root, job.output_basename)
        exact_matches = [path for path in matches if path.name == job.output_basename]
        if len(exact_matches) == 1:
            # The reviewed plan's complete basename includes its current ordinal.
            # Prefer that exact identity when older attempts with another
            # session-local ordinal also exist. Multiple exact copies in
            # different directories remain ambiguous.
            matches = exact_matches
        if not matches and len(special_cohorts) == 1:
            matches = [special_cohorts[0][job.title_index]]
        elif not matches and len(special_cohorts) > 1:
            matches = [cohort[job.title_index] for cohort in special_cohorts]
        if not matches:
            missing.append(job.title_index)
            continue
        if len(matches) != 1:
            ambiguous.append(job.title_index)
        records = _candidate_records(root, job, matches)
        candidates.extend(records)
        if not records:
            missing.append(job.title_index)
    identity = [
        {
            "job_id": item.job_id,
            "title_index": item.title_index,
            "basename": item.basename,
            "size_bytes": item.size_bytes,
            "relative_parent": item.relative_parent,
        }
        for item in candidates
    ]
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExistingRipRecoveryPlan(
        candidates=tuple(candidates),
        missing_title_indexes=tuple(sorted(missing)),
        ambiguous_title_indexes=tuple(sorted(ambiguous)),
        plan_sha256=digest,
    )


def recovered_jobs(
    jobs: tuple[RipJob, ...], plan: ExistingRipRecoveryPlan
) -> tuple[RipJob, ...]:
    """Bind candidates to their existing parent directories for queue admission."""

    by_id = {item.job_id: item for item in plan.candidates}
    return tuple(
        replace(
            job,
            relative_output_dir=by_id[job.job_id].relative_parent,
            output_basename=by_id[job.job_id].basename,
            final_relative_dir=None,
        )
        for job in jobs
        if job.job_id in by_id
    )
