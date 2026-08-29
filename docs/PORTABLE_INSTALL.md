# RipWeaver Portable Installation

This archive contains RipWeaver and its Python runtime. Keep the complete
`RipWeaver` directory together; the executable depends on the adjacent
`_internal` directory.

## Verify the download

Official release archives include a matching `.sha256` file. On Linux, verify
the archive before extracting it:

```bash
sha256sum --check RipWeaver-Linux-x64.tar.gz.sha256
```

On Windows, compare the hash printed by PowerShell with the first value in the
downloaded `.sha256` file:

```powershell
Get-FileHash .\RipWeaver-Windows-x64.zip -Algorithm SHA256
Get-Content .\RipWeaver-Windows-x64.zip.sha256
```

GitHub CLI users can also verify that an archive was produced by the official
RipWeaver build workflow:

```text
gh attestation verify <archive> --repo fajis1/ripweaver
```

Do not run an archive when either verification fails. The Windows portable
build is not currently Authenticode-signed, so Windows may display an
`Unknown publisher` or Microsoft Defender SmartScreen prompt even after these
checks pass.

## Windows installer

The simplest Windows download is `RipWeaver-Setup-Windows-x64.exe`. It installs
RipWeaver for the current user, adds Start Menu shortcuts, and requires no
administrator access. The installer does not install or download MakeMKV,
HandBrakeCLI, FFmpeg, or FFprobe. Its external-tool page opens only the official
download sites you choose.

The installer is not currently Authenticode-signed, so Windows may display an
`Unknown publisher` or Microsoft Defender SmartScreen prompt. Verify its
matching `.sha256` file or GitHub build attestation before running it.

## Windows portable archive

1. Install MakeMKV, HandBrakeCLI, and FFmpeg/FFprobe for the complete pipeline.
2. Extract the entire `RipWeaver-Windows-x64.zip` archive to a user-writable
   directory.
3. Run `RipWeaver\RipWeaver.exe`.
4. Complete the folder, tool, and provider settings in the local web dashboard.

RipWeaver opens its dashboard on `http://localhost:8001` and binds to the local
computer by default. Keep the console window open while RipWeaver is running;
closing it stops the application.

## Linux

1. Install MakeMKV, HandBrakeCLI, and FFmpeg/FFprobe for the complete pipeline.
2. Extract `RipWeaver-Linux-x64.tar.gz`.
3. Run `./RipWeaver/RipWeaver`.
4. Complete the folder, tool, and provider settings in the local web dashboard.

## First-run configuration

For disc ripping and the complete downstream pipeline, configure:

- the MakeMKV, HandBrakeCLI, FFmpeg, and FFprobe executables;
- separate rip-staging and encoded-staging roots;
- at least one television or movie media-library root for Plex, Jellyfin, Emby,
  or another server; and
- an OpenSubtitles API key unless the local-only subtitle provider is selected.

TMDb improves canonical media metadata. Gemini and Tesseract OCR are optional.
External tools and provider credentials are not bundled with RipWeaver.

RipWeaver stores its local configuration, logs, and private pipeline state
outside this program directory. Replacing the portable program directory does
not intentionally remove that user data. Always review release notes before an
upgrade.

## Source installation

The portable archive requires neither Python, uv, nor Node.js. Developers can
instead clone `https://github.com/fajis1/ripweaver` and run:

```powershell
uv sync --extra cpu --group dev
uv run mkv-match serve
```
