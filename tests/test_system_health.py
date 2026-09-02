import json

from mkv_episode_matcher.backend.system_health import build_system_health
from mkv_episode_matcher.core.models import Config


def _touch_tools(tmp_path):
    tools = {}
    for field, filename in {
        "makemkv_path": "makemkvcon64.exe",
        "handbrake_path": "HandBrakeCLI.exe",
        "ffmpeg_path": "ffmpeg.exe",
        "ffprobe_path": "ffprobe.exe",
    }.items():
        path = tmp_path / filename
        path.write_bytes(b"synthetic")
        tools[field] = path
    return tools


def test_health_reports_complete_pipeline_ready_without_exposing_paths(tmp_path):
    tools = _touch_tools(tmp_path)
    rip = tmp_path / "private-rips"
    encoded = tmp_path / "private-encoded"
    library = tmp_path / "private-library"
    for directory in (rip, encoded, library):
        directory.mkdir()
    config = Config(
        **tools,
        rip_output_root=rip,
        transcode_output_root=encoded,
        jellyfin_tv_root=library,
    )

    result = build_system_health(
        config,
        credential_is_configured=lambda name: name == "opensubtitles-api",
    )

    assert result["status"] == "ready"
    assert result["ready_for"]["full_pipeline"] is True
    serialized = json.dumps(result)
    assert "private-rips" not in serialized
    assert "private-library" not in serialized


def test_health_explains_missing_tools_folders_and_provider():
    result = build_system_health(
        Config(),
        credential_is_configured=lambda _name: False,
    )

    assert result["status"] == "needs_setup"
    assert result["ready_for"] == {
        "launch": True,
        "disc_ripping": False,
        "transcoding": False,
        "media_analysis": False,
        "media_organization": False,
        "episode_identification": False,
        "full_pipeline": False,
    }
    items = {item["id"]: item for item in result["items"]}
    assert items["makemkv"]["status"] == "missing"
    assert items["makemkv"]["download_url"] == "https://www.makemkv.com/download/"
    assert items["tesseract"]["status"] == "optional"
    assert items["opensubtitles_api"]["status"] == "missing"
    assert items["opensubtitles_api"]["required"] is True
    assert items["tmdb_api"]["status"] == "optional"
    assert items["tmdb_api"]["download_url"] == (
        "https://www.themoviedb.org/settings/api"
    )
    assert items["gemini_primary_api"]["status"] == "optional"
    assert items["gemini_fallback_api"]["status"] == "optional"
    assert items["gemini_primary_api"]["download_url"] == (
        "https://aistudio.google.com/app/apikey"
    )
    assert items["media_library"]["message"].startswith("Choose at least one")
    assert "Jellyfin" not in items["media_library"]["message"]


def test_health_distinguishes_detected_from_saved_and_invalid(tmp_path):
    invalid = tmp_path / "missing" / "HandBrakeCLI.exe"
    detected_makemkv = tmp_path / "makemkvcon64.exe"
    detected_makemkv.write_bytes(b"synthetic")

    result = build_system_health(
        Config(handbrake_path=invalid, sub_provider="local"),
        discovered={"makemkv_path": str(detected_makemkv)},
    )
    items = {item["id"]: item for item in result["items"]}

    assert items["makemkv"]["status"] == "available"
    assert "detected" in items["makemkv"]["message"].lower()
    assert items["handbrake"]["status"] == "invalid"
    assert items["opensubtitles_api"]["status"] == "optional"
    assert items["opensubtitles_api"]["required"] is False
    assert result["ready_for"]["episode_identification"] is True
