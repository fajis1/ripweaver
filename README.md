# RipWeaver

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build](https://github.com/fajis1/ripweaver/actions/workflows/tests.yml/badge.svg)](https://github.com/fajis1/ripweaver/actions/workflows/tests.yml)

RipWeaver is a local web application for guarded disc ripping, episode and
movie identification, optional transcoding, and collision-refusing media
organization.

## 🚀 Quick Start

Follow these steps to get up and running in minutes.

### 1. Prerequisites

For the complete pipeline, install:

- [MakeMKV](https://www.makemkv.com/) and its command-line executable;
- [HandBrakeCLI](https://handbrake.fr/downloads2.php); and
- [FFmpeg](https://ffmpeg.org/download.html), including FFprobe.

Episode matching through OpenSubtitles requires an OpenSubtitles.com API key
unless the local-only subtitle provider is selected. TMDb improves canonical
metadata. Gemini and Tesseract OCR are optional.

### 2. Install & Launch
The easiest Windows installation is the portable release:

1. Download `RipWeaver-Windows-x64.zip` from
   [RipWeaver Releases](https://github.com/fajis1/ripweaver/releases).
2. Extract the complete archive; do not move `RipWeaver.exe` away from its
   adjacent `_internal` directory.
3. Run `RipWeaver\RipWeaver.exe`.
4. Keep the console window open while using the dashboard at
   `http://localhost:8001`.

The portable build includes RipWeaver's Python runtime and web frontend. It does
not include MakeMKV, HandBrakeCLI, FFmpeg, provider credentials, or media tools.

> [!NOTE]
> On the very first run, the system needs to download the speech recognition model (approx. 5-10 seconds). You will see a "System Loading" indicator.

### 3. Setup
1. In the Web UI, go to **Settings**.
2. Discover or select the MakeMKV, HandBrakeCLI, FFmpeg, and FFprobe
   executables.
3. Select separate rip-staging and encoded-staging roots plus at least one
   TV or movie media-library root used by Plex, Jellyfin, Emby, or another
   media server.
4. Enter the provider credentials you intend to use and click **Save**. The
   page has fields for the TMDb API key, OpenSubtitles username/password/API
   key, and Gemini primary/backup API keys.

Credential values are written to the local ignored `.env` and are never
returned to the browser.

For CLI setup, use hidden prompts:

```powershell
uv run mkv-match credentials tmdb
uv run mkv-match credentials opensubtitles-api
uv run mkv-match credentials opensubtitles-username
uv run mkv-match credentials opensubtitles-password
uv run mkv-match credentials gemini-primary
uv run mkv-match credentials gemini-paid
```

Run `uv run mkv-match credentials` without a name to see configured status and
official provider links. Credential values are never displayed. If TMDb or
OpenSubtitles rejects a key during an interactive CLI match, the command offers
to replace it and retries once. The Web UI stops safely and links to Settings
and the provider's key-management page.

One-time migration from an older user JSON config is also available:

```powershell
uv run mkv-match credentials --migrate-legacy
```

It reports only the names moved, never their values.

### Gemini model fallback

Under **Settings → Core Settings**, use the dropdowns to choose a primary
Gemini model and up to two ordered backup models. RipWeaver tries the primary and backup API keys for the
current model before switching models after exhausted capacity (HTTP 429),
sustained overload (HTTP 503), or a definite unavailable-model response. Other
request errors stop for review instead of silently changing models.

The default Flash chain is `gemini-3.6-flash` → `gemini-3.5-flash` →
`gemini-2.5-flash`. A cost-oriented chain can use
`gemini-3.5-flash-lite` → `gemini-3.1-flash-lite` →
`gemini-2.5-flash-lite`.

### Bug reports and support ZIPs

Open **Support & Bug Reports** in the sidebar to create a bounded diagnostic
ZIP. It includes redacted setup status, recent pipeline events, and limited
RipWeaver log tails. It excludes credentials, environment values, paths, media
names, dialogue, media files, and private Gemini provider responses. The ZIP is
created locally and downloaded; RipWeaver never uploads or emails it
automatically. Review it, then attach it to the prefilled GitHub issue or email
draft.

### 4. Make Your First Match
1.  Go to the **Dashboard**.
2.  Use the file browser to navigate to a folder containing your TV show episodes.
3.  Click **"Scan This Folder"**.
4.  The system will analyze your files and propose matches. You can review them before confirming the rename.

---

## ✨ Key Features
- **Modern Web Interface**: Easy-to-use Dashboard with dark mode.
- **Advanced Speech Recognition**: Identifies episodes by listening to the audio.
- **Smart Subtitles**: Automatically downloads subtitles from OpenSubtitles.
- **Safe**: Review matches before any files are renamed.

---

## ⚠️ Important: OpenSubtitles Download Limits

**Before using the app, please understand OpenSubtitles.com download limitations to avoid frustration.**

### The Issue
When you run MKV Episode Matcher, it automatically downloads reference subtitles for the entire season from OpenSubtitles.com. **Free OpenSubtitles accounts have very low daily download limits (typically 5-20 downloads per day).**

If you're trying to match a TV season with more episodes than your daily limit, you'll immediately see this error:
```
"Download limit reached. Please upgrade your account or wait for your quota to reset (~24hrs)"
```

### Solutions

**Option 1: Upgrade to OpenSubtitles VIP ($3/month)**
- Significantly higher download limits
- Fastest and most reliable solution
- [Upgrade here](https://www.opensubtitles.com/en/consumers)

**Option 2: Build Your Cache Gradually (Free)**
- Run the matcher once per day until you have all subtitles
- Each day you can download a few more episodes
- Requires patience but eventually works

**Option 3: Manual Subtitle Download**
- Download `.srt` files manually from any source
- Place them in your cache directory: `~/.mkv-episode-matcher/cache/data/`
- Use naming format: `{Show Name} - S{season:02d}E{episode:02d}.srt`

**Option 4: Switch to Engram (Recommended)**
- [Engram](https://github.com/Jsakkos/engram) includes multiple subtitle sources
- Better chance of success without hitting limits
- More modern interface and faster matching

### Why This Happens
MKV Episode Matcher needs reference subtitles to compare against the audio it extracts from your video files. The app downloads all subtitles for a season upfront, which quickly exhausts free account limits.

---

## 🛠️ Advanced Installation & Usage

The `mkv-episode-matcher` package on PyPI is the upstream project and is not a
distribution channel for the current RipWeaver application. Install current
RipWeaver from a portable release or this repository.

### Run from Source
We recommend using [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/fajis1/ripweaver.git
cd ripweaver

# Install dependencies
uv sync --extra cpu --group dev

# Launch Server
uv run mkv-match serve
```

### CLI Mode
You can also use the Command Line Interface (CLI) for automation. Make sure to run the command from the root directory of the repository or your home directory, not the show directory.

```bash
# Match a folder
mkv-match match "C:\Path\To\Show"

# Match with subtitle download
mkv-match match "C:\Path\To\Show" --get-subs
```

### Plan Titles from Saved MakeMKV Metadata

This command reads preflight JSON only. It does not access a disc or create a
rip command:

```powershell
uv run mkv-match plan-titles .mkv-preflight\saved-report.json
uv run mkv-match plan-titles .mkv-preflight\saved-report.json --json
```

The plan identifies likely individual episodes, combined-title duplicates,
short extras, review items, and the preferred diagnostic audio stream.

For ambiguous discs, optional constraints make the selection evidence explicit:

```powershell
uv run mkv-match plan-titles saved-report.json `
  --expected-episodes 4 `
  --expected-runtime 50 `
  --runtime-tolerance 3
```

### Plan Audio Diagnostics from Saved FFprobe JSON

This fixture-only Phase 2 command does not invoke FFprobe or FFmpeg and does not
open an MKV:

```powershell
uv run mkv-match plan-audio saved.ffprobe.json
uv run mkv-match plan-audio saved.ffprobe.json --json
```

It ranks main, multichannel, and commentary streams; plans three sample
windows; and records the loudness, silence, and transcript-information
measurements required before matching.

### Create a Sanitized FFprobe Report

This command reads metadata from explicit MKV files sequentially. It does not
extract, transcribe, rename, move, delete, or transcode media:

```powershell
uv run mkv-match probe-mkv C:\path\to\explicit-test-file.mkv
```

Configure `FFPROBE_PATH` in the local `.env`, pass `--ffprobe-path`, or place
`ffprobe.exe` on `PATH`. Reports use ordinal names under
`.mkv-preflight\ffprobe` and omit source filenames. Use those reports with
`mkv-match plan-audio`.

### Building the Executable
To build the portable application directory yourself:
```bash
uv sync --extra cpu --group dev
uv run pyinstaller mkv_match.spec
```

PyInstaller writes `dist/RipWeaver/RipWeaver.exe` on Windows. Release CI adds
the portable README and license files, archives the complete directory, extracts
it into a path containing spaces and non-ASCII characters, and runs the frozen
smoke check before uploading the artifact.

Release CI currently packages the reviewed frontend already tracked under
`mkv_episode_matcher/frontend/dist`. It deliberately refuses a bundle missing
the existing-rip recovery marker. Do not rebuild that frontend from the current
source until the recovery work described in `FRONTEND_RECOVERY_GUIDE.md` has
been restored and reviewed.

---

## Reference Subtitle Structure
If you have your own subtitles or don't use the auto-download feature, ensure your files are named correctly so the system can find them.

**Cache Directory:** `C:\Users\{username}\.mkv-episode-matcher\cache\data\`

**Naming Format:**
`{show_name} - S{season:02d}E{episode:02d}.srt`

Example:
```
Show Name/
├── Show Name - S01E01.srt
├── Show Name - S01E02.srt
```

## Contributing
1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License
Distributed under the MIT License. See `LICENSE` for more information.

RipWeaver is based on [MKV Episode Matcher](https://github.com/Jsakkos/mkv-episode-matcher)
and incorporates selected ideas or adapted behavior from
[Riplex](https://github.com/AnyCredit5518/riplex). Copyright, dependency, and
service acknowledgments are retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
and [docs/ATTRIBUTIONS.md](docs/ATTRIBUTIONS.md).

## Documentation
Full documentation is available at [https://jsakkos.github.io/mkv-episode-matcher/](https://jsakkos.github.io/mkv-episode-matcher/)
