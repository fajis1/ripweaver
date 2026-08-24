"""Private restart-safe evidence and identification-attempt dossiers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.ffprobe_runner import inspect_mkv, resolve_ffprobe_path
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiMatchResult,
    GeminiReviewPlan,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.media.silent_video_review import (
    collect_silent_video_review,
    resolve_tesseract_path,
)
from mkv_episode_matcher.media.transcript_batch import (
    FFmpegSampleExtractor,
    TranscriptBatchItem,
    collect_transcript_batch,
    resolve_ffmpeg_path,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_BRANCHES = {
    "tv-local",
    "tv-opensubtitles",
    "tv-disc-range",
    "tv-gemini",
    "tv-play-all",
    "tv-movie",
    "movie-bonus",
    "tv-bonus",
    "gemini-synthesis",
    "visual-ocr",
}
_DISPOSITIONS = {"started", "matched", "review", "failed", "skipped"}
_AUDIT_DISPOSITIONS = _DISPOSITIONS | {"completed", "selected", "rejected"}
_AUDIT_EVENT_KINDS = {"workflow", "attempt", "candidate"}
_AUDIT_PHASE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ANALYSIS_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
SCHEMA_VERSION = 1
AUDIT_SCHEMA_VERSION = 1
SAMPLING_VERSION = "twelve-window-fallback-v3"


@dataclass(frozen=True)
class SourceIdentity:
    disc_fingerprint: str
    title_index: int
    source_size_bytes: int
    source_mtime_ns: int
    asr_model: str

    def digest(self) -> str:
        payload = {
            "disc_fingerprint": self.disc_fingerprint,
            "title_index": self.title_index,
            "source_size_bytes": self.source_size_bytes,
            "source_mtime_ns": self.source_mtime_ns,
            "asr_model": self.asr_model,
            "sampling_version": SAMPLING_VERSION,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def source_identity(payload: dict, source: Path, asr_model: str) -> SourceIdentity:
    fingerprint = payload.get("disc_fingerprint")
    title_index = payload.get("title_index")
    expected_size = payload.get("source_size_bytes")
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None
        or not isinstance(title_index, int)
        or isinstance(title_index, bool)
        or title_index < 0
        or not isinstance(expected_size, int)
        or expected_size <= 0
        or not source.is_file()
    ):
        raise PipelineQueueError("Verified source identity is unavailable")
    stat = source.stat()
    if stat.st_size != expected_size:
        raise PipelineQueueError("Verified source size changed")
    return SourceIdentity(
        fingerprint,
        title_index,
        expected_size,
        stat.st_mtime_ns,
        asr_model,
    )


class IdentificationDossierStore:
    """Store private excerpts and safe branch summaries outside public events."""

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def _check_id(media_id: str) -> str:
        if _SAFE_ID.fullmatch(media_id) is None:
            raise PipelineQueueError("Evidence dossier media ID is invalid")
        return media_id

    def _path(self, media_id: str) -> Path:
        return self.root / f"{self._check_id(media_id)}.private.json"

    def _audit_path(self, media_id: str) -> Path:
        return self.root / f"{self._check_id(media_id)}.identification-audit.jsonl"

    @staticmethod
    def _validate_safe_summary(
        summary: dict[str, str | int | float | bool | None | list[str]],
    ) -> None:
        if any(
            not isinstance(key, str)
            or len(key) > 64
            or isinstance(value, dict | tuple)
            or (
                isinstance(value, list)
                and (
                    len(value) > 12
                    or any(
                        not isinstance(item, str) or _SAFE_ID.fullmatch(item) is None
                        for item in value
                    )
                )
            )
            or (isinstance(value, str) and len(value) > 240)
            for key, value in summary.items()
        ):
            raise PipelineQueueError("Identification attempt details are unsafe")

    def _append_audit_events(
        self, media_id: str, events: tuple[dict[str, object], ...]
    ) -> None:
        """Append safe audit records without expanding the polled queue response."""

        if not events:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._audit_path(media_id)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            for event in events:
                stream.write(
                    json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
                )
            stream.flush()
            os.fsync(stream.fileno())

    def _load(self, media_id: str) -> dict | None:
        path = self._path(media_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Private evidence dossier is unreadable") from exc
        if payload.get("schema_version") != SCHEMA_VERSION:
            return None
        return payload

    def _write(self, media_id: str, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(media_id)
        temporary = self.root / f".{target.name}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def load_evidence(
        self, media_id: str, identity: SourceIdentity
    ) -> UnmatchedFileEvidence | None:
        payload = self._load(media_id)
        if payload is None or payload.get("source_identity") != identity.digest():
            return None
        return self._evidence_from_payload(media_id, payload)

    @staticmethod
    def _evidence_from_payload(
        media_id: str, payload: dict
    ) -> UnmatchedFileEvidence | None:
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict):
            return None
        duration = evidence.get("duration_seconds")
        excerpts = evidence.get("transcript_excerpts")
        if (
            not isinstance(duration, int | float)
            or duration <= 0
            or not isinstance(excerpts, list)
            or not 0 <= len(excerpts) <= 12
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 600
                for value in excerpts
            )
        ):
            return None
        return UnmatchedFileEvidence(media_id, float(duration), tuple(excerpts))

    def load_evidence_by_identity(
        self, media_id: str, identity: SourceIdentity
    ) -> UnmatchedFileEvidence | None:
        """Reuse private evidence after a queue ID changes for the exact source."""

        if not self.root.is_dir():
            return None
        expected = identity.digest()
        for path in self.root.glob("*.private.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                payload.get("schema_version") != SCHEMA_VERSION
                or payload.get("source_identity") != expected
            ):
                continue
            return self._evidence_from_payload(media_id, payload)
        return None

    def save_evidence(
        self, identity: SourceIdentity, evidence: UnmatchedFileEvidence
    ) -> None:
        previous = self._load(evidence.file_id)
        attempts = (
            previous.get("attempts", [])
            if previous and isinstance(previous.get("attempts"), list)
            else []
        )
        self._write(
            evidence.file_id,
            {
                "schema_version": SCHEMA_VERSION,
                "media_id": evidence.file_id,
                "source_identity": identity.digest(),
                "sampling_version": SAMPLING_VERSION,
                "evidence": {
                    "duration_seconds": evidence.duration_seconds,
                    "transcript_excerpts": list(evidence.transcript_excerpts),
                },
                "attempts": attempts,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def record_initial_match_trace(  # noqa: C901 - linear audit normalization
        self,
        media_id: str,
        trace: dict[str, object],
        *,
        candidate_series_name: str | None = None,
    ) -> None:
        """Persist one complete path- and dialogue-free first-pass decision."""

        checked_id = self._check_id(media_id)
        if not isinstance(trace, dict) or trace.get("schema_version") != 1:
            return
        payload = self._load(checked_id)
        if payload is None:
            self._write(
                checked_id,
                {
                    "schema_version": SCHEMA_VERSION,
                    "media_id": checked_id,
                    "attempts": [],
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
        analysis_run_id = uuid4().hex
        engine_decision = trace.get("engine_decision")
        matched = engine_decision == "matched"

        def safe_value(key: str) -> str | int | float | bool | None:
            value = trace.get(key)
            if isinstance(value, bool | int | float) or value is None:
                return value
            if isinstance(value, str):
                return " ".join(value.split())[:240]
            return None

        summary_keys = (
            "policy",
            "segment_threshold",
            "engine_threshold",
            "reference_variant_count",
            "duration_seconds",
            "successful_segment_count",
            "empty_segment_count",
            "selected_episode_id",
            "selected_episode_title",
            "selected_score",
            "selected_vote_count",
            "selected_score_sum",
            "runner_up_episode_id",
            "runner_up_score",
            "runner_up_vote_count",
            "supplemental_attempted",
            "supplemental_segment_count",
            "supplemental_reason",
            "subtitle_release_match",
            "subtitle_release_name",
            "engine_reason",
        )
        summary = {
            key: value for key in summary_keys if (value := safe_value(key)) is not None
        }
        segments = trace.get("segments")
        if not isinstance(segments, list):
            segments = []
        summary["candidate_segment_count"] = len(segments)
        review_candidates = [
            candidate
            for segment in segments
            if isinstance(segment, dict)
            for candidate in segment.get("candidate_evaluations", [])
            if isinstance(candidate, dict)
            and isinstance(candidate.get("candidate_episode_id"), str)
            and isinstance(candidate.get("candidate_episode_title"), str)
            and isinstance(candidate.get("score"), int | float)
            and not isinstance(candidate.get("score"), bool)
        ]
        best_review_candidate = max(
            review_candidates,
            key=lambda candidate: float(candidate["score"]),
            default=None,
        )
        if best_review_candidate is not None and isinstance(candidate_series_name, str):
            cleaned_series_name = " ".join(candidate_series_name.split())[:240]
            if cleaned_series_name:
                summary.update({
                    "candidate_series_name": cleaned_series_name,
                    "candidate_episode_id": " ".join(
                        str(best_review_candidate["candidate_episode_id"]).split()
                    )[:240],
                    "candidate_episode_title": " ".join(
                        str(best_review_candidate["candidate_episode_title"]).split()
                    )[:240],
                    "best_score": float(best_review_candidate["score"]),
                })
        self.record_attempt(
            (checked_id,),
            branch="tv-local",
            disposition="matched" if matched else "review",
            summary=summary,
            analysis_run_id=analysis_run_id,
        )

        selected_episode_id = trace.get("selected_episode_id")
        for ordinal, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                continue
            phase = f"initial-segment-{ordinal}"
            segment_summary = {
                "segment_index": int(segment.get("segment_index", ordinal - 1)),
                "sample_start_seconds": float(segment.get("sample_start_seconds", 0.0)),
                "sample_duration_seconds": float(
                    segment.get("sample_duration_seconds", 0.0)
                ),
                "reference_variant_count": int(
                    segment.get("reference_variant_count", 0)
                ),
                "episode_candidate_count": int(
                    segment.get("episode_candidate_count", 0)
                ),
                "qualifying_candidate_count": int(
                    segment.get("qualifying_candidate_count", 0)
                ),
                "best_episode_id": (
                    " ".join(str(segment.get("best_episode_id")).split())[:240]
                    if segment.get("best_episode_id") is not None
                    else None
                ),
                "best_score": float(segment.get("best_score", 0.0)),
                "reason": " ".join(str(segment.get("reason", "unknown")).split())[:240],
            }
            status = segment.get("status")
            self.record_workflow_event(
                (checked_id,),
                analysis_run_id=analysis_run_id,
                phase=phase,
                disposition=(
                    "failed"
                    if status == "failed"
                    else "review"
                    if status in {"below_threshold", "unusable_transcript"}
                    else "completed"
                ),
                summary=segment_summary,
            )
            evaluations = []
            raw_evaluations = segment.get("candidate_evaluations")
            if not isinstance(raw_evaluations, list):
                raw_evaluations = []
            for candidate in raw_evaluations:
                if not isinstance(candidate, dict):
                    continue
                candidate_id = candidate.get("candidate_episode_id")
                qualified = candidate.get("qualified") is True
                selected = bool(
                    matched and qualified and candidate_id == selected_episode_id
                )
                reason = (
                    "contributed_to_selected_episode"
                    if selected
                    else "below_segment_threshold"
                    if not qualified
                    else "different_episode_won"
                    if candidate_id != selected_episode_id
                    else "final_engine_decision_required_review"
                )
                evaluations.append({
                    "phase": "initial-six-window",
                    "segment_index": int(segment.get("segment_index", ordinal - 1)),
                    "sample_start_seconds": float(
                        segment.get("sample_start_seconds", 0.0)
                    ),
                    "rank": int(candidate.get("rank", len(evaluations) + 1)),
                    "candidate_episode_id": (
                        " ".join(str(candidate_id).split())[:240]
                        if candidate_id is not None
                        else None
                    ),
                    "candidate_episode_title": (
                        " ".join(str(candidate.get("candidate_episode_title")).split())[
                            :240
                        ]
                        if candidate.get("candidate_episode_title") is not None
                        else None
                    ),
                    "score": float(candidate.get("score", 0.0)),
                    "segment_threshold": float(candidate.get("segment_threshold", 0.0)),
                    "qualified": qualified,
                    "subtitle_release_match": (
                        " ".join(
                            str(
                                candidate.get("subtitle_release_match", "unresolved")
                            ).split()
                        )[:240]
                    ),
                    "subtitle_release_name": (
                        " ".join(str(candidate.get("subtitle_release_name")).split())[
                            :240
                        ]
                        if candidate.get("subtitle_release_name") is not None
                        else None
                    ),
                    "disposition": "selected" if selected else "rejected",
                    "reason": reason,
                })
            self.record_candidate_evaluations(
                checked_id,
                analysis_run_id=analysis_run_id,
                branch="tv-local",
                evaluations=tuple(evaluations),
            )

        self.record_workflow_event(
            (checked_id,),
            analysis_run_id=analysis_run_id,
            phase="initial-local-matcher",
            disposition="matched" if matched else "review",
            summary={
                "engine_decision": ("matched" if matched else "review"),
                "engine_reason": safe_value("engine_reason"),
                "selected_episode_id": safe_value("selected_episode_id"),
                "selected_score": safe_value("selected_score"),
                "runner_up_episode_id": safe_value("runner_up_episode_id"),
                "runner_up_score": safe_value("runner_up_score"),
            },
        )

    def record_attempt(
        self,
        media_ids: tuple[str, ...],
        *,
        branch: str,
        disposition: str,
        summary: dict[str, str | int | float | bool | None | list[str]],
        analysis_run_id: str | None = None,
    ) -> None:
        if branch not in _BRANCHES or disposition not in _DISPOSITIONS:
            raise PipelineQueueError("Identification attempt summary is invalid")
        self._validate_safe_summary(summary)
        if (
            analysis_run_id is not None
            and _ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None
        ):
            raise PipelineQueueError("Identification analysis run ID is invalid")
        now = datetime.now(UTC).isoformat()
        for media_id in media_ids:
            payload = self._load(media_id)
            if payload is None:
                continue
            attempts = payload.setdefault("attempts", [])
            attempt = {
                "branch": branch,
                "disposition": disposition,
                "summary": summary,
                "created_at": now,
            }
            if analysis_run_id is not None:
                attempt["analysis_run_id"] = analysis_run_id
            attempts.append(attempt)
            payload["attempts"] = attempts[-20:]
            payload["updated_at"] = now
            self._write(media_id, payload)
            self._append_audit_events(
                media_id,
                (
                    {
                        "schema_version": AUDIT_SCHEMA_VERSION,
                        "event_kind": "attempt",
                        "analysis_run_id": analysis_run_id,
                        "branch": branch,
                        "disposition": disposition,
                        "summary": summary,
                        "created_at": now,
                    },
                ),
            )

    def record_workflow_event(
        self,
        media_ids: tuple[str, ...],
        *,
        analysis_run_id: str,
        phase: str,
        disposition: str,
        summary: dict[str, str | int | float | bool | None | list[str]],
    ) -> None:
        """Record a path- and dialogue-free workflow boundary for a disc run."""

        if (
            _ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None
            or _AUDIT_PHASE.fullmatch(phase) is None
            or disposition not in _AUDIT_DISPOSITIONS
        ):
            raise PipelineQueueError("Identification workflow audit is invalid")
        self._validate_safe_summary(summary)
        now = datetime.now(UTC).isoformat()
        for media_id in media_ids:
            self._append_audit_events(
                media_id,
                (
                    {
                        "schema_version": AUDIT_SCHEMA_VERSION,
                        "event_kind": "workflow",
                        "analysis_run_id": analysis_run_id,
                        "phase": phase,
                        "disposition": disposition,
                        "summary": summary,
                        "created_at": now,
                    },
                ),
            )

    def record_candidate_evaluations(
        self,
        media_id: str,
        *,
        analysis_run_id: str,
        branch: str,
        evaluations: tuple[dict[str, str | int | float | bool | None | list[str]], ...],
    ) -> None:
        """Persist every safe candidate decision outside routine dashboard polling."""

        if (
            _ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None
            or branch not in _BRANCHES
        ):
            raise PipelineQueueError("Identification candidate audit is invalid")
        now = datetime.now(UTC).isoformat()
        records = []
        for evaluation in evaluations:
            self._validate_safe_summary(evaluation)
            disposition = evaluation.get("disposition")
            if disposition not in _AUDIT_DISPOSITIONS:
                raise PipelineQueueError("Candidate audit disposition is invalid")
            records.append({
                "schema_version": AUDIT_SCHEMA_VERSION,
                "event_kind": "candidate",
                "analysis_run_id": analysis_run_id,
                "branch": branch,
                "disposition": disposition,
                "summary": evaluation,
                "created_at": now,
            })
        self._append_audit_events(media_id, tuple(records))

    def audit_events(self, media_id: str) -> tuple[dict[str, object], ...]:
        """Return the complete safe audit, ignoring only an interrupted tail line."""

        path = self._audit_path(media_id)
        if not path.is_file():
            return ()
        events = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise PipelineQueueError("Identification audit is unreadable") from exc
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    continue
                raise PipelineQueueError("Identification audit is unreadable") from exc
            if (
                not isinstance(event, dict)
                or event.get("schema_version") != AUDIT_SCHEMA_VERSION
                or event.get("event_kind") not in _AUDIT_EVENT_KINDS
                or event.get("disposition") not in _AUDIT_DISPOSITIONS
                or not isinstance(event.get("summary"), dict)
            ):
                raise PipelineQueueError("Identification audit is unreadable")
            events.append(event)
        return tuple(events)

    def safe_attempts(self, media_id: str) -> tuple[dict[str, object], ...]:
        payload = self._load(media_id)
        if payload is None or not isinstance(payload.get("attempts"), list):
            return ()
        attempts = tuple(
            item
            for item in payload["attempts"]
            if isinstance(item, dict)
            and item.get("branch") in _BRANCHES
            and item.get("disposition") in _DISPOSITIONS
            and isinstance(item.get("summary"), dict)
        )
        return attempts[-12:]

    def attempted(self, media_id: str, branch: str) -> bool:
        return any(
            item.get("branch") == branch for item in self.safe_attempts(media_id)
        )

    def load_gemini_review(
        self, request_digest: str, *, model: str
    ) -> GeminiReviewPlan | None:
        if re.fullmatch(r"[0-9a-f]{64}", request_digest) is None:
            raise PipelineQueueError("Gemini cache digest is invalid")
        target = self.root / "gemini-review-cache" / f"{request_digest}.private.json"
        if not target.is_file():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version") != 1
                or payload.get("request_digest") != request_digest
                or payload.get("model") != model
                or not isinstance(payload.get("matches"), list)
            ):
                return None
            matches = tuple(
                GeminiMatchResult(
                    file_id=str(item["file_id"]),
                    episode_id=(
                        str(item["episode_id"])
                        if item.get("episode_id") is not None
                        else None
                    ),
                    confidence=float(item["confidence"]),
                    evidence=tuple(str(value) for value in item["evidence"]),
                )
                for item in payload["matches"]
            )
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return GeminiReviewPlan("gemini-unmatched-review-plan", model, matches)

    def save_gemini_review(self, request_digest: str, review: GeminiReviewPlan) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", request_digest) is None:
            raise PipelineQueueError("Gemini cache digest is invalid")
        folder = self.root / "gemini-review-cache"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{request_digest}.private.json"
        if target.exists():
            return
        temporary = folder / f".{request_digest}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_digest": request_digest,
                    "model": review.model,
                    "matches": [asdict(match) for match in review.matches],
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)


def collect_dossier_evidence(  # noqa: C901 - linear cache/probe/audio guards
    items: tuple[tuple[object, dict], ...],
    config: Config,
    asr,
    contract_root: Path,
    include_visual_text: bool = False,
    visual_review_recorder: Callable[[str, str], None] | None = None,
    analysis_run_id: str | None = None,
) -> tuple[tuple[UnmatchedFileEvidence, ...], IdentificationDossierStore]:
    """Reuse exact-source evidence and collect only cache misses.

    Transcript excerpts remain in the private dossier. The returned objects are
    intended only for the in-process matchers and must not be placed in events.
    """

    dossier = IdentificationDossierStore(
        contract_root.parent / "identification-evidence"
    )
    ordered_ids: list[str] = []
    identities: dict[str, SourceIdentity] = {}
    cached: dict[str, UnmatchedFileEvidence] = {}
    missing: list[tuple[object, Path]] = []
    for item, payload in items:
        media_id = getattr(item, "media_id", None)
        if not isinstance(media_id, str):
            raise PipelineQueueError("Evidence item media ID is invalid")
        source = Path(str(payload.get("source_path", "")))
        identity = source_identity(payload, source, config.asr_model_name)
        ordered_ids.append(media_id)
        identities[media_id] = identity
        evidence = dossier.load_evidence(media_id, identity)
        if evidence is None:
            evidence = dossier.load_evidence_by_identity(media_id, identity)
            if evidence is not None:
                dossier.save_evidence(identity, evidence)
        if evidence is None:
            missing.append((item, source))
        else:
            cached[media_id] = evidence

    if missing:
        ffprobe = resolve_ffprobe_path(config.ffprobe_path)
        ffmpeg = resolve_ffmpeg_path(config.ffmpeg_path)
        transcript_items = []
        runtime_only: dict[str, UnmatchedFileEvidence] = {}
        for item, source in missing:
            inspection = inspect_mkv(ffprobe, source, timeout_seconds=60)
            if inspection.media.audio_streams:
                transcript_items.append(
                    TranscriptBatchItem(item.media_id, source, inspection.media)
                )
            else:
                runtime_only[item.media_id] = UnmatchedFileEvidence(
                    item.media_id, float(inspection.media.duration_seconds), ()
                )
        collected = {}
        if transcript_items:
            transcripts = collect_transcript_batch(
                tuple(transcript_items),
                asr,
                FFmpegSampleExtractor(ffmpeg),
                model_name=config.asr_model_name,
                sampling_mode="expanded",
            )
            collected = {item.file_id: item for item in transcripts.files}
        for item, _source in missing:
            runtime_evidence = runtime_only.get(item.media_id)
            if runtime_evidence is not None:
                dossier.save_evidence(identities[item.media_id], runtime_evidence)
                cached[item.media_id] = runtime_evidence
                continue
            transcript = collected.get(item.media_id)
            if transcript is None:
                raise PipelineQueueError(
                    "Transcript collection omitted a reviewed title"
                )
            evidence = UnmatchedFileEvidence(
                item.media_id,
                float(transcript.duration_seconds),
                tuple(
                    window.text[:600]
                    for window in transcript.windows
                    if window.text.strip()
                )[:6],
            )
            dossier.save_evidence(identities[item.media_id], evidence)
            cached[item.media_id] = evidence
    ordered = tuple(cached[media_id] for media_id in ordered_ids)
    if not include_visual_text:
        return ordered, dossier

    try:
        ffmpeg = resolve_ffmpeg_path(config.ffmpeg_path)
        tesseract = resolve_tesseract_path(config.tesseract_path)
    except (OSError, RuntimeError, ValueError):
        dossier.record_attempt(
            tuple(ordered_ids),
            branch="visual-ocr",
            disposition="failed",
            analysis_run_id=analysis_run_id,
            summary={"reason": "visual_tools_unavailable"},
        )
        return ordered, dossier
    visual_root = (
        contract_root.parent / "identification-evidence" / f"visual-{uuid4().hex}"
    )
    visual_root.mkdir(parents=True, exist_ok=False)
    augmented: list[UnmatchedFileEvidence] = []
    sources = {
        item.media_id: Path(str(payload.get("source_path", "")))
        for item, payload in items
    }
    for evidence in ordered:
        excerpts = evidence.transcript_excerpts
        try:
            item_visual_root = (
                visual_root
                / hashlib.sha256(evidence.file_id.encode("utf-8")).hexdigest()[:16]
            )
            item_visual_root.mkdir()
            visual = collect_silent_video_review(
                media_id=evidence.file_id,
                media_path=sources[evidence.file_id],
                duration_seconds=evidence.duration_seconds,
                output_root=item_visual_root,
                ffmpeg_path=ffmpeg,
                tesseract_path=tesseract,
            )
            dossier.record_attempt(
                (evidence.file_id,),
                branch="visual-ocr",
                disposition=(
                    "matched"
                    if visual.category in {"likely_warning_screen", "likely_disc_menu"}
                    else "review"
                ),
                analysis_run_id=analysis_run_id,
                summary={
                    "category": visual.category,
                    "ocr_text_characters": visual.ocr_text_characters,
                    "sampled_frame_count": visual.sampled_frame_count,
                },
            )
            if visual_review_recorder is not None:
                visual_review_recorder(evidence.file_id, visual.category)
            if visual.ocr_excerpt:
                on_screen = f"On-screen text (OCR): {visual.ocr_excerpt}"[:600]
                excerpts = (on_screen, *excerpts[:5])
        except (OSError, RuntimeError, ValueError) as exc:
            dossier.record_attempt(
                (evidence.file_id,),
                branch="visual-ocr",
                disposition="failed",
                analysis_run_id=analysis_run_id,
                summary={"reason": type(exc).__name__},
            )
        augmented.append(
            UnmatchedFileEvidence(
                evidence.file_id,
                evidence.duration_seconds,
                excerpts,
            )
        )
    return tuple(augmented), dossier


def collect_supplemental_dossier_evidence(
    items: tuple[tuple[object, dict], ...],
    existing: tuple[UnmatchedFileEvidence, ...],
    config: Config,
    asr,
    contract_root: Path,
) -> tuple[tuple[UnmatchedFileEvidence, ...], IdentificationDossierStore]:
    """Add one offset six-window pass for exact sources that remain unresolved."""

    dossier = IdentificationDossierStore(
        contract_root.parent / "identification-evidence"
    )
    existing_by_id = {item.file_id: item for item in existing}
    ffprobe = resolve_ffprobe_path(config.ffprobe_path)
    ffmpeg = resolve_ffmpeg_path(config.ffmpeg_path)
    transcript_items: list[TranscriptBatchItem] = []
    identities: dict[str, SourceIdentity] = {}
    for item, payload in items:
        media_id = getattr(item, "media_id", None)
        if not isinstance(media_id, str) or media_id not in existing_by_id:
            raise PipelineQueueError("Supplemental evidence item is invalid")
        current = existing_by_id[media_id]
        if len(current.transcript_excerpts) >= 12:
            continue
        source = Path(str(payload.get("source_path", "")))
        identities[media_id] = source_identity(payload, source, config.asr_model_name)
        inspection = inspect_mkv(ffprobe, source, timeout_seconds=60)
        if inspection.media.audio_streams:
            transcript_items.append(
                TranscriptBatchItem(media_id, source, inspection.media)
            )

    if transcript_items:
        result = collect_transcript_batch(
            tuple(transcript_items),
            asr,
            FFmpegSampleExtractor(ffmpeg),
            model_name=config.asr_model_name,
            sampling_mode="expanded-offset",
        )
        for transcript in result.files:
            current = existing_by_id[transcript.file_id]
            added = tuple(
                window.text[:600]
                for window in transcript.windows
                if window.text.strip()
                and window.text[:600] not in current.transcript_excerpts
            )
            combined = UnmatchedFileEvidence(
                current.file_id,
                current.duration_seconds,
                (*current.transcript_excerpts, *added)[:12],
            )
            dossier.save_evidence(identities[transcript.file_id], combined)
            existing_by_id[transcript.file_id] = combined

    return (
        tuple(existing_by_id[item.media_id] for item, _payload in items),
        dossier,
    )
