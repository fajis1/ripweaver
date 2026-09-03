from pathlib import Path

from mkv_episode_matcher.backend.routers.triage import (
    parse_triage_metadata,
)


def test_triage_parses_ancestor_of_season_and_skips_wrapper_folders(tmp_path: Path):
    triage_root = tmp_path / "Videos"
    show_dir = triage_root / "MKV_Matcher_Staging" / "Eureka" / "Season 01"
    show_dir.mkdir(parents=True)
    raw_mkv = show_dir / "EUREKA 1.2_t00.mkv"
    raw_mkv.write_bytes(b"dummy")

    meta = parse_triage_metadata(raw_mkv, triage_root)
    assert meta["series_name"] == "Eureka"
    assert meta["season_num"] == 1
    assert meta["ep_match"] is None
    assert meta["action"] == "identify"
    assert meta["is_extra"] is False


def test_triage_inherits_sibling_context_and_detects_extras(tmp_path: Path):
    triage_root = tmp_path / "Videos"
    conv_dir = triage_root / "MKV_Matcher_Staging" / "Tobeconverted" / "Season 01"
    conv_dir.mkdir(parents=True)

    main_movie = conv_dir / "PSYCH_THREE MOVIES_t00.mkv"
    main_movie.write_bytes(b"dummy")

    extra_title = conv_dir / "title_t04.mkv"
    extra_title.write_bytes(b"dummy")

    meta_main = parse_triage_metadata(main_movie, triage_root)
    assert "psych" in meta_main["series_name"].lower()
    assert meta_main["is_movie"] is True

    meta_extra = parse_triage_metadata(extra_title, triage_root)
    assert "psych" in meta_extra["series_name"].lower()


def test_triage_extracts_identified_episodes(tmp_path: Path):
    triage_root = tmp_path / "Videos"
    monk_dir = triage_root / "MKV_Matcher_Staging" / "Monk" / "Season 06"
    monk_dir.mkdir(parents=True)
    monk_file = monk_dir / "Monk - S06E04.mkv"
    monk_file.write_bytes(b"dummy")

    meta = parse_triage_metadata(monk_file, triage_root)
    assert meta["series_name"] == "Monk"
    assert meta["season_num"] == 6
    assert meta["ep_match"] == "S06E04"
