"""Restartable, manifest-linked orchestration for the complete media pipeline."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class PipelineError(RuntimeError):
    """Raised when a checkpoint or stage contract cannot be trusted."""


PIPELINE_STAGES = ("rip", "identify", "transcode", "organize")


@dataclass(frozen=True)
class PipelineArtifact:
    """One immutable JSON contract passed from one stage to the next."""

    stage: str
    contract_path: Path
    contract_sha256: str
    item_count: int


@dataclass(frozen=True)
class PipelineStageContext:
    pipeline_id: str
    plan_sha256: str
    stage: str
    previous: PipelineArtifact | None


StageRunner = Callable[[PipelineStageContext], PipelineArtifact]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PipelineError("A pipeline contract could not be read") from exc
    return digest.hexdigest()


def _validate_artifact(artifact: PipelineArtifact, expected_stage: str) -> None:
    if artifact.stage != expected_stage or artifact.item_count < 0:
        raise PipelineError("A pipeline stage returned an invalid artifact")
    if not artifact.contract_path.is_file():
        raise PipelineError("A pipeline contract is missing")
    if _sha256(artifact.contract_path) != artifact.contract_sha256:
        raise PipelineError("A pipeline contract changed after its stage completed")


def _load_checkpoint(path: Path, pipeline_id: str, plan_sha256: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 1,
            "pipeline_id": pipeline_id,
            "plan_sha256": plan_sha256,
            "status": "pending",
            "completed": [],
            "artifacts": {},
        }
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("Pipeline checkpoint is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("pipeline_id") != pipeline_id
        or payload.get("plan_sha256") != plan_sha256
        or not isinstance(payload.get("completed"), list)
        or not isinstance(payload.get("artifacts"), dict)
    ):
        raise PipelineError("Pipeline checkpoint does not match the requested plan")
    completed = payload["completed"]
    if completed != list(PIPELINE_STAGES[: len(completed)]):
        raise PipelineError("Pipeline checkpoint stages are not contiguous")
    return payload


def _artifact_from_checkpoint(payload: dict[str, Any], stage: str) -> PipelineArtifact:
    try:
        raw = payload["artifacts"][stage]
        artifact = PipelineArtifact(
            stage=stage,
            contract_path=Path(raw["contract_path"]),
            contract_sha256=str(raw["contract_sha256"]),
            item_count=int(raw["item_count"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("Pipeline checkpoint artifact is invalid") from exc
    _validate_artifact(artifact, stage)
    return artifact


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise PipelineError("Pipeline checkpoint directory must already exist")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PipelineError("Pipeline checkpoint could not be updated") from exc


def run_checkpointed_pipeline(
    *,
    pipeline_id: str,
    plan_sha256: str,
    checkpoint_path: Path,
    runners: Mapping[str, StageRunner],
) -> PipelineArtifact:
    """Run or resume all four stages, validating every immutable handoff."""

    if set(runners) != set(PIPELINE_STAGES):
        raise PipelineError("Pipeline requires exactly one runner for every stage")
    if not pipeline_id or len(plan_sha256) != 64:
        raise PipelineError("Pipeline identity is invalid")
    checkpoint = _load_checkpoint(checkpoint_path, pipeline_id, plan_sha256)
    previous: PipelineArtifact | None = None
    for completed_stage in checkpoint["completed"]:
        previous = _artifact_from_checkpoint(checkpoint, completed_stage)

    start = len(checkpoint["completed"])
    for stage in PIPELINE_STAGES[start:]:
        context = PipelineStageContext(
            pipeline_id=pipeline_id,
            plan_sha256=plan_sha256,
            stage=stage,
            previous=previous,
        )
        try:
            artifact = runners[stage](context)
            _validate_artifact(artifact, stage)
        except Exception as exc:
            checkpoint["status"] = "failed"
            checkpoint["failed_stage"] = stage
            checkpoint["error_type"] = type(exc).__name__
            _write_checkpoint(checkpoint_path, checkpoint)
            if isinstance(exc, PipelineError):
                raise
            raise PipelineError(f"Pipeline stage failed: {stage}") from exc
        checkpoint["completed"].append(stage)
        checkpoint["artifacts"][stage] = {
            **asdict(artifact),
            "contract_path": str(artifact.contract_path.resolve()),
        }
        checkpoint["status"] = (
            "completed" if stage == PIPELINE_STAGES[-1] else "running"
        )
        checkpoint.pop("failed_stage", None)
        checkpoint.pop("error_type", None)
        _write_checkpoint(checkpoint_path, checkpoint)
        previous = artifact
    if previous is None:
        raise PipelineError("Pipeline completed without an artifact")
    return previous
