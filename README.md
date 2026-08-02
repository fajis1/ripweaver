# MKV Episode Matcher

[![Development Status](https://img.shields.io/pypi/status/mkv-episode-matcher)](https://pypi.org/project/mkv-episode-matcher/)
[![PyPI version](https://img.shields.io/pypi/v/mkv-episode-matcher.svg)](https://pypi.org/project/mkv-episode-matcher/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Documentation Status](https://img.shields.io/github/actions/workflow/status/Jsakkos/mkv-episode-matcher/documentation.yml?label=docs)](https://jsakkos.github.io/mkv-episode-matcher/)
[![Downloads](https://static.pepy.tech/badge/mkv-episode-matcher)](https://pepy.tech/project/mkv-episode-matcher)

Automatically match and rename your MKV TV episodes using advanced speech recognition and subtitle matching.

> [!TIP]
> **Recommended: Try Engram for new projects.** [Engram](https://github.com/Jsakkos/engram) provides a complete end-to-end media workflow including episode matching, automated organization, and more. MKV Episode Matcher remains available for standalone matching use cases.

## 🚀 Quick Start

Follow these steps to get up and running in minutes.

### 1. Prerequisites
Before you start, ensure you have the following:
*   **[FFmpeg](https://ffmpeg.org/download.html)**: Installed and added to your system PATH.
*   **API Keys**:
    *   **OpenSubtitles.com** account (for downloading subtitles).
    *   **TMDb** API Key (for fetching episode titles).
*   **Directory Structure**: Your files must be organized by Show/Season. See [Directory Structure](#folder-directory-structure) below.

### 2. Install & Launch
**The easiest way to run MKV Episode Matcher is using the standalone Windows executable.**

1.  Download the latest `mkv-match.exe` from [GitHub Releases](https://github.com/Jsakkos/mkv-episode-matcher/releases).
2.  Double-click `mkv-match.exe` to launch.
3.  The Web UI will automatically open in your default browser at `http://localhost:8001`.

> [!NOTE]
> On the very first run, the system needs to download the speech recognition model (approx. 5-10 seconds). You will see a "System Loading" indicator.

### 3. Setup
1.  In the Web UI, go to the **Settings** tab.
2.  Enter your **OpenSubtitles** credentials and **TMDb API Key**. Saved values
    are written to the local, Git-ignored `.env` and are never returned to the
    browser.
3.  Click **Save**.

For CLI setup, use hidden prompts:

```powershell
uv run mkv-match credentials tmdb
uv run mkv-match credentials opensubtitles-api
uv run mkv-match credentials opensubtitles-username
uv run mkv-match credentials opensubtitles-password
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

For developers, Linux/macOS users, or those preferring the command line.

### Option A: Install via pip (Cross-platform)
```bash
# Basic install
pip install mkv-episode-matcher[cpu]

# With CUDA support (NVIDIA GPU required)
pip install mkv-episode-matcher[cu128]
```

### Option B: Run from Source (Development)
We recommend using [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/Jsakkos/mkv-episode-matcher.git
cd mkv-episode-matcher

# Install dependencies
uv sync --extra cpu

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
To build the `.exe` yourself:
```bash
uv sync --extra cpu
uv run pyinstaller mkv_match.spec
```

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
