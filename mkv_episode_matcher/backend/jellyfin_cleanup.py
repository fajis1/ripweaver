"""Plan and apply collision-refusing cleanup of verified staging media."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal


class CleanupError(RuntimeError):
    """Raised when a cleanup plan is invalid or changed."""


@dataclass(frozen=True)
class CleanupCandidate:
    category: Literal["rip", "encoded", "cleanup"]
    relative_path: str
    library_relative: str
    size_bytes: int
    modified_at: str
    backed_up: bool
    disk_key: str
    disk_label: str


@dataclass(frozen=True)
class CleanupPlan:
    plan_sha256: str
    mode: str
    cutoff: str | None
    candidates: tuple[CleanupCandidate, ...]

    @property
    def total_size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.candidates)


def filter_plan(plan: CleanupPlan, candidates: tuple[CleanupCandidate, ...]) -> CleanupPlan:
    """Return a digest-consistent plan after cross-library de-duplication."""

    records = [
        {
            "category": item.category,
            "relative_path": item.relative_path,
            "library_relative": item.library_relative,
            "size_bytes": item.size_bytes,
            "modified_at": item.modified_at,
            "backed_up": item.backed_up,
            "disk_key": item.disk_key,
            "disk_label": item.disk_label,
        }
        for item in candidates
    ]
    return CleanupPlan(
        plan_sha256=_digest(records, plan.mode, plan.cutoff),
        mode=plan.mode,
        cutoff=plan.cutoff,
        candidates=candidates,
    )


def _file_record(category: str, path: Path, root: Path, library_relative: str):
    try:
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    disk_key, disk_label = _disk_metadata(path)
    return {
        "category": category,
        "relative_path": relative,
        "library_relative": library_relative,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "backed_up": bool(library_relative),
        "disk_key": disk_key,
        "disk_label": disk_label,
    }


def _disk_metadata(path: Path) -> tuple[str, str]:
    text = path.as_posix()
    match = re.search(
        r"(?i)(?P<label>[a-z0-9][a-z0-9 ._-]*?)--?disc-(?P<ordinal>\d+)-(?P<fingerprint>[0-9a-f]{16})",
        text,
    )
    if match is None:
        match = re.search(
            r"(?i)(?:^|/)disc-(?P<ordinal>\d+)[^/]*?/(?P<fingerprint>[0-9a-f]{16})(?:/|$)",
            text,
        )
    if match is None:
        match = re.search(
            r"(?i)disc-(?P<ordinal>\d+)-(?P<fingerprint>[0-9a-f]{16})",
            text,
        )
    if match is None:
        return "unassigned", "Unassigned staging"
    ordinal = int(match.group("ordinal"))
    fingerprint = match.group("fingerprint").lower()
    label = (match.groupdict().get("label") or "").replace("-", " ").strip()
    if label:
        label = re.sub(r"\s+\d+$", "", label).strip()
    path_series = _series_from_path(path)
    series = path_series or label
    display = f"{series} · Disc {ordinal:02d}" if series else f"Disc {ordinal:02d}"
    return f"disc-{ordinal:02d}-{fingerprint}", display


def _series_from_path(path: Path) -> str | None:
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if part.casefold() != "tv shows":
            continue
        candidate = parts[index + 1].strip()
        if candidate and candidate.casefold() not in {"unmatched", "unknown"}:
            return candidate
    for index, part in enumerate(parts):
        if part.casefold() != "unmatched" or index == 0:
            continue
        candidate = parts[index - 1].strip()
        if candidate and candidate.casefold() not in {"tv shows", "season 00"}:
            return candidate
    return None


def _safe_library_relative(value: object) -> str | None:
    if not isinstance(value, str) or not value.lower().endswith(".mkv"):
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return relative.as_posix()


def _encoded_candidates(root: Path, library_root: Path) -> list[dict]:
    results = []
    staging_prefix = Path("encoded-staging")
    try:
        files = root.rglob("*.mkv")
    except OSError:
        return results
    for path in files:
        if not path.is_file() or path.name.lower().endswith(".partial.mkv"):
            continue
        try:
            relative = path.relative_to(root)
            if relative.parts[:1] != staging_prefix.parts:
                continue
            library_relative = _safe_library_relative(
                Path(*relative.parts[1:]).as_posix()
            )
            if library_relative is None or not (library_root / Path(library_relative)).is_file():
                continue
        except (OSError, ValueError):
            continue
        record = _file_record("encoded", path, root, library_relative)
        if record is not None:
            results.append(record)
    return results


def _all_staging_candidates(
    root: Path,
    category: str,
    *,
    excluded_roots: tuple[Path, ...] = (),
    seen_paths: set[Path] | None = None,
) -> list[dict]:
    results = []
    try:
        files = root.rglob("*.mkv")
    except OSError:
        return results
    for path in files:
        if not path.is_file() or path.name.lower().endswith(".partial.mkv"):
            continue
        relative_parts = {part.casefold() for part in path.relative_to(root).parts}
        if any(
            part.startswith(".pytest-")
            or part in {".pytest_cache", ".mkv-staging-canary", "encoded-staging-samples"}
            for part in relative_parts
        ):
            continue
        try:
            resolved = path.resolve()
            if any(
                resolved == excluded
                or resolved.is_relative_to(excluded)
                for excluded in excluded_roots
            ):
                continue
        except OSError:
            continue
        if seen_paths is not None:
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
        record = _file_record(category, path, root, "")
        if record is not None:
            results.append(record)
    return results


def _contract_candidates(  # noqa: C901
    rip_root: Path, contract_root: Path, library_root: Path
) -> list[dict]:
    results = []
    if not contract_root.is_dir():
        return results
    try:
        contracts = contract_root.rglob("*.json")
    except OSError:
        return results
    for contract_path in contracts:
        try:
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("mode") != "verified-transcode-contract":
            continue
        source_value = payload.get("original_source_path")
        encoded_value = payload.get("encoded_path")
        library_relative = _safe_library_relative(payload.get("library_relative"))
        if not isinstance(source_value, str) or not isinstance(encoded_value, str):
            continue
        if library_relative is None:
            continue
        source = Path(source_value)
        encoded = Path(encoded_value)
        try:
            source.relative_to(rip_root)
            source = source.resolve()
            encoded = encoded.resolve()
            if not source.is_file() or not encoded.is_file():
                continue
            if not (library_root / Path(library_relative)).is_file():
                continue
        except (OSError, ValueError):
            continue
        record = _file_record("rip", source, rip_root, library_relative)
        if record is not None:
            results.append(record)
    return results


def _digest(records: list[dict], mode: str, cutoff: str | None) -> str:
    serialized = json.dumps(
        {"mode": mode, "cutoff": cutoff, "candidates": records},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def plan_cleanup(  # noqa: C901
    *,
    rip_root: Path,
    encoded_root: Path,
    contract_root: Path,
    library_root: Path | None,
    mode: Literal["older_than", "all", "all_staging"],
    cleanup_root: Path | None = None,
    protected_roots: tuple[Path, ...] = (),
    days: int | None = None,
    now: datetime | None = None,
    cutoff: datetime | None = None,
) -> CleanupPlan:
    """Find staging files with an exact verified counterpart in Jellyfin."""

    rip_root = rip_root.resolve()
    encoded_root = encoded_root.resolve()
    library_root = library_root.resolve() if library_root is not None else None
    if not rip_root.is_dir() or not encoded_root.is_dir():
        raise CleanupError("Configured cleanup roots must already exist")
    if mode != "all_staging" and (library_root is None or not library_root.is_dir()):
        raise CleanupError("Configured Jellyfin library root must already exist")
    if mode == "older_than" and days not in {7, 14}:
        raise CleanupError("Cleanup age must be 7 or 14 days")
    if mode in {"all", "all_staging"}:
        cutoff_value = None
    else:
        current = now or datetime.now(UTC)
        cutoff_value = (cutoff or (current - timedelta(days=days))).isoformat()
    if mode == "all_staging":
        seen_paths: set[Path] = set()
        records = _all_staging_candidates(
            rip_root, "rip", excluded_roots=protected_roots, seen_paths=seen_paths
        ) + _all_staging_candidates(
            encoded_root,
            "encoded",
            excluded_roots=protected_roots,
            seen_paths=seen_paths,
        )
        if cleanup_root is not None and cleanup_root.is_dir():
            records += _all_staging_candidates(
                cleanup_root.resolve(),
                "cleanup",
                excluded_roots=protected_roots,
                seen_paths=seen_paths,
            )
        verified_by_key: dict[tuple[str, str], str] = {}
        for protected in protected_roots:
            if not protected.is_dir():
                continue
            for verified in _encoded_candidates(encoded_root, protected) + _contract_candidates(
                rip_root, contract_root.resolve(), protected
            ):
                verified_by_key[(verified["category"], verified["relative_path"])] = verified[
                    "library_relative"
                ]
        for record in records:
            library_relative = verified_by_key.get(
                (record["category"], record["relative_path"])
            )
            if library_relative:
                record["backed_up"] = True
                record["library_relative"] = library_relative
    else:
        records = _encoded_candidates(encoded_root, library_root) + _contract_candidates(
            rip_root, contract_root.resolve(), library_root
        )
    records.sort(key=lambda item: (item["category"], item["relative_path"].casefold()))
    if cutoff_value is not None:
        cutoff_time = datetime.fromisoformat(cutoff_value)
        records = [
            item
            for item in records
            if datetime.fromisoformat(item["modified_at"]) <= cutoff_time
        ]
    return CleanupPlan(
        plan_sha256=_digest(records, mode, cutoff_value),
        mode=mode,
        cutoff=cutoff_value,
        candidates=tuple(CleanupCandidate(**item) for item in records),
    )


def apply_cleanup(  # noqa: C901
    *,
    plan: CleanupPlan,
    rip_root: Path,
    encoded_root: Path,
    contract_root: Path,
    library_root: Path | None,
    cleanup_root: Path | None = None,
    protected_roots: tuple[Path, ...] = (),
    candidate_keys: tuple[str, ...] | None = None,
    expected_plan_sha256: str,
    authorized_file_count: int,
) -> int:
    """Delete exactly one unchanged, digest-bound cleanup plan."""

    if plan.plan_sha256 != expected_plan_sha256:
        raise CleanupError("Cleanup plan changed; review a fresh scan")
    if authorized_file_count != len(plan.candidates):
        raise CleanupError("Cleanup file count changed; review a fresh scan")
    fresh = plan_cleanup(
        rip_root=rip_root,
        encoded_root=encoded_root,
        contract_root=contract_root,
        library_root=library_root,
        mode=plan.mode,  # type: ignore[arg-type]
        cleanup_root=cleanup_root,
        protected_roots=protected_roots,
        days=7 if plan.cutoff is not None else None,
        cutoff=datetime.fromisoformat(plan.cutoff) if plan.cutoff is not None else None,
    )
    if candidate_keys is not None:
        selected = set(candidate_keys)
        fresh = filter_plan(
            fresh,
            tuple(
                candidate
                for candidate in fresh.candidates
                if f"{candidate.category}:{candidate.relative_path}" in selected
            ),
        )
    if fresh.plan_sha256 != plan.plan_sha256:
        raise CleanupError("Cleanup files changed; review a fresh scan")
    roots = {
        "rip": rip_root.resolve(),
        "encoded": encoded_root.resolve(),
        "cleanup": cleanup_root.resolve() if cleanup_root is not None else None,
    }
    paths = []
    for candidate in plan.candidates:
        category_root = roots[candidate.category]
        if category_root is None:
            raise CleanupError("Cleanup staging root is unavailable")
        path = (category_root / Path(candidate.relative_path)).resolve()
        try:
            path.relative_to(category_root)
        except ValueError as exc:
            raise CleanupError("Cleanup path escaped its configured root") from exc
        try:
            stat = path.stat()
        except OSError as exc:
            raise CleanupError("Cleanup file is no longer available") from exc
        if stat.st_size != candidate.size_bytes:
            raise CleanupError("Cleanup file changed; review a fresh scan")
        paths.append(path)
    for path in paths:
        try:
            path.unlink()
        except OSError as exc:
            raise CleanupError("Cleanup stopped after a file deletion failure") from exc
    return len(paths)
