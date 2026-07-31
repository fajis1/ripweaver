"""Safe, explicit HandBrakeCLI adapter for reviewed media transcodes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mkv_episode_matcher.core.environment import load_environment_settings


class HandBrakeError(RuntimeError):
    """Raised when a HandBrake operation cannot proceed safely."""


class HandBrakeProcessError(HandBrakeError):
    """A nonzero HandBrake exit with a typed interruption classification."""

    def __init__(self, return_code: int) -> None:
        self.return_code = return_code
        self.interrupted = _is_process_interruption(return_code)
        super().__init__(
            f"HandBrake failed with exit code {return_code}; "
            "partial output was preserved"
        )


_HARDWARE_ENCODERS = {"vce_h264", "vce_h265", "vce_h265_10bit"}
_EXPECTED_VIDEO_CODEC = {
    "vce_h264": "h264",
    "vce_h265": "hevc",
    "vce_h265_10bit": "hevc",
}
_NLMEANS_PRESETS = {"ultralight", "light", "medium", "strong"}
_NLMEANS_TUNES = {
    "none",
    "film",
    "grain",
    "highmotion",
    "animation",
    "tape",
    "sprite",
}
_CONTENT_KINDS = {"unknown", "live_action", "animation"}
_MEDIA_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_WINDOWS_PATH_TO_LINE_END = re.compile(
    r"(?im)[A-Za-z]:[\\/].*$",
)
_UNC_PATH_TO_LINE_END = re.compile(r"(?m)(?<![\\])\\\\[^\\\r\n]+\\.*$")
_WINDOWS_INTERRUPTION_CODES = {
    0x40010004,  # DBG_TERMINATE_PROCESS
    0xC000013A,  # STATUS_CONTROL_C_EXIT
}
_POSIX_INTERRUPTION_CODES = {-1, -2, -15}  # SIGHUP, SIGINT, SIGTERM


def _is_process_interruption(return_code: int) -> bool:
    normalized = return_code & 0xFFFFFFFF
    return (
        return_code in _POSIX_INTERRUPTION_CODES
        or normalized in _WINDOWS_INTERRUPTION_CODES
    )


@dataclass(frozen=True)
class HandBrakeProfile:
    """Reviewed encoding choices independent of any mutable GUI preset."""

    encoder: str = "vce_h265"
    encoder_preset: str = "quality"
    quality: float = 26.0
    selective_decomb: bool = True
    content_kind: str = "unknown"
    nlmeans_preset: str | None = None
    nlmeans_tune: str = "none"
    audio_track: int = 1
    compatibility_audio_bitrate: int = 256
    stereo_first: bool = True
    retain_subtitles: bool = True


@dataclass(frozen=True)
class HandBrakeJob:
    media_id: str
    source: Path
    destination: Path
    profile: HandBrakeProfile = HandBrakeProfile()
    sample_start_seconds: int | None = None
    sample_duration_seconds: int | None = None
    attempt_number: int = 1

    def safe_plan(self) -> dict[str, object]:
        return {
            "mode": "handbrake-plan",
            "media_id": self.media_id,
            "profile": asdict(self.profile),
            "sample_start_seconds": self.sample_start_seconds,
            "sample_duration_seconds": self.sample_duration_seconds,
            "attempt_number": self.attempt_number,
            "source_extension": self.source.suffix.lower(),
            "destination_extension": self.destination.suffix.lower(),
        }


@dataclass(frozen=True)
class HandBrakeCapabilities:
    vcn_available: bool
    encoders: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedMedia:
    duration_seconds: float
    size_bytes: int
    video_codec: str
    audio_streams: int
    subtitle_streams: int


@dataclass(frozen=True)
class HandBrakeResult:
    media_id: str
    encoder: str
    output_bytes: int
    duration_seconds: float
    video_codec: str
    audio_streams: int
    subtitle_streams: int
    process_log: Path
    event_log: Path

    def safe_report(self) -> dict[str, object]:
        report = asdict(self)
        report.pop("process_log")
        report.pop("event_log")
        return {"mode": "handbrake-result", **report}


def resolve_handbrake_path(explicit_path: Path | None = None) -> Path:
    candidate = explicit_path or load_environment_settings().handbrake_path
    if candidate is None:
        discovered = shutil.which("HandBrakeCLI")
        if discovered is None:
            raise HandBrakeError(
                "HandBrakeCLI was not found; configure HANDBRAKE_PATH or "
                "pass --handbrake-path"
            )
        candidate = Path(discovered)
    candidate = candidate.expanduser()
    if not candidate.is_file():
        raise HandBrakeError("Configured HandBrakeCLI executable was not found")
    return candidate.resolve()


def validate_handbrake_job(job: HandBrakeJob) -> HandBrakeJob:
    _validate_job_paths(job)
    _validate_profile(job.profile)
    _validate_sample(job)
    return job


def _validate_job_paths(job: HandBrakeJob) -> None:
    if _MEDIA_ID.fullmatch(job.media_id) is None:
        raise HandBrakeError("Media ID contains unsupported characters")
    if not job.source.is_file() or job.source.suffix.lower() != ".mkv":
        raise HandBrakeError("Transcode source must be one explicit MKV")
    if job.destination.suffix.lower() != ".mkv":
        raise HandBrakeError("Transcode destination must use the .mkv extension")
    if not job.destination.parent.is_dir():
        raise HandBrakeError("Transcode destination directory does not exist")
    if job.destination.exists():
        raise HandBrakeError("Transcode destination exists; refusing overwrite")
    if job.source.resolve() == job.destination.resolve():
        raise HandBrakeError("Transcode source and destination must differ")
    if job.source.resolve().parent == job.destination.resolve().parent:
        raise HandBrakeError("Transcode destination must use a separate staging folder")


def _validate_nlmeans(profile: HandBrakeProfile) -> None:
    if profile.content_kind not in _CONTENT_KINDS:
        raise HandBrakeError("Unsupported content kind")
    if (
        profile.nlmeans_preset is not None
        and profile.nlmeans_preset not in _NLMEANS_PRESETS
    ):
        raise HandBrakeError("Unsupported NLMeans preset")
    if profile.nlmeans_tune not in _NLMEANS_TUNES:
        raise HandBrakeError("Unsupported NLMeans tune")
    if profile.nlmeans_preset is None and profile.nlmeans_tune != "none":
        raise HandBrakeError("NLMeans tune requires an NLMeans preset")
    if profile.nlmeans_preset is not None and profile.content_kind != "live_action":
        raise HandBrakeError("NLMeans requires explicitly reviewed live-action content")


def _validate_profile(profile: HandBrakeProfile) -> None:
    if profile.encoder not in _HARDWARE_ENCODERS:
        raise HandBrakeError("Only explicit AMD VCN encoders are permitted")
    if profile.encoder_preset not in {"speed", "balanced", "quality"}:
        raise HandBrakeError("Unsupported AMD VCN encoder preset")
    if not 0 <= profile.quality <= 51:
        raise HandBrakeError("HandBrake quality must be between 0 and 51")
    _validate_nlmeans(profile)
    if profile.audio_track <= 0:
        raise HandBrakeError("HandBrake audio track is one-based and must be positive")
    if not 64 <= profile.compatibility_audio_bitrate <= 512:
        raise HandBrakeError("Compatibility audio bitrate is outside safe bounds")


def validate_handbrake_profile(profile: HandBrakeProfile) -> HandBrakeProfile:
    """Validate a reusable profile without requiring media or destination paths."""

    _validate_profile(profile)
    return profile


def _validate_sample(job: HandBrakeJob) -> None:
    sample_values = (job.sample_start_seconds, job.sample_duration_seconds)
    if (sample_values[0] is None) != (sample_values[1] is None):
        raise HandBrakeError("Sample start and duration must be supplied together")
    if job.sample_start_seconds is not None and job.sample_start_seconds < 0:
        raise HandBrakeError("Sample start must not be negative")
    if job.sample_duration_seconds is not None and not (
        10 <= job.sample_duration_seconds <= 600
    ):
        raise HandBrakeError("Sample duration must be between 10 and 600 seconds")
    if job.attempt_number < 1:
        raise HandBrakeError("Transcode attempt number must be positive")


def partial_output_path(job: HandBrakeJob) -> Path:
    attempt = "" if job.attempt_number == 1 else f".retry-{job.attempt_number:03d}"
    return job.destination.with_name(
        f"{job.destination.stem}.{job.media_id}{attempt}.partial"
        f"{job.destination.suffix}"
    )


def build_handbrake_command(
    executable: Path,
    job: HandBrakeJob,
    *,
    output_path: Path | None = None,
) -> tuple[str, ...]:
    validate_handbrake_job(job)
    if not executable.is_file():
        raise HandBrakeError("HandBrakeCLI executable was not found")
    partial = output_path or partial_output_path(job)
    if partial.exists():
        raise HandBrakeError("Partial transcode exists; refusing overwrite")

    profile = job.profile
    audio_track = str(profile.audio_track)
    if profile.stereo_first:
        audio_encoders = "av_aac,copy"
        audio_mixdowns = "dpl2,none"
        audio_bitrates = f"{profile.compatibility_audio_bitrate},0"
    else:
        audio_encoders = "copy,av_aac"
        audio_mixdowns = "none,dpl2"
        audio_bitrates = f"0,{profile.compatibility_audio_bitrate}"
    command = [
        str(executable.resolve()),
        "--input",
        str(job.source.resolve()),
        "--output",
        str(partial.resolve()),
        "--format",
        "av_mkv",
        "--encoder",
        profile.encoder,
        "--encoder-preset",
        profile.encoder_preset,
        "--quality",
        str(profile.quality),
        "--vfr",
        "--audio",
        f"{audio_track},{audio_track}",
        "--aencoder",
        audio_encoders,
        "--audio-copy-mask",
        "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,opus,vorbis,flac,alac,pcm",
        "--audio-fallback",
        "av_aac",
        "--mixdown",
        audio_mixdowns,
        "--ab",
        audio_bitrates,
        "--markers",
        "--keep-metadata",
        "--json",
    ]
    if profile.selective_decomb:
        command.extend(["--comb-detect", "--decomb"])
    if profile.nlmeans_preset is not None:
        command.extend([
            f"--nlmeans={profile.nlmeans_preset}",
            "--nlmeans-tune",
            profile.nlmeans_tune,
        ])
    if profile.retain_subtitles:
        command.append("--all-subtitles")
    if job.sample_start_seconds is not None:
        command.extend([
            "--start-at",
            f"seconds:{job.sample_start_seconds}",
            "--stop-at",
            f"seconds:{job.sample_duration_seconds}",
        ])
    return tuple(command)


def inspect_handbrake_capabilities(
    executable: Path,
    *,
    timeout_seconds: int = 30,
) -> HandBrakeCapabilities:
    if not executable.is_file():
        raise HandBrakeError("HandBrakeCLI executable was not found")
    try:
        completed = subprocess.run(
            (str(executable.resolve()), "--help"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandBrakeError(
            f"HandBrake capability check failed: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise HandBrakeError("HandBrake capability check returned a failure")
    combined = f"{completed.stdout}\n{completed.stderr}".lower()
    encoders = tuple(
        sorted(encoder for encoder in _HARDWARE_ENCODERS if encoder in combined)
    )
    return HandBrakeCapabilities(
        vcn_available="vcn: is available" in combined,
        encoders=encoders,
    )


def _redact(text: str, paths: tuple[Path, ...]) -> str:
    redacted = text
    candidates: set[str] = set()
    for path in paths:
        candidates.update({
            str(path),
            str(path.resolve()),
            str(path).replace("\\", "/"),
            str(path.resolve()).replace("\\", "/"),
            path.name,
        })
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            redacted = redacted.replace(candidate, "<media>")
    # HandBrake can echo paths stored in source metadata which are unrelated to
    # the explicit input/output paths. Drop the remainder of any such log line
    # so generated diagnostics cannot retain an unknown local or network path.
    redacted = _WINDOWS_PATH_TO_LINE_END.sub("<path>", redacted)
    redacted = _UNC_PATH_TO_LINE_END.sub("<path>", redacted)
    return redacted


def _probe_media(
    ffprobe: Path,
    media: Path,
    *,
    timeout_seconds: int = 60,
) -> VerifiedMedia:
    if not ffprobe.is_file():
        raise HandBrakeError("FFprobe executable was not found")
    try:
        completed = subprocess.run(
            (
                str(ffprobe.resolve()),
                "-v",
                "error",
                "-show_entries",
                "format=duration,size:stream=codec_type,codec_name",
                "-of",
                "json",
                str(media.resolve()),
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandBrakeError(
            f"Post-transcode verification failed: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise HandBrakeError("Post-transcode FFprobe returned a failure")
    try:
        payload: dict[str, Any] = json.loads(completed.stdout)
        format_data = payload["format"]
        streams = payload["streams"]
        duration = float(format_data["duration"])
        size = int(format_data["size"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HandBrakeError(
            "Post-transcode FFprobe returned invalid metadata"
        ) from exc
    videos = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    audio = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    subtitles = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "subtitle"
    ]
    if duration <= 0 or size <= 0 or len(videos) != 1 or not audio:
        raise HandBrakeError("Post-transcode media failed stream verification")
    return VerifiedMedia(
        duration_seconds=duration,
        size_bytes=size,
        video_codec=str(videos[0].get("codec_name") or ""),
        audio_streams=len(audio),
        subtitle_streams=len(subtitles),
    )


def _write_event(path: Path, event: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True))
        stream.write("\n")


def _validate_execution_boundary(
    executable: Path,
    job: HandBrakeJob,
    run_dir: Path,
    *,
    confirm_transcode: bool,
    timeout_seconds: int,
) -> None:
    if not confirm_transcode:
        raise HandBrakeError("Transcode execution requires explicit confirmation")
    validate_handbrake_job(job)
    if timeout_seconds <= 0:
        raise HandBrakeError("Transcode timeout must be positive")
    if not run_dir.is_dir():
        raise HandBrakeError("Transcode run-log directory does not exist")
    if (run_dir / "STOP").exists():
        raise HandBrakeError("Cancellation marker exists; refusing to start")

    capabilities = inspect_handbrake_capabilities(executable)
    if not capabilities.vcn_available:
        raise HandBrakeError("AMD VCN is not available")
    if job.profile.encoder not in capabilities.encoders:
        raise HandBrakeError("Requested AMD VCN encoder is not available")


def _new_log_paths(run_dir: Path, job: HandBrakeJob) -> tuple[Path, Path]:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    event_log = run_dir / f"handbrake-{job.media_id}-{timestamp}.jsonl"
    process_log = run_dir / f"handbrake-{job.media_id}-{timestamp}.log"
    for path in (event_log, process_log):
        if path.exists():
            raise HandBrakeError("Transcode log collision; refusing overwrite")
    return event_log, process_log


def _run_handbrake_process(
    command: tuple[str, ...],
    event_log: Path,
    job: HandBrakeJob,
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_event(
            event_log,
            {
                "at": datetime.now(UTC).isoformat(),
                "event": "timed-out",
                "media_id": job.media_id,
            },
        )
        raise HandBrakeError("HandBrake transcode timed out") from exc
    except OSError as exc:
        raise HandBrakeError(
            f"HandBrake could not be started: {type(exc).__name__}"
        ) from exc


def _verify_expected_output(
    ffprobe: Path,
    job: HandBrakeJob,
    partial: Path,
) -> VerifiedMedia:
    if not partial.is_file() or partial.stat().st_size <= 0:
        raise HandBrakeError("HandBrake reported success but produced no output")

    verified = _probe_media(ffprobe, partial)
    expected_codec = _EXPECTED_VIDEO_CODEC[job.profile.encoder]
    if verified.video_codec != expected_codec:
        raise HandBrakeError("Verified output uses an unexpected video codec")
    if job.sample_duration_seconds is not None:
        tolerance = max(5.0, job.sample_duration_seconds * 0.10)
        if abs(verified.duration_seconds - job.sample_duration_seconds) > tolerance:
            raise HandBrakeError("Verified sample duration is outside tolerance")
    if job.destination.exists():
        raise HandBrakeError(
            "Destination appeared during transcode; partial output was preserved"
        )
    return verified


def execute_handbrake_job(
    executable: Path,
    ffprobe: Path,
    job: HandBrakeJob,
    run_dir: Path,
    *,
    confirm_transcode: bool = False,
    timeout_seconds: int = 21600,
) -> HandBrakeResult:
    """Execute one explicitly approved job and promote only verified output."""

    _validate_execution_boundary(
        executable,
        job,
        run_dir,
        confirm_transcode=confirm_transcode,
        timeout_seconds=timeout_seconds,
    )

    partial = partial_output_path(job)
    command = build_handbrake_command(executable, job, output_path=partial)
    event_log, process_log = _new_log_paths(run_dir, job)

    _write_event(
        event_log,
        {
            "at": datetime.now(UTC).isoformat(),
            "event": "started",
            **job.safe_plan(),
        },
    )
    completed = _run_handbrake_process(
        command,
        event_log,
        job,
        timeout_seconds=timeout_seconds,
    )

    process_log.write_text(
        _redact(
            f"{completed.stdout}\n{completed.stderr}",
            (job.source, job.destination, partial),
        ),
        encoding="utf-8",
    )
    if completed.returncode != 0:
        _write_event(
            event_log,
            {
                "at": datetime.now(UTC).isoformat(),
                "event": "failed",
                "media_id": job.media_id,
                "return_code": completed.returncode,
            },
        )
        raise HandBrakeProcessError(completed.returncode)
    verified = _verify_expected_output(ffprobe, job, partial)
    partial.rename(job.destination)

    result = HandBrakeResult(
        media_id=job.media_id,
        encoder=job.profile.encoder,
        output_bytes=verified.size_bytes,
        duration_seconds=verified.duration_seconds,
        video_codec=verified.video_codec,
        audio_streams=verified.audio_streams,
        subtitle_streams=verified.subtitle_streams,
        process_log=process_log,
        event_log=event_log,
    )
    _write_event(
        event_log,
        {
            "at": datetime.now(UTC).isoformat(),
            "event": "verified-complete",
            **result.safe_report(),
        },
    )
    return result
