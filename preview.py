import json
from pathlib import Path
from mkv_episode_matcher.disc.rip_preview import build_rip_preview
from mkv_episode_matcher.disc.rip_manifest import MediaContext

contexts = {
    "disc-01": MediaContext(
        disc_id="disc-01",
        series_name="Short Circuit 2",
        season=None,
        disc_number=None,
        volume_number=None,
        content_hint="movie"
    )
}

preview = build_rip_preview([Path(".mkv-preflight/drive_2_Short_Circuit_2.json")], contexts)
print(json.dumps(preview.to_dict(), indent=2))
