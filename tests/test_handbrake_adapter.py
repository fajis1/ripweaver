import ctypes
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.handbrake import (
    HandBrakeError,
    HandBrakeJob,
    HandBrakeProfile,
    _is_process_interruption,
    _redact,
    build_handbrake_command,
    execute_handbrake_job,
    inspect_handbrake_capabilities,
    partial_output_path,
    validate_handbrake_job,
)


def test_redact_removes_unlisted_windows_and_unc_paths(tmp_path):
    explicit_media = tmp_path / "episode.mkv"
    log = (
        f"input={explicit_media}\n"
        "metadata source D:\\old-rip\\disc\\title00.mkv\n"
        "compact-prefixD:\\second-old-rip\\title01.mkv\n"
        "network=\\\\media-server\\private-share\\episode.mkv\n"
        "safe diagnostic remains\n"
    )

    redacted = _redact(log, (explicit_media,))

    assert str(explicit_media) not in redacted
    assert "D:\\old-rip" not in redacted
    assert "D:\\second-old-rip" not in redacted
    assert "\\\\media-server" not in redacted
    assert "safe diagnostic remains" in redacted


@pytest.mark.parametrize(
    "code",
    [
        0x40010004,
        ctypes.c_int32(0x40010004).value,
        0xC000013A,
        ctypes.c_int32(0xC000013A).value,
    ],
)
def test_windows_interrupt_codes_are_classified_for_all_signages(code):
    assert _is_process_interruption(code)


@pytest.mark.parametrize("code", [-1, -2, -15])
def test_posix_interrupt_codes_are_classified(code):
    assert _is_process_interruption(code)


def test_non_interrupt_codes_are_not_classified():
    assert not _is_process_interruption(1)
    assert not _is_process_interruption(255)


def _job(tmp_path: Path, **changes) -> HandBrakeJob:
    source_dir = tmp_path / "source"
    source_dir.mkdir(exist_ok=True)
    source = source_dir / "private-episode.mkv"
    source.touch()
    staging = tmp_path / "encoded-staging"
    staging.mkdir(exist_ok=True)
    values = {
        "media_id": "media-1",
        "source": source,
        "destination": staging / "episode.mkv",
    }
    values.update(changes)
    return HandBrakeJob(**values)


def _tools(tmp_path: Path) -> tuple[Path, Path]:
    handbrake = tmp_path / "HandBrakeCLI.exe"
    handbrake.touch()
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.touch()
    return handbrake, ffprobe


def test_plan_refuses_existing_destination_and_source_folder(tmp_path):
    job = _job(tmp_path)
    job.destination.touch()
    with pytest.raises(HandBrakeError, match="refusing overwrite"):
        validate_handbrake_job(job)

    same_folder = HandBrakeJob(
        "media-1",
        job.source,
        job.source.with_name("encoded.mkv"),
    )
    with pytest.raises(HandBrakeError, match="separate staging"):
        validate_handbrake_job(same_folder)


def test_command_forces_vcn_and_preserves_surround_compatibility(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        sample_start_seconds=300,
        sample_duration_seconds=60,
    )

    command = build_handbrake_command(handbrake, job)

    assert command[command.index("--encoder") + 1] == "vce_h265"
    assert command[command.index("--encoder-preset") + 1] == "quality"
    assert command[command.index("--audio") + 1] == "1,1"
    assert command[command.index("--aencoder") + 1] == "av_aac,copy"
    assert command[command.index("--mixdown") + 1] == "dpl2,none"
    assert command[command.index("--ab") + 1] == "256,0"
    assert "--comb-detect" in command
    assert "--decomb" in command
    assert "--all-subtitles" in command
    assert "--preset-import-gui" not in command
    assert "--overwrite" not in command
    assert command[command.index("--start-at") + 1] == "seconds:300"
    assert command[command.index("--stop-at") + 1] == "seconds:60"


def test_command_adds_reviewed_nlmeans_preset_and_tune(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(
            quality=26,
            content_kind="live_action",
            nlmeans_preset="ultralight",
            nlmeans_tune="film",
        ),
    )

    command = build_handbrake_command(handbrake, job)

    assert "--nlmeans=ultralight" in command
    assert command[command.index("--nlmeans-tune") + 1] == "film"


def test_profile_rejects_invalid_or_unpaired_nlmeans_settings(tmp_path):
    with pytest.raises(HandBrakeError, match="preset"):
        validate_handbrake_job(
            _job(tmp_path, profile=HandBrakeProfile(nlmeans_preset="maximum"))
        )
    with pytest.raises(HandBrakeError, match="tune"):
        validate_handbrake_job(
            _job(
                tmp_path,
                profile=HandBrakeProfile(
                    content_kind="live_action",
                    nlmeans_preset="ultralight",
                    nlmeans_tune="television",
                ),
            )
        )
    with pytest.raises(HandBrakeError, match="requires"):
        validate_handbrake_job(
            _job(
                tmp_path,
                profile=HandBrakeProfile(
                    content_kind="live_action",
                    nlmeans_tune="film",
                ),
            )
        )


@pytest.mark.parametrize("content_kind", ["unknown", "animation"])
def test_profile_rejects_nlmeans_for_non_live_action_content(
    tmp_path,
    content_kind,
):
    with pytest.raises(HandBrakeError, match="live-action"):
        validate_handbrake_job(
            _job(
                tmp_path,
                profile=HandBrakeProfile(
                    content_kind=content_kind,
                    nlmeans_preset="ultralight",
                    nlmeans_tune="film",
                ),
            )
        )


def test_profile_rejects_unknown_content_kind(tmp_path):
    with pytest.raises(HandBrakeError, match="content kind"):
        validate_handbrake_job(
            _job(tmp_path, profile=HandBrakeProfile(content_kind="documentary"))
        )


def test_command_can_keep_original_surround_first(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(stereo_first=False),
    )

    command = build_handbrake_command(handbrake, job)

    assert command[command.index("--aencoder") + 1] == "copy,av_aac"
    assert command[command.index("--mixdown") + 1] == "none,dpl2"
    assert command[command.index("--ab") + 1] == "0,256"


def test_command_can_preserve_interlacing_when_profile_explicitly_disables_decomb(
    tmp_path,
):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(selective_decomb=False),
    )

    command = build_handbrake_command(handbrake, job)

    assert "--comb-detect" not in command
    assert "--decomb" not in command


def test_rejects_cpu_encoder_even_if_preset_name_suggests_amd(tmp_path):
    with pytest.raises(HandBrakeError, match="AMD VCN"):
        validate_handbrake_job(_job(tmp_path, profile=HandBrakeProfile(encoder="x264")))


def test_capability_parser_requires_explicit_vcn_signal(tmp_path, monkeypatch):
    handbrake, _ = _tools(tmp_path)
    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout="vce_h264 vce_h265 vce_h265_10bit",
                stderr="vcn: is available",
            )
        ),
    )

    capabilities = inspect_handbrake_capabilities(handbrake)

    assert capabilities.vcn_available
    assert capabilities.encoders == ("vce_h264", "vce_h265", "vce_h265_10bit")


def test_execution_requires_confirmation_without_starting_process(
    tmp_path,
    monkeypatch,
):
    handbrake, ffprobe = _tools(tmp_path)
    run = Mock()
    monkeypatch.setattr("mkv_episode_matcher.media.handbrake.subprocess.run", run)

    with pytest.raises(HandBrakeError, match="explicit confirmation"):
        execute_handbrake_job(
            handbrake,
            ffprobe,
            _job(tmp_path),
            tmp_path,
        )

    run.assert_not_called()


def test_success_promotes_only_verified_partial_and_redacts_logs(
    tmp_path,
    monkeypatch,
):
    handbrake, ffprobe = _tools(tmp_path)
    job = _job(
        tmp_path,
        sample_start_seconds=300,
        sample_duration_seconds=60,
    )
    partial = partial_output_path(job)

    def fake_run(command, **_kwargs):
        if command[-1] == "--help":
            return subprocess.CompletedProcess(
                command,
                0,
                "vce_h265",
                "vcn: is available",
            )
        if Path(command[0]) == handbrake.resolve():
            partial.write_bytes(b"encoded")
            return subprocess.CompletedProcess(
                command,
                0,
                f"encoded {job.source.resolve()} to {partial.resolve()}",
                "",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "format": {"duration": "60.0", "size": "7"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1920,
                        "height": 1080,
                        "field_order": "progressive",
                    },
                    {"codec_type": "audio", "codec_name": "ac3"},
                    {"codec_type": "audio", "codec_name": "aac"},
                    {"codec_type": "subtitle", "codec_name": "dvd_subtitle"},
                ],
            }),
            "",
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake.subprocess.run",
        fake_run,
    )

    result = execute_handbrake_job(
        handbrake,
        ffprobe,
        job,
        tmp_path,
        confirm_transcode=True,
        timeout_seconds=120,
    )

    assert job.destination.read_bytes() == b"encoded"
    assert not partial.exists()
    assert result.encoder == "vce_h265"
    assert result.audio_streams == 2
    assert result.height == 1080
    assert result.field_order == "progressive"
    process_log = result.process_log.read_text(encoding="utf-8")
    event_log = result.event_log.read_text(encoding="utf-8")
    assert str(job.source) not in process_log
    assert job.source.name not in process_log
    assert str(job.destination) not in event_log


def test_failed_encode_preserves_partial_and_never_creates_final(
    tmp_path,
    monkeypatch,
):
    handbrake, ffprobe = _tools(tmp_path)
    job = _job(tmp_path)
    partial = partial_output_path(job)

    def fake_run(command, **_kwargs):
        if command[-1] == "--help":
            return subprocess.CompletedProcess(
                command,
                0,
                "vce_h265",
                "vcn: is available",
            )
        partial.write_bytes(b"incomplete")
        return subprocess.CompletedProcess(command, 3, "", "failed")

    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake.subprocess.run",
        fake_run,
    )

    with pytest.raises(HandBrakeError, match="partial output was preserved"):
        execute_handbrake_job(
            handbrake,
            ffprobe,
            job,
            tmp_path,
            confirm_transcode=True,
        )

    assert partial.read_bytes() == b"incomplete"
    assert not job.destination.exists()


def test_plan_cli_is_path_free_and_never_starts_handbrake(tmp_path, monkeypatch):
    job = _job(tmp_path)
    run = Mock()
    monkeypatch.setattr("mkv_episode_matcher.media.handbrake.subprocess.run", run)

    result = CliRunner().invoke(
        app,
        [
            "plan-handbrake",
            str(job.source),
            str(job.destination),
            "--media-id",
            job.media_id,
            "--sample-start",
            "300",
            "--sample-duration",
            "60",
            "--content-kind",
            "live_action",
            "--nlmeans",
            "ultralight",
            "--nlmeans-tune",
            "film",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"mode": "handbrake-plan"' in result.output
    assert '"content_kind": "live_action"' in result.output
    assert '"nlmeans_preset": "ultralight"' in result.output
    assert '"nlmeans_tune": "film"' in result.output
    assert '"stereo_first": true' in result.output
    assert str(job.source) not in result.output
    assert job.source.name not in result.output
    run.assert_not_called()


def test_plan_cli_rejects_nlmeans_for_animation(tmp_path, monkeypatch):
    job = _job(tmp_path)
    run = Mock()
    monkeypatch.setattr("mkv_episode_matcher.media.handbrake.subprocess.run", run)

    result = CliRunner().invoke(
        app,
        [
            "plan-handbrake",
            str(job.source),
            str(job.destination),
            "--media-id",
            job.media_id,
            "--content-kind",
            "animation",
            "--nlmeans",
            "ultralight",
        ],
    )

    assert result.exit_code == 1
    assert "live-action" in result.output
    run.assert_not_called()


def test_execute_cli_refuses_without_confirmation(tmp_path, monkeypatch):
    job = _job(tmp_path)
    run = Mock()
    monkeypatch.setattr("mkv_episode_matcher.media.handbrake.subprocess.run", run)

    result = CliRunner().invoke(
        app,
        [
            "execute-handbrake",
            str(job.source),
            str(job.destination),
            "--media-id",
            job.media_id,
        ],
    )

    assert result.exit_code == 2
    assert "Execution refused" in result.output
    run.assert_not_called()
