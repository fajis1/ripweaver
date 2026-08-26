import json

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app


def test_cli_builds_saved_sequence_plan_without_media_access(tmp_path):
    transcripts = tmp_path / "transcripts.json"
    catalog = tmp_path / "catalog.json"
    report = tmp_path / "plan.json"
    transcripts.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-title000",
                    "duration_seconds": 1800,
                    "status": "collected",
                    "windows": [{"start_seconds": 120, "text": "Castle story"}],
                },
                {
                    "file_id": "disc-title001",
                    "duration_seconds": 1800,
                    "status": "collected",
                    "windows": [{"start_seconds": 120, "text": "Dragon story"}],
                },
            ]
        }),
        encoding="utf-8",
    )
    catalog.write_text(
        json.dumps({
            "episodes": [
                {
                    "episode_id": "S01E01",
                    "season": 1,
                    "episode": 1,
                    "title": "Apple",
                    "overview": "Apple story",
                    "runtime_seconds": 1800,
                },
                {
                    "episode_id": "S01E02",
                    "season": 1,
                    "episode": 2,
                    "title": "Castle",
                    "overview": "Castle story",
                    "runtime_seconds": 1800,
                },
                {
                    "episode_id": "S01E03",
                    "season": 1,
                    "episode": 3,
                    "title": "Dragon",
                    "overview": "Dragon story",
                    "runtime_seconds": 1800,
                },
            ]
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "plan-disc-sequences",
            str(transcripts),
            str(catalog),
            "--group",
            "disc=disc-title000,disc-title001",
            "--report-out",
            str(report),
        ],
    )

    assert result.exit_code == 0
    assert "No MKV" in result.stdout
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["groups"][0]["items"][0]["proposed_episode"] == "S01E02"
    assert "Castle story" not in report.read_text(encoding="utf-8")


def test_cli_requires_explicit_groups(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "plan-disc-sequences",
            str(source),
            str(source),
        ],
    )

    assert result.exit_code == 1
    assert "Provide 1-20 explicit sequence groups" in result.stdout
