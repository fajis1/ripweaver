# Third-Party Notices

RipWeaver is distributed under the MIT License in `LICENSE`. It is derived
from and incorporates work from the projects acknowledged below. Copyright
notices must be retained in source and binary distributions.

## MKV Episode Matcher

Original project: <https://github.com/Jsakkos/mkv-episode-matcher>

Copyright (c) 2025 Jonathan Sakkos

Licensed under the MIT License. The complete license text is in this
distribution's `LICENSE` file.

## Riplex

Selected disc-scanning, runtime-matching, deduplication, Plex/Jellyfin
organization, MakeMKV, and disc-orchestration behavior was informed by or
adapted from Riplex: <https://github.com/AnyCredit5518/riplex>

MIT License

Copyright (c) 2026 AnyCredit5518

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Bundled dependencies

The executable build includes Python and JavaScript dependencies under their
respective licenses. PyInstaller release builds copy installed distribution
metadata recursively so their copyright and license files accompany the
binary. Source dependency identities and exact versions are locked in
`uv.lock` and `mkv_episode_matcher/frontend/package-lock.json`.

Notable runtime components include Faster Whisper, CTranslate2, FastAPI,
Uvicorn, React, OpenSubtitles.com client libraries, librosa, RapidFuzz,
SoundFile, Pydantic, Rich, Typer, and their transitive dependencies. These
projects are not affiliated with and do not endorse RipWeaver.

## External applications and services

MakeMKV, HandBrake, FFmpeg/FFprobe, Jellyfin, OpenSubtitles.com, TMDB, and
Gemini are independent external applications or services. RipWeaver invokes
user-installed tools or user-configured services; they are not relicensed by
RipWeaver. See `docs/ATTRIBUTIONS.md` for service notices and official links.
