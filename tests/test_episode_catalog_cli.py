from unittest.mock import patch

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry


@patch("mkv_episode_matcher.tmdb_client.fetch_aired_episode_catalog")
def test_fetch_aired_catalog_sends_only_reviewed_show_id(mock_fetch, tmp_path):
    output = tmp_path / "catalog.json"
    mock_fetch.return_value = (
        EpisodeCatalogEntry(
            "S01E01",
            1,
            1,
            "First Story",
            "A safe overview.",
            3000,
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "fetch-aired-catalog",
            "4603",
            "--report-out",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "No transcript, media path" in result.stdout
    assert "S01E01" in output.read_text(encoding="utf-8")
    mock_fetch.assert_called_once_with(4603)
