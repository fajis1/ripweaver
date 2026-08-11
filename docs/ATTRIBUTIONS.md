# Attributions and External Services

## TMDB

This product uses the TMDB API but is not endorsed or certified by TMDB.

TMDB data is used for movie, television-series, season, and episode metadata.
TMDB's approved logo and the required notice appear in the application's Help
and Credits view. See <https://www.themoviedb.org> and
<https://www.themoviedb.org/about/logos-attribution>.

## OpenSubtitles.com

Reference subtitles may be obtained from OpenSubtitles.com when a user enables
that integration and supplies their own credentials. OpenSubtitles.com is an
independent service and does not endorse RipWeaver.

## Google Gemini

Gemini is an optional final ambiguity-resolution provider. It is contacted only
through the separately disclosed and confirmed evidence workflow. Google does
not endorse RipWeaver.

## TheDiscDB

TheDiscDB is an optional disc-layout metadata provider. When enabled, RipWeaver
calculates a compatibility identifier from the inserted disc's file names and
sizes and sends only that identifier to TheDiscDB. Returned episode metadata is
accepted only after its source playlist and, when both sides provide one,
segment map agree with the read-only MakeMKV inventory. TheDiscDB does not
endorse RipWeaver. Its public data repository is MIT-licensed and its web/API
repository is Apache-2.0 licensed; see `THIRD_PARTY_NOTICES.md`.

## External media tools

RipWeaver can invoke separately installed MakeMKV, HandBrakeCLI, FFmpeg, and
FFprobe executables. Those programs remain governed by their own licenses and
terms. They are not included in the RipWeaver source license and should not be
redistributed by a RipWeaver installer without a separate packaging and license
review.

## Project origins

RipWeaver is based on MKV Episode Matcher by Jonathan Sakkos and incorporates
selected ideas or adapted behavior from Riplex by AnyCredit5518. The applicable
MIT notices are retained in `LICENSE` and `THIRD_PARTY_NOTICES.md`.
