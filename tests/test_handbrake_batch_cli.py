import json
from unittest.mock import patch

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.handbrake import HandBrakeCapabilities


@patch("mkv_episode_matcher.media.handbrake.inspect_handbrake_capabilities")
@patch("mkv_episode_matcher.media.handbrake.resolve_handbrake_path")
def test_cli_writes_path_redacted_batch_without_creating_staging(
    mock_resolve,
    mock_capabilities,
    tmp_path,
):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "output"
    source_root.mkdir()
    output_root.mkdir()
    source = source_root / "disc-title000.mkv"
    source.write_bytes(b"x" * 100)
    executable = tmp_path / "HandBrakeCLI.exe"
    executable.touch()
    mock_resolve.return_value = executable
    mock_capabilities.return_value = HandBrakeCapabilities(True, ("vce_h265",))
    organization = tmp_path / "organization.json"
    organization.write_text(
        json.dumps({
            "mode": "tv-organization-plan",
            "item_count": 1,
            "proposed_count": 1,
            "review_count": 0,
            "items": [
                {
                    "file_id": "disc-title000",
                    "relative_destination": (
                        "Test Series/Season 03/Test Series - S03E01 - First.mkv"
                    ),
                    "status": "proposed",
                }
            ],
        }),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"

    result = CliRunner().invoke(
        app,
        [
            "plan-handbrake-batch",
            str(organization),
            "--source",
            f"disc-title000={source}",
            "--output-root",
            str(output_root),
            "--reserve-gib",
            "0",
            "--content-kind",
            "live_action",
            "--nlmeans",
            "ultralight",
            "--nlmeans-tune",
            "film",
            "--manifest-out",
            str(manifest),
        ],
    )

    assert result.exit_code == 0
    assert "NO TRANSCODE" in result.stdout
    assert not (output_root / "encoded-staging").exists()
    serialized = manifest.read_text(encoding="utf-8")
    assert str(source_root) not in serialized
    assert str(output_root) not in serialized
    profile = json.loads(serialized)["profile"]
    assert profile["content_kind"] == "live_action"
    assert profile["nlmeans_preset"] == "ultralight"
    assert profile["nlmeans_tune"] == "film"
    assert profile["stereo_first"] is True
