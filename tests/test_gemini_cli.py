import json
from unittest.mock import patch

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app


@patch("mkv_episode_matcher.media.gemini_matcher.requests.post")
def test_plan_gemini_unmatched_is_dry_run_and_dialogue_free(
    mock_post,
    tmp_path,
):
    bundle = tmp_path / "bundle.json"
    report = tmp_path / "report.json"
    bundle.write_text(
        json.dumps({
            "files": [
                {
                    "file_id": "disc-01-title-000",
                    "duration_seconds": 3000,
                    "transcript_excerpts": [
                        "A princess discovers a hidden dancing kingdom."
                    ],
                }
            ],
            "episodes": [
                {
                    "episode_id": "S06E03",
                    "season": 6,
                    "episode": 3,
                    "title": "The Dancing Princesses",
                    "overview": "Princesses secretly dance each night.",
                    "runtime_seconds": 3000,
                }
            ],
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "plan-gemini-unmatched",
            str(bundle),
            "--report-out",
            str(report),
            "--model",
            "gemini-test",
        ],
    )

    assert result.exit_code == 0
    assert "No Gemini/TMDb request" in result.stdout
    serialized = report.read_text(encoding="utf-8")
    assert "hidden dancing kingdom" not in serialized
    assert "S06E03" in serialized
    mock_post.assert_not_called()
