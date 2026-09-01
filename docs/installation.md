# Installation Guide

RipWeaver is distributed as a portable application directory. Windows users do
not need Python, uv, or Node.js when using a release archive.

## Windows portable release

1. Install the external tools needed for your workflow:
   - MakeMKV for disc inventory and ripping;
   - HandBrakeCLI for transcoding; and
   - FFmpeg and FFprobe for evidence extraction and output verification.
2. Download `RipWeaver-Windows-x64.zip` from the
   [RipWeaver releases page](https://github.com/fajis1/ripweaver/releases).
3. Extract the complete archive into a user-writable directory.
4. Run `RipWeaver\RipWeaver.exe`.
5. Keep its console window open while using `http://localhost:8001`.

The executable must remain beside its `_internal` directory. The release does
not install a service, create shortcuts, modify `PATH`, or start with Windows.
Those behaviors are reserved for the future per-user installer.

## First-run configuration

Use **Settings** in the local dashboard to configure:

- MakeMKV, HandBrakeCLI, FFmpeg, and FFprobe executable paths;
- separate MakeMKV rip-staging and encoded-staging directories;
- Jellyfin television and/or movie library roots;
- an OpenSubtitles API key unless using local subtitles; and
- optional TMDb, Gemini, and Tesseract OCR integrations.

The dashboard can discover common executable locations and provides local
folder pickers. External programs and provider credentials are not bundled.

The first episode-identification run may download the selected faster-whisper
model into the user's model cache. This requires network access and additional
disk space.

## Linux portable release

Download and extract `RipWeaver-Linux-x64.tar.gz`, then run:

```bash
./RipWeaver/RipWeaver
```

The same external tools and first-run configuration are required. Physical-disc
and hardware behavior is developed primarily against Windows; review release
notes before relying on another platform for live disc work.

## Source installation

Source development supports Python 3.10 through 3.12 and uses Python 3.11 in
this checkout:

```powershell
git clone https://github.com/fajis1/ripweaver.git
Set-Location ripweaver
uv sync --extra cpu --group dev
uv run mkv-match serve
```

Node.js 20 and npm are required only when rebuilding the React frontend. The
compiled frontend is included in source and release packages.

Do not use `pip install mkv-episode-matcher` to install current RipWeaver. That
name belongs to the upstream MKV Episode Matcher distribution on PyPI.

## Building a portable release

From a prepared source checkout:

```powershell
uv sync --extra cpu --group dev
uv run pyinstaller mkv_match.spec --clean
dist\RipWeaver\RipWeaver.exe --portable-smoke-test
```

The current release process packages the reviewed frontend bundle tracked in
`mkv_episode_matcher/frontend/dist`. Do not run `npm run build` for a release
until the source recovery described in `FRONTEND_RECOVERY_GUIDE.md` is complete.
CI verifies that the reviewed bundle still contains the existing-rip recovery
surface before packaging it.

The smoke flag starts only a minimal loopback server. It verifies the packaged
health response and frontend assets, then shuts down. It does not run the normal
backend startup lifecycle, inspect drives, invoke media tools, or expose
production control routes.

Release CI packages the complete `dist/RipWeaver` directory and repeats the
smoke test after extracting the archive into a path containing spaces and
non-ASCII characters. A tag matching `v*` publishes the validated CPU archives;
a manual workflow run builds artifacts without creating a GitHub release.
