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
    HandBrakeResult,
    VerifiedMedia,
    _is_process_interruption,
    _redact,
    _source_audio_channels,
    build_handbrake_command,
    execute_handbrake_job,
    inspect_handbrake_capabilities,
    partial_output_path,
    recover_handbrake_completed_output,
    recover_handbrake_partial,
    select_audio_track,
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
    assert command[command.index("--subtitle-lang-list") + 1] == "eng"
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


def test_command_uses_layout_specific_bitrates(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(
            audio_preference="5.1",
            audio_bitrate_stereo=192,
            audio_bitrate_5_1=576,
            stereo_first=False,
        ),
    )

    command = build_handbrake_command(handbrake, job)

    assert command[command.index("--audio") + 1] == "1,1"
    assert command[command.index("--aencoder") + 1] == "av_aac,av_aac"
    assert command[command.index("--mixdown") + 1] == "5point1,dpl2"
    assert command[command.index("--ab") + 1] == "576,192"


def test_profile_rejects_invalid_layout_bitrate(tmp_path):
    with pytest.raises(HandBrakeError, match="layout bitrate"):
        validate_handbrake_job(
            _job(tmp_path, profile=HandBrakeProfile(audio_bitrate_7_1=1056))
        )


@pytest.mark.parametrize(
    ("primary", "secondary", "encoders", "mixdowns", "bitrates"),
    [
        ("stereo", "5.1", "av_aac,av_aac", "dpl2,5point1", "192,576"),
        ("highest", "stereo", "copy,av_aac", "none,dpl2", "0,192"),
    ],
)
def test_command_respects_explicit_primary_and_secondary_audio_order(
    tmp_path, primary, secondary, encoders, mixdowns, bitrates
):
    handbrake, _ = _tools(tmp_path)
    profile = HandBrakeProfile(
        audio_primary_layout=primary,
        audio_secondary_layout=secondary,
        audio_bitrate_stereo=192,
        audio_bitrate_5_1=576,
    )

    command = build_handbrake_command(handbrake, _job(tmp_path, profile=profile))

    assert command[command.index("--aencoder") + 1] == encoders
    assert command[command.index("--mixdown") + 1] == mixdowns
    assert command[command.index("--ab") + 1] == bitrates


def test_profile_rejects_duplicate_explicit_audio_layouts(tmp_path):
    with pytest.raises(HandBrakeError, match="distinct"):
        validate_handbrake_job(
            _job(
                tmp_path,
                profile=HandBrakeProfile(
                    audio_primary_layout="stereo",
                    audio_secondary_layout="stereo",
                ),
            )
        )


def test_command_can_select_all_english_subtitles_and_mark_first_default(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(subtitle_default="first"),
    )

    command = build_handbrake_command(handbrake, job)

    assert command[command.index("--subtitle-lang-list") + 1] == "eng"
    assert "--all-subtitles" in command
    assert command[command.index("--subtitle-default") + 1] == "1"


def test_command_can_limit_resolution_and_set_frame_rate(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(resolution_policy="720p", frame_rate_policy="23.976"),
    )

    command = build_handbrake_command(handbrake, job)

    assert command[command.index("--maxHeight") + 1] == "720"
    assert command[command.index("--rate") + 1] == "23.976"
    assert "--pfr" in command
    assert "--vfr" not in command


def test_resolution_policy_selects_matching_quality_value(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(resolution_policy="1080p", quality_1080p=23.5),
    )

    command = build_handbrake_command(handbrake, job)

    assert command[command.index("--quality") + 1] == "23.5"


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("first_matching", "--first-subtitle"),
        ("all", "--all-subtitles"),
        ("none", "none"),
    ],
)
def test_subtitle_selection_policies(tmp_path, selection, expected):
    handbrake, _ = _tools(tmp_path)
    job = _job(tmp_path, profile=HandBrakeProfile(subtitle_selection=selection))
    command = build_handbrake_command(handbrake, job)

    if expected == "none":
        assert command[command.index("--subtitle") + 1] == "none"
    else:
        assert expected in command


def test_command_can_keep_preferred_pair_followed_by_every_other_audio(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(
        tmp_path,
        profile=HandBrakeProfile(
            audio_track=2,
            additional_audio="all",
        ),
    )

    command = build_handbrake_command(handbrake, job, source_audio_track_count=4)

    assert command[command.index("--audio") + 1] == "2,2,1,3,4"
    assert command[command.index("--aencoder") + 1] == "av_aac,copy,copy,copy,copy"
    assert command[command.index("--mixdown") + 1] == "dpl2,none,none,none,none"
    assert command[command.index("--ab") + 1] == "256,0,0,0,0"


def test_all_audio_requires_known_source_track_count(tmp_path):
    handbrake, _ = _tools(tmp_path)
    job = _job(tmp_path, profile=HandBrakeProfile(additional_audio="all"))

    with pytest.raises(HandBrakeError, match="requires source audio metadata"):
        build_handbrake_command(handbrake, job)


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


def test_rejects_unknown_encoder(tmp_path):
    with pytest.raises(HandBrakeError, match="video encoder"):
        validate_handbrake_job(
            _job(tmp_path, profile=HandBrakeProfile(encoder="not-an-encoder"))
        )


@pytest.mark.parametrize(
    ("preference", "channels", "expected"),
    [
        ("default", (2, 6, 8), 1),
        ("stereo", (6, 2), 2),
        ("2.1", (2, 3, 6), 2),
        ("5.1", (2, 6, 8), 2),
        ("7.1", (2, 6, 8), 3),
        ("highest", (2, 8, 6), 2),
        ("5.1", (2, 8), 2),
    ],
)
def test_audio_layout_preference_resolves_per_source(preference, channels, expected):
    assert select_audio_track(preference, channels) == expected


def test_audio_layout_preference_rejects_missing_metadata():
    with pytest.raises(HandBrakeError, match="cannot be resolved"):
        select_audio_track("5.1", ())


def test_source_audio_channels_uses_fixed_ffprobe_metadata_query(tmp_path, monkeypatch):
    ffprobe = tmp_path / "ffprobe.exe"
    source = tmp_path / "source.mkv"
    ffprobe.write_bytes(b"synthetic")
    source.write_bytes(b"synthetic")
    run = Mock(
        return_value=subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=json.dumps({"streams": [{"channels": 2}, {"channels": 6}]}),
            stderr="",
        )
    )
    monkeypatch.setattr("mkv_episode_matcher.media.handbrake.subprocess.run", run)

    assert _source_audio_channels(ffprobe, source) == (2, 6)
    command = run.call_args.args[0]
    assert command[1:-1] == (
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=channels:stream_tags=language",
        "-of",
        "json",
    )
    assert command[-1] == str(source.resolve())


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
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "width": 640,
                        "height": 360,
                        "disposition": {"attached_pic": 1},
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


def test_recovery_promotes_verified_existing_partial_without_handbrake(
    tmp_path, monkeypatch
):
    _handbrake, ffprobe = _tools(tmp_path)
    job = _job(tmp_path)
    partial = partial_output_path(job)
    partial.write_bytes(b"complete-encoded-output")
    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake._probe_media",
        lambda _ffprobe, _partial: VerifiedMedia(
            duration_seconds=1200,
            size_bytes=partial.stat().st_size,
            video_codec="hevc",
            audio_streams=2,
            subtitle_streams=1,
            width=1440,
            height=1080,
            field_order="progressive",
        ),
    )

    result = recover_handbrake_partial(ffprobe, job, tmp_path, confirm_transcode=True)

    assert not partial.exists()
    assert job.destination.read_bytes() == b"complete-encoded-output"
    assert result.output_bytes == len(b"complete-encoded-output")
    assert "partial-recovered" in result.event_log.read_text(encoding="utf-8")


def test_recovery_adopts_exact_verified_completed_output_without_transcode(
    tmp_path, monkeypatch
):
    _handbrake, ffprobe = _tools(tmp_path)
    job = _job(tmp_path, attempt_number=2)
    job.destination.write_bytes(b"complete-encoded-output")
    run_dir = tmp_path / "run-attempt-002"
    run_dir.mkdir()
    verified = VerifiedMedia(
        duration_seconds=1200,
        size_bytes=job.destination.stat().st_size,
        video_codec="hevc",
        audio_streams=2,
        subtitle_streams=1,
        width=1440,
        height=1080,
        field_order="progressive",
    )
    result = HandBrakeResult(
        media_id=job.media_id,
        encoder=job.profile.encoder,
        output_bytes=verified.size_bytes,
        duration_seconds=verified.duration_seconds,
        video_codec=verified.video_codec,
        audio_streams=verified.audio_streams,
        subtitle_streams=verified.subtitle_streams,
        process_log=run_dir / "process.log",
        event_log=run_dir / "events.jsonl",
        width=verified.width,
        height=verified.height,
        field_order=verified.field_order,
    )
    event_log = run_dir / "handbrake-media-1-20260827T000000Z.jsonl"
    event_log.write_text(
        "\n".join((
            json.dumps({"event": "started", **job.safe_plan()}),
            json.dumps({"event": "verified-complete", **result.safe_report()}),
        ))
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake._probe_media",
        lambda _ffprobe, _destination: verified,
    )

    recovered = recover_handbrake_completed_output(
        ffprobe, job, run_dir, confirm_transcode=True
    )

    assert job.destination.read_bytes() == b"complete-encoded-output"
    assert recovered.output_bytes == verified.size_bytes
    assert recovered.event_log == event_log


def test_completed_output_recovery_refuses_changed_attempt_plan(tmp_path, monkeypatch):
    _handbrake, ffprobe = _tools(tmp_path)
    job = _job(tmp_path, attempt_number=2)
    job.destination.write_bytes(b"complete-encoded-output")
    run_dir = tmp_path / "run-attempt-002"
    run_dir.mkdir()
    changed_plan = job.safe_plan()
    changed_plan["attempt_number"] = 1
    event_log = run_dir / "handbrake-media-1-20260827T000000Z.jsonl"
    event_log.write_text(
        "\n".join((
            json.dumps({"event": "started", **changed_plan}),
            json.dumps({
                "event": "verified-complete",
                "mode": "handbrake-result",
                "media_id": job.media_id,
                "encoder": job.profile.encoder,
                "output_bytes": job.destination.stat().st_size,
                "duration_seconds": 1200,
                "video_codec": "hevc",
                "audio_streams": 2,
                "subtitle_streams": 1,
                "width": 1440,
                "height": 1080,
                "field_order": "progressive",
            }),
        ))
        + "\n",
        encoding="utf-8",
    )
    probe = Mock()
    monkeypatch.setattr("mkv_episode_matcher.media.handbrake._probe_media", probe)

    with pytest.raises(HandBrakeError, match="plan changed"):
        recover_handbrake_completed_output(
            ffprobe, job, run_dir, confirm_transcode=True
        )

    probe.assert_not_called()


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
        if "-show_entries" in command:
            return subprocess.CompletedProcess(
                command, 0, '{"streams":[{"height":480}]}', ""
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
