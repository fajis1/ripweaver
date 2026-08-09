"""Private restart-safe evidence and identification-attempt dossiers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.ffprobe_runner import inspect_mkv, resolve_ffprobe_path
from mkv_episode_matcher.media.gemini_matcher import UnmatchedFileEvidence
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
    "tv-gemini",
    "movie-bonus",
    "gemini-synthesis",
}
_DISPOSITIONS = {"started", "matched", "review", "failed", "skipped"}
SCHEMA_VERSION = 1
SAMPLING_VERSION = "six-window-v2"


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
            or not 0 <= len(excerpts) <= 6
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
            if previous and previous.get("source_identity") == identity.digest()
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

    def record_attempt(
        self,
        media_ids: tuple[str, ...],
        *,
        branch: str,
        disposition: str,
        summary: dict[str, str | int | float | bool | None],
    ) -> None:
        if branch not in _BRANCHES or disposition not in _DISPOSITIONS:
            raise PipelineQueueError("Identification attempt summary is invalid")
        if any(
            not isinstance(key, str)
            or len(key) > 64
            or isinstance(value, dict | list | tuple)
            or (isinstance(value, str) and len(value) > 240)
            for key, value in summary.items()
        ):
            raise PipelineQueueError("Identification attempt details are unsafe")
        now = datetime.now(UTC).isoformat()
        for media_id in media_ids:
            payload = self._load(media_id)
            if payload is None:
                continue
            attempts = payload.setdefault("attempts", [])
            attempts.append({
                "branch": branch,
                "disposition": disposition,
                "summary": summary,
                "created_at": now,
            })
            payload["attempts"] = attempts[-20:]
            payload["updated_at"] = now
            self._write(media_id, payload)

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


def collect_dossier_evidence(  # noqa: C901 - linear cache/probe/audio guards
    items: tuple[tuple[object, dict], ...],
    config: Config,
    asr,
    contract_root: Path,
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
    return tuple(cached[media_id] for media_id in ordered_ids), dossier
