"""Keep every user-facing RipWeaver version on package metadata."""

from importlib.metadata import version
from pathlib import Path

import mkv_episode_matcher


def test_runtime_version_matches_installed_distribution() -> None:
    assert mkv_episode_matcher.__version__ == version("mkv-episode-matcher")


def test_legacy_setup_metadata_does_not_pin_a_stale_version() -> None:
    setup_cfg = Path("setup.cfg").read_text(encoding="utf-8")
    metadata = setup_cfg.split("[options]", maxsplit=1)[0]
    assert "version =" not in metadata


def test_source_fallback_is_explicitly_unknown() -> None:
    source = Path("mkv_episode_matcher/__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "0+unknown"' in source
