import json
from unittest.mock import patch

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app


@patch("mkv_episode_matcher.media.gemini_matcher.requests.post")
def test_build_unmatched_bundle_uses_saved_data_only(mock_post, tmp_path):
    transcript_path = tmp_path / "transcripts.json"
    catalog_path = tmp_path / "catalog.json"
    bundle_path = tmp_path / "private-bundle.json"
    report_path = tmp_path / "safe-plan.json"
    transcript_path.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-01-title-000",
                    "duration_seconds": 2940,
                    "windows": [
                        {
                            "start_seconds": 300,
                            "text": "The evil goblin created a magic mirror.",
                        }
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )
    catalog_path.write_text(
        json.dumps({
            "episodes": [
                {
                    "episode_id": "S04E02",
                    "season": 4,
                    "episode": 2,
                    "title": "The Snow Queen",
                    "overview": "A goblin mirror separates two friends.",
                    "runtime_seconds": 2940,
                }
            ]
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "build-unmatched-bundle",
            str(transcript_path),
            str(catalog_path),
            "--bundle-out",
            str(bundle_path),
            "--report-out",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    assert "No Whisper/TMDb/Gemini request" in result.stdout
    assert "evil goblin" in bundle_path.read_text(encoding="utf-8")
    assert "evil goblin" not in report_path.read_text(encoding="utf-8")
    mock_post.assert_not_called()


def test_build_unmatched_bundle_refuses_existing_output_before_writing(tmp_path):
    transcript_path = tmp_path / "transcripts.json"
    catalog_path = tmp_path / "catalog.json"
    bundle_path = tmp_path / "private-bundle.json"
    report_path = tmp_path / "safe-plan.json"
    transcript_path.write_text('{"files": []}', encoding="utf-8")
    catalog_path.write_text('{"episodes": []}', encoding="utf-8")
    report_path.write_text("preserve", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "build-unmatched-bundle",
            str(transcript_path),
            str(catalog_path),
            "--bundle-out",
            str(bundle_path),
            "--report-out",
            str(report_path),
        ],
    )

    assert result.exit_code == 1
    assert "refusing overwrite" in result.stdout
    assert not bundle_path.exists()
    assert report_path.read_text(encoding="utf-8") == "preserve"
