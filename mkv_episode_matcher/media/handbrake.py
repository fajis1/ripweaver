"""Safe, explicit HandBrakeCLI adapter for reviewed media transcodes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from mkv_episode_matcher.core.datetime_compat import UTC
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


_ENCODER_PRESETS = {
    "x264": {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    },
    "vce_h264": {"speed", "balanced", "quality"},
    "vce_h265": {"speed", "balanced", "quality"},
    "vce_h265_10bit": {"speed", "balanced", "quality"},
    "nvenc_h264": {"fast", "medium", "slow"},
    "nvenc_h265": {"fast", "medium", "slow"},
    "qsv_h264": {"speed", "balanced", "quality"},
    "qsv_h265": {"speed", "balanced", "quality"},
    "svt_av1": {str(preset) for preset in range(0, 14)},
    "x265": {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    },
}
_HARDWARE_ENCODERS = set(_ENCODER_PRESETS)
_AMD_ENCODERS = {"vce_h264", "vce_h265", "vce_h265_10bit"}
_EXPECTED_VIDEO_CODEC = {
    "x264": "h264",
    "vce_h264": "h264",
    "vce_h265": "hevc",
    "vce_h265_10bit": "hevc",
    "nvenc_h264": "h264",
    "nvenc_h265": "hevc",
    "qsv_h264": "h264",
    "qsv_h265": "hevc",
    "x265": "hevc",
    "svt_av1": "av1",
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
_AUDIO_PREFERENCES = {
    "default": None,
    "stereo": 2,
    "2.1": 3,
    "5.1": 6,
    "7.1": 8,
    "highest": None,
}
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
_RESOLUTION_POLICIES = {
    "source": None,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "2160p": 2160,
}
_FRAME_RATE_POLICIES = {
    "source",
    "vfr",
    "23.976",
    "24",
    "25",
    "29.97",
    "30",
    "50",
    "59.94",
    "60",
}


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
    quality_480p: float = 26.0
    quality_720p: float = 25.0
    quality_1080p: float = 24.0
    quality_2160p: float = 22.0
    selective_decomb: bool = True
    content_kind: str = "unknown"
    nlmeans_preset: str | None = None
    nlmeans_tune: str = "none"
    audio_track: int = 1
    audio_preference: str = "default"
    audio_primary_layout: str = ""
    audio_secondary_layout: str = ""
    audio_default_language: str = "default"
    audio_language: str = "default"
    audio_selection: str = "all_matching"
    additional_audio: str = "selected_only"
    compatibility_audio_bitrate: int = 256
    audio_bitrate_stereo: int = 256
    audio_bitrate_2_1: int = 320
    audio_bitrate_5_1: int = 512
    audio_bitrate_7_1: int = 640
    stereo_first: bool = True
    retain_subtitles: bool = True
    subtitle_language: str = "eng"
    subtitle_selection: str = "all_matching"
    subtitle_default: str = "none"
    resolution_policy: str = "source"
    frame_rate_policy: str = "source"


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
    width: int
    height: int
    field_order: str


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
    width: int | None = None
    height: int | None = None
    field_order: str = "unknown"

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


def _validate_subtitle_profile(profile: HandBrakeProfile) -> None:
    if not re.fullmatch(r"[a-z]{3}(?:,[a-z]{3})*", profile.subtitle_language):
        raise HandBrakeError("Subtitle language codes must be ISO 639-2 values")
    if profile.subtitle_selection not in {
        "all_matching",
        "first_matching",
        "all",
        "none",
    }:
        raise HandBrakeError("Unsupported subtitle selection policy")
    if profile.subtitle_default not in {"none", "first"}:
        raise HandBrakeError("Unsupported subtitle default policy")


def _validate_video_policies(profile: HandBrakeProfile) -> None:
    if profile.resolution_policy not in _RESOLUTION_POLICIES:
        raise HandBrakeError("Unsupported resolution policy")
    if profile.frame_rate_policy not in _FRAME_RATE_POLICIES:
        raise HandBrakeError("Unsupported frame-rate policy")


def _validate_audio_language(profile: HandBrakeProfile) -> None:
    if profile.audio_default_language != "default" and not re.fullmatch(
        r"[a-z]{3}", profile.audio_default_language
    ):
        raise HandBrakeError("Default audio language must be one ISO 639-2 code")
    if profile.audio_language != "default" and not re.fullmatch(
        r"[a-z]{3}(?:,[a-z]{3})*", profile.audio_language
    ):
        raise HandBrakeError("Audio language codes must be ISO 639-2 values")
    if profile.audio_selection not in {"all_matching", "first_matching", "all"}:
        raise HandBrakeError("Unsupported audio language selection policy")


def _configured_audio_layouts(profile: HandBrakeProfile) -> tuple[str, ...]:
    """Return explicit output order, preserving legacy profile behavior."""

    if profile.audio_primary_layout:
        layouts = (profile.audio_primary_layout, profile.audio_secondary_layout)
        return tuple(layout for layout in layouts if layout != "none")
    legacy_pair = ("stereo", profile.audio_preference)
    return legacy_pair if profile.stereo_first else tuple(reversed(legacy_pair))


def _source_audio_preference(profile: HandBrakeProfile) -> str:
    """Choose a source with enough channels for every requested output."""

    if not profile.audio_primary_layout:
        return profile.audio_preference
    layouts = _configured_audio_layouts(profile)
    if "highest" in layouts:
        return "highest"
    ranked = {"default": 0, "stereo": 2, "2.1": 3, "5.1": 6, "7.1": 8}
    return max(layouts, key=lambda layout: ranked[layout])


def _validate_audio_profile(profile: HandBrakeProfile) -> None:
    if profile.audio_track <= 0:
        raise HandBrakeError("HandBrake audio track is one-based and must be positive")
    if profile.audio_preference not in _AUDIO_PREFERENCES:
        raise HandBrakeError("Unsupported source audio preference")
    supported_layouts = {"default", "stereo", "2.1", "5.1", "7.1", "highest"}
    if (
        profile.audio_primary_layout
        and profile.audio_primary_layout not in supported_layouts
    ):
        raise HandBrakeError("Unsupported primary audio layout")
    if profile.audio_secondary_layout and profile.audio_secondary_layout not in (
        supported_layouts | {"none"}
    ):
        raise HandBrakeError("Unsupported secondary audio layout")
    configured_layouts = _configured_audio_layouts(profile)
    if not configured_layouts or len(set(configured_layouts)) != len(
        configured_layouts
    ):
        raise HandBrakeError("Audio output layouts must be present and distinct")
    if profile.additional_audio not in {"selected_only", "all"}:
        raise HandBrakeError("Unsupported additional audio policy")
    if not 64 <= profile.compatibility_audio_bitrate <= 512:
        raise HandBrakeError("Compatibility audio bitrate is outside safe bounds")
    if any(
        not 64 <= bitrate <= 1024
        for bitrate in (
            profile.audio_bitrate_stereo,
            profile.audio_bitrate_2_1,
            profile.audio_bitrate_5_1,
            profile.audio_bitrate_7_1,
        )
    ):
        raise HandBrakeError("Audio layout bitrate is outside safe bounds")


def _validate_profile(profile: HandBrakeProfile) -> None:
    presets = _ENCODER_PRESETS.get(profile.encoder)
    if presets is None:
        raise HandBrakeError("Unsupported HandBrake video encoder")
    if profile.encoder_preset not in presets:
        raise HandBrakeError("Unsupported preset for the selected video encoder")
    if any(
        not 0 <= quality <= 51
        for quality in (
            profile.quality,
            profile.quality_480p,
            profile.quality_720p,
            profile.quality_1080p,
            profile.quality_2160p,
        )
    ):
        raise HandBrakeError("HandBrake quality must be between 0 and 51")
    _validate_nlmeans(profile)
    _validate_audio_profile(profile)
    _validate_subtitle_profile(profile)
    _validate_video_policies(profile)
    _validate_audio_language(profile)


def validate_handbrake_profile(profile: HandBrakeProfile) -> HandBrakeProfile:
    """Validate a reusable profile without requiring media or destination paths."""

    _validate_profile(profile)
    return profile


def encoder_requires_vcn(encoder: str) -> bool:
    """Return whether an encoder requires AMD's explicit VCN availability signal."""

    return encoder in _AMD_ENCODERS


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


def _audio_options(
    profile: HandBrakeProfile,
    source_audio_track_count: int | None,
    audio_track_indices: tuple[int, ...] | None = None,
) -> tuple[str, str, str, str]:
    if profile.additional_audio == "all":
        if source_audio_track_count is None or source_audio_track_count < 1:
            raise HandBrakeError("All-audio retention requires source audio metadata")
        if profile.audio_track > source_audio_track_count:
            raise HandBrakeError("Preferred audio track is absent from the source")
    layout_options = {
        "stereo": ("av_aac", "dpl2", str(profile.audio_bitrate_stereo)),
        "2.1": ("av_aac", "2point1", str(profile.audio_bitrate_2_1)),
        "5.1": ("av_aac", "5point1", str(profile.audio_bitrate_5_1)),
        "7.1": ("av_aac", "7point1", str(profile.audio_bitrate_7_1)),
        "default": ("copy", "none", "0"),
        "highest": ("copy", "none", "0"),
    }
    layouts = _configured_audio_layouts(profile)
    audio_tracks = [profile.audio_track for _ in layouts]
    layout_specs = [layout_options[layout] for layout in layouts]
    audio_encoders = [spec[0] for spec in layout_specs]
    audio_mixdowns = [spec[1] for spec in layout_specs]
    audio_bitrates = [spec[2] for spec in layout_specs]
    if profile.additional_audio == "all":
        available_tracks = audio_track_indices or tuple(
            range(1, source_audio_track_count + 1)
        )
        additional_tracks = [
            track for track in available_tracks if track != profile.audio_track
        ]
        audio_tracks.extend(additional_tracks)
        audio_encoders.extend("copy" for _ in additional_tracks)
        audio_mixdowns.extend("none" for _ in additional_tracks)
        audio_bitrates.extend("0" for _ in additional_tracks)
    return (
        ",".join(str(track) for track in audio_tracks),
        ",".join(audio_encoders),
        ",".join(audio_mixdowns),
        ",".join(audio_bitrates),
    )


def _subtitle_options(profile: HandBrakeProfile) -> tuple[str, ...]:
    if not profile.retain_subtitles or profile.subtitle_selection == "none":
        return ("--subtitle", "none")
    if profile.subtitle_selection == "all":
        options = ("--all-subtitles",)
    else:
        selector = (
            "--all-subtitles"
            if profile.subtitle_selection == "all_matching"
            else "--first-subtitle"
        )
        options = ("--subtitle-lang-list", profile.subtitle_language, selector)
    if profile.subtitle_default == "first":
        options += ("--subtitle-default", "1")
    return options


def _video_options(profile: HandBrakeProfile) -> tuple[str, ...]:
    options: list[str] = []
    max_height = _RESOLUTION_POLICIES[profile.resolution_policy]
    if max_height is not None:
        options.extend(("--maxHeight", str(max_height)))
    if profile.frame_rate_policy == "vfr":
        options.append("--vfr")
    elif profile.frame_rate_policy != "source":
        options.extend(("--rate", profile.frame_rate_policy, "--pfr"))
    return tuple(options)


def _quality_for_profile(profile: HandBrakeProfile) -> float:
    return {
        "source": profile.quality,
        "480p": profile.quality_480p,
        "720p": profile.quality_720p,
        "1080p": profile.quality_1080p,
        "2160p": profile.quality_2160p,
    }[profile.resolution_policy]


def _quality_for_source_height(profile: HandBrakeProfile, height: int | None) -> float:
    if height is None:
        return profile.quality
    if height <= 480:
        return profile.quality_480p
    if height <= 720:
        return profile.quality_720p
    if height <= 1080:
        return profile.quality_1080p
    return profile.quality_2160p


def build_handbrake_command(
    executable: Path,
    job: HandBrakeJob,
    *,
    output_path: Path | None = None,
    source_audio_track_count: int | None = None,
    audio_track_indices: tuple[int, ...] | None = None,
    source_height: int | None = None,
) -> tuple[str, ...]:
    validate_handbrake_job(job)
    if not executable.is_file():
        raise HandBrakeError("HandBrakeCLI executable was not found")
    partial = output_path or partial_output_path(job)
    if partial.exists():
        raise HandBrakeError("Partial transcode exists; refusing overwrite")

    profile = job.profile
    audio_tracks, audio_encoders, audio_mixdowns, audio_bitrates = _audio_options(
        profile, source_audio_track_count, audio_track_indices
    )
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
        str(
            _quality_for_source_height(profile, source_height)
            if profile.resolution_policy == "source"
            else _quality_for_profile(profile)
        ),
        *_video_options(profile),
        "--audio",
        audio_tracks,
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
    command.extend(_subtitle_options(profile))
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
                (
                    "format=duration,size:"
                    "stream=codec_type,codec_name,width,height,field_order:"
                    "stream_disposition=attached_pic"
                ),
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
        if isinstance(stream, dict)
        and stream.get("codec_type") == "video"
        and not (
            isinstance(stream.get("disposition"), dict)
            and stream["disposition"].get("attached_pic") == 1
        )
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
    try:
        width = int(videos[0]["width"])
        height = int(videos[0]["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HandBrakeError("Post-transcode video dimensions are invalid") from exc
    if width <= 0 or height <= 0:
        raise HandBrakeError("Post-transcode video dimensions are invalid")
    return VerifiedMedia(
        duration_seconds=duration,
        size_bytes=size,
        video_codec=str(videos[0].get("codec_name") or ""),
        audio_streams=len(audio),
        subtitle_streams=len(subtitles),
        width=width,
        height=height,
        field_order=str(videos[0].get("field_order") or "unknown").lower(),
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
    if job.profile.encoder in _AMD_ENCODERS and not capabilities.vcn_available:
        raise HandBrakeError("AMD VCN is not available")
    if job.profile.encoder not in capabilities.encoders:
        raise HandBrakeError("Requested HandBrake encoder is not available")


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


def recover_handbrake_partial(
    ffprobe: Path,
    job: HandBrakeJob,
    run_dir: Path,
    *,
    confirm_transcode: bool,
) -> HandBrakeResult:
    """Verify and promote one exact preserved partial without re-encoding."""

    if not confirm_transcode:
        raise HandBrakeError("Partial recovery requires explicit confirmation")
    validate_handbrake_job(job)
    if not run_dir.is_dir():
        raise HandBrakeError("Transcode run-log directory does not exist")
    partial = partial_output_path(job)
    verified = _verify_expected_output(ffprobe, job, partial)
    event_log, process_log = _new_log_paths(run_dir, job)
    process_log.write_text(
        "Existing partial passed read-only verification and was promoted.\n",
        encoding="utf-8",
    )
    _write_event(
        event_log,
        {
            "at": datetime.now(UTC).isoformat(),
            "event": "partial-recovered",
            **job.safe_plan(),
        },
    )
    partial.rename(job.destination)
    return HandBrakeResult(
        media_id=job.media_id,
        encoder=job.profile.encoder,
        output_bytes=verified.size_bytes,
        duration_seconds=verified.duration_seconds,
        video_codec=verified.video_codec,
        audio_streams=verified.audio_streams,
        subtitle_streams=verified.subtitle_streams,
        process_log=process_log,
        event_log=event_log,
        width=verified.width,
        height=verified.height,
        field_order=verified.field_order,
    )


def select_audio_track(preference: str, channel_counts: tuple[int, ...]) -> int:
    """Resolve a reusable layout preference to a one-based HandBrake audio track."""

    if preference not in _AUDIO_PREFERENCES or not channel_counts:
        raise HandBrakeError("Source audio preference cannot be resolved")
    if any(value <= 0 for value in channel_counts):
        raise HandBrakeError("Source audio channel metadata is invalid")
    if preference == "default":
        return 1
    if preference == "highest":
        return max(range(len(channel_counts)), key=channel_counts.__getitem__) + 1
    target = _AUDIO_PREFERENCES[preference]
    assert target is not None
    return (
        min(
            range(len(channel_counts)),
            key=lambda index: (
                abs(channel_counts[index] - target),
                -channel_counts[index],
                index,
            ),
        )
        + 1
    )


def _source_audio_tracks(
    ffprobe: Path, source: Path
) -> tuple[tuple[int, str | None, int], ...]:
    if not ffprobe.is_file():
        raise HandBrakeError("FFprobe executable was not found")
    try:
        completed = subprocess.run(
            (
                str(ffprobe.resolve()),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=channels:stream_tags=language",
                "-of",
                "json",
                str(source.resolve()),
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        payload = json.loads(completed.stdout)
        tracks = tuple(
            (index + 1, stream.get("tags", {}).get("language"), int(stream["channels"]))
            for index, stream in enumerate(payload["streams"])
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise HandBrakeError("Source audio inspection failed safely") from exc
    if completed.returncode != 0 or not tracks:
        raise HandBrakeError("Source audio inspection returned no usable tracks")
    return tracks


def _source_audio_channels(ffprobe: Path, source: Path) -> tuple[int, ...]:
    """Compatibility helper returning only channel counts."""

    return tuple(channels for _, _, channels in _source_audio_tracks(ffprobe, source))


def _source_video_height(ffprobe: Path, source: Path) -> int:
    if not ffprobe.is_file():
        raise HandBrakeError("FFprobe executable was not found")
    try:
        completed = subprocess.run(
            (
                str(ffprobe.resolve()),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=height",
                "-of",
                "json",
                str(source.resolve()),
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        payload = json.loads(completed.stdout)
        height = int(payload["streams"][0]["height"])
    except (
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        IndexError,
    ) as exc:
        raise HandBrakeError("Source video inspection failed safely") from exc
    if completed.returncode != 0 or height <= 0:
        raise HandBrakeError("Source video inspection returned no usable height")
    return height


def source_video_height(ffprobe: Path, source: Path) -> int:
    """Return the verified source height for profile selection."""

    return _source_video_height(ffprobe, source)


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

    effective_job = job
    source_audio_channels = None
    source_audio_track_indices = None
    source_height = None
    if job.profile.resolution_policy == "source":
        source_height = _source_video_height(ffprobe, job.source)
    source_audio_preference = _source_audio_preference(job.profile)
    if (
        source_audio_preference != "default"
        or job.profile.additional_audio == "all"
        or job.profile.audio_language != "default"
        or job.profile.audio_default_language != "default"
    ):
        source_audio_tracks = _source_audio_tracks(ffprobe, job.source)
        source_audio_channels = tuple(
            channels for _, _, channels in source_audio_tracks
        )
        if (
            job.profile.audio_language != "default"
            or job.profile.audio_default_language != "default"
        ):
            languages = set(
                job.profile.audio_language.split(",")
                if job.profile.audio_language != "default"
                else ()
            )
            if job.profile.audio_default_language != "default":
                languages.add(job.profile.audio_default_language)
            matching = tuple(
                index
                for index, language, _ in source_audio_tracks
                if language in languages
            )
            if not matching:
                raise HandBrakeError("Requested audio language was not found")
            default_matches = tuple(
                index
                for index in matching
                if source_audio_tracks[index - 1][1]
                == job.profile.audio_default_language
            )
            ordered_matching = default_matches + tuple(
                index for index in matching if index not in default_matches
            )
            matching_channels = tuple(
                source_audio_channels[index - 1] for index in ordered_matching
            )
            preferred_relative = (
                0
                if source_audio_preference == "default"
                else select_audio_track(source_audio_preference, matching_channels) - 1
            )
            effective_job = replace(
                effective_job,
                profile=replace(
                    effective_job.profile,
                    audio_track=ordered_matching[preferred_relative],
                ),
            )
            # With an explicit language list, retain only tracks in those
            # languages; this keeps unrelated foreign/commentary tracks out.
            source_audio_track_indices = ordered_matching
            if job.profile.audio_selection == "first_matching":
                source_audio_track_indices = ordered_matching[:1]
    if source_audio_preference != "default":
        resolved_track = select_audio_track(
            source_audio_preference,
            source_audio_channels or (),
        )
        effective_job = replace(
            effective_job,
            profile=replace(effective_job.profile, audio_track=resolved_track),
        )
    partial = partial_output_path(effective_job)
    command = build_handbrake_command(
        executable,
        effective_job,
        output_path=partial,
        source_audio_track_count=(
            len(source_audio_channels) if source_audio_channels is not None else None
        ),
        audio_track_indices=source_audio_track_indices,
        source_height=source_height,
    )
    event_log, process_log = _new_log_paths(run_dir, effective_job)

    _write_event(
        event_log,
        {
            "at": datetime.now(UTC).isoformat(),
            "event": "started",
            **effective_job.safe_plan(),
        },
    )
    completed = _run_handbrake_process(
        command,
        event_log,
        effective_job,
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
    verified = _verify_expected_output(ffprobe, effective_job, partial)
    partial.rename(effective_job.destination)

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
        width=verified.width,
        height=verified.height,
        field_order=verified.field_order,
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
