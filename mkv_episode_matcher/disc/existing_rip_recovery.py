"""Path-redacted recovery planning for previously ripped staging MKVs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median

from mkv_episode_matcher.disc.batch_output_validation import (
    is_complete_batch_output_size,
    is_inventory_planned_tiny_output,
)
from mkv_episode_matcher.disc.ripper import RipError, RipJob

_STAGING_BASENAME = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,81}--)?"
    r"disc-\d{2}-(?P<fingerprint>[0-9a-f]{16})-title-(?P<title>\d{3})\.mkv"
)
_SPECIAL_BASENAME = re.compile(
    r"special-(?P<token>[0-9a-f]{16})-title-(?P<title>\d{3})\.mkv"
)
_BATCH_BASENAME = re.compile(r"(?P<prefix>.+)_t(?P<ordinal>\d{2,3})\.mkv")
_SPECIAL_SIZE_TOLERANCE = 0.05
_BATCH_ESTIMATE_RATIO_MIN = 0.75
_BATCH_ESTIMATE_RATIO_MAX = 1.20
_BATCH_COHORT_RATIO_TOLERANCE = 0.12


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


def _failed_batch_cohorts(  # noqa: C901 - linear identity and prefix guards
    root: Path, jobs: tuple[RipJob, ...]
) -> tuple[dict[int, Path], ...]:
    """Recover the verified-size prefix of failed single-open batch attempts."""

    ordered = tuple(sorted(jobs, key=lambda item: item.title_index))
    identities = [
        _STAGING_BASENAME.fullmatch(job.output_basename or "") for job in ordered
    ]
    fingerprints = {
        match.group("fingerprint") for match in identities if match is not None
    }
    if (
        not ordered
        or any(match is None for match in identities)
        or len(fingerprints) != 1
        or any(
            job.estimated_bytes is None or job.estimated_bytes <= 0 for job in ordered
        )
    ):
        return ()
    fingerprint = next(iter(fingerprints))
    grouped: dict[tuple[Path, str], dict[int, Path]] = {}
    for path in root.glob(f".staging/disc-??/attempt-*/{fingerprint}/title-*/*.mkv"):
        if not path.is_file():
            continue
        match = _BATCH_BASENAME.fullmatch(path.name)
        if match is None:
            continue
        ordinal = int(match.group("ordinal"))
        parent_match = re.fullmatch(r"title-(\d{3})", path.parent.name)
        first_title_index = int(parent_match.group(1)) if parent_match else None
        largest_possible_ordinal = len(ordered) - 1
        if (
            first_title_index is not None
            and first_title_index != ordered[0].title_index
        ):
            largest_possible_ordinal = max(
                largest_possible_ordinal,
                max(job.title_index for job in ordered) - first_title_index,
            )
        if ordinal > largest_possible_ordinal:
            continue
        key = (path.parent.resolve(), match.group("prefix"))
        if ordinal in grouped.setdefault(key, {}):
            grouped[key].pop(ordinal, None)
            continue
        grouped[key][ordinal] = path.resolve()

    cohorts = []
    seen_cohorts: set[tuple[tuple[int, Path], ...]] = set()
    for (parent, _prefix), ordinal_paths in grouped.items():
        parent_match = re.fullmatch(r"title-(\d{3})", parent.name)
        first_title_index = int(parent_match.group(1)) if parent_match else None
        ordinal_maps = [
            {job.title_index: ordinal for ordinal, job in enumerate(ordered)}
        ]
        if (
            first_title_index is not None
            and first_title_index != ordered[0].title_index
            and all(job.title_index >= first_title_index for job in ordered)
        ):
            # Fresh whole-disc acquisition selects every title in index order.
            # A later failed-disc plan may contain only relevant titles, but
            # MakeMKV's preserved _tNN suffixes still use the original batch
            # ordinals. The isolated batch directory identifies the first
            # original title, allowing that narrowed plan to recover _t01,
            # _t03, etc. without treating its first relevant title as _t00.
            ordinal_maps = [
                {
                    job.title_index: job.title_index - first_title_index
                    for job in ordered
                }
            ]
        for ordinal_map in ordinal_maps:
            recovered: dict[int, Path] = {}
            accepted_ratios: list[float] = []
            for job in ordered:
                path = ordinal_paths.get(ordinal_map[job.title_index])
                if path is None:
                    # An inventory-predicted menu/control title is not required
                    # to recover later exact-ordinal outputs from the same
                    # failed batch. It remains missing and cannot masquerade as
                    # an episode, but it no longer hides subsequent content.
                    if is_inventory_planned_tiny_output(job.estimated_bytes):
                        continue
                    break
                actual_bytes = path.stat().st_size
                if is_inventory_planned_tiny_output(job.estimated_bytes):
                    if not is_complete_batch_output_size(
                        actual_bytes=actual_bytes,
                        estimated_bytes=job.estimated_bytes,
                    ):
                        break
                    recovered[job.title_index] = path
                    # Container overhead dominates tiny files, so their ratio
                    # must not distort the episode-sized cohort baseline.
                    continue
                ratio = actual_bytes / job.estimated_bytes
                if not _BATCH_ESTIMATE_RATIO_MIN <= ratio <= _BATCH_ESTIMATE_RATIO_MAX:
                    break
                if accepted_ratios:
                    baseline = median(accepted_ratios)
                    if abs(ratio - baseline) / baseline > _BATCH_COHORT_RATIO_TOLERANCE:
                        break
                recovered[job.title_index] = path
                accepted_ratios.append(ratio)
            identity = tuple(sorted(recovered.items()))
            if recovered and identity not in seen_cohorts:
                cohorts.append(recovered)
                seen_cohorts.add(identity)
    return tuple(cohorts)


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


def discover_existing_rips(  # noqa: C901 - ordered recovery precedence guards
    output_root: Path, jobs: tuple[RipJob, ...]
) -> ExistingRipRecoveryPlan:
    """Find exact collision-safe basenames without opening media content."""

    root = output_root.resolve()
    if not root.is_dir():
        raise RipError("Rip staging root is unavailable")
    candidates: list[ExistingRipCandidate] = []
    missing: list[int] = []
    ambiguous: list[int] = []
    batch_cohorts = _failed_batch_cohorts(root, jobs)
    special_cohorts: tuple[dict[int, Path], ...] | None = None
    for job in jobs:
        if not job.output_basename:
            missing.append(job.title_index)
            continue
        matches = [
            cohort[job.title_index]
            for cohort in batch_cohorts
            if job.title_index in cohort
        ]
        if not matches:
            matches = _normal_matches(root, job.output_basename)
        exact_matches = [path for path in matches if path.name == job.output_basename]
        if len(exact_matches) == 1:
            # The reviewed plan's complete basename includes its current ordinal.
            # Prefer that exact identity when older attempts with another
            # session-local ordinal also exist. Multiple exact copies in
            # different directories remain ambiguous.
            matches = exact_matches
        if not matches and special_cohorts is None:
            special_cohorts = _complete_special_cohorts(root, jobs)
        if not matches and len(special_cohorts or ()) == 1:
            assert special_cohorts is not None
            matches = [special_cohorts[0][job.title_index]]
        elif not matches and len(special_cohorts or ()) > 1:
            assert special_cohorts is not None
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
