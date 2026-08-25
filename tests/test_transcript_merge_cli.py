import json

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app


def test_merge_transcript_reports_filters_without_media_access(tmp_path):
    source = tmp_path / "source.json"
    output = tmp_path / "merged.json"
    source.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "control-file",
                    "duration_seconds": 100,
                    "status": "collected",
                    "windows": [{"start_seconds": 0, "text": "Control dialogue"}],
                },
                {
                    "file_id": "theatre-file",
                    "duration_seconds": 200,
                    "status": "collected",
                    "windows": [{"start_seconds": 0, "text": "Theatre dialogue"}],
                },
            ]
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "merge-transcript-reports",
            str(source),
            "--report-out",
            str(output),
            "--file-id-prefix",
            "theatre-",
        ],
    )

    assert result.exit_code == 0
    assert "Merged 1 redacted file IDs" in result.stdout
    serialized = output.read_text(encoding="utf-8")
    assert "Theatre dialogue" in serialized
    assert "Control dialogue" not in serialized


def test_merge_transcript_reports_maps_reviewed_redacted_id(tmp_path):
    source = tmp_path / "source.json"
    output = tmp_path / "merged.json"
    source.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-03-title-001",
                    "duration_seconds": 200,
                    "status": "collected",
                    "windows": [{"start_seconds": 120, "text": "Story dialogue"}],
                }
            ]
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "merge-transcript-reports",
            str(source),
            "--report-out",
            str(output),
            "--map-file-id",
            "disc-03-title-001=theatre-disc03-title001",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["files"][0]["file_id"] == "theatre-disc03-title001"
