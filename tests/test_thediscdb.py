from __future__ import annotations

from pathlib import Path

import pytest
import requests

from mkv_episode_matcher.disc.preflight import (
    DiscInventory,
    MakeMKVDrive,
    MakeMKVTitle,
)
from mkv_episode_matcher.disc.thediscdb import (
    DiscHashFile,
    TheDiscDbClient,
    TheDiscDbError,
    calculate_content_hash,
    enrich_inventory,
    parse_lookup_response,
    read_disc_filesystem_identity,
    select_consistent_disc,
)

CONTENT_HASH = "8B6FCE0775F77E41B1EB2E293BA9BA80"


def _inventory(*titles: MakeMKVTitle) -> DiscInventory:
    return DiscInventory(
        drive=MakeMKVDrive(
            index=0,
            visible=2,
            enabled=999,
            flags=1,
            drive_name="<hardware-redacted>",
            disc_name="Test Disc",
            device_name="<device-redacted>",
        ),
        disc_attributes={},
        titles=list(titles),
        return_code=0,
        started_at="2026-08-10T00:00:00+00:00",
        finished_at="2026-08-10T00:00:01+00:00",
        warnings=[],
    )


def _response(*, second_episode: int = 2) -> dict[str, object]:
    return {
        "data": {
            "mediaItems": {
                "nodes": [
                    {
                        "title": "Example Series",
                        "type": "Series",
                        "externalids": {"tmdb": "123"},
                        "releases": [
                            {
                                "title": "Complete Collection",
                                "discs": [
                                    {
                                        "index": 1,
                                        "name": "Season 1 Disc 1",
                                        "format": "Blu-ray",
                                        "contentHash": CONTENT_HASH,
                                        "titles": [
                                            {
                                                "index": 7,
                                                "sourceFile": "00800.mpls",
                                                "segmentMap": "46,47",
                                                "duration": "0:22:00",
                                                "size": 2_000_000_000,
                                                "item": {
                                                    "title": "Pilot",
                                                    "type": "Episode",
                                                    "season": "1",
                                                    "episode": "1",
                                                },
                                            },
                                            {
                                                "index": 8,
                                                "sourceFile": "00801.mpls",
                                                "segmentMap": "48",
                                                "duration": "0:22:00",
                                                "size": 2_000_000_000,
                                                "item": {
                                                    "title": "Second",
                                                    "type": "Episode",
                                                    "season": "1",
                                                    "episode": str(second_episode),
                                                },
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }


def test_content_hash_uses_sorted_names_and_little_endian_int64_sizes():
    files = (
        DiscHashFile("00002.m2ts", 256),
        DiscHashFile("00001.m2ts", 1),
    )

    assert calculate_content_hash(files) == "6B613AF0A76E7854CFE9EA151A618800"


def test_disc_filesystem_identity_reads_only_bounded_bluray_inputs(tmp_path: Path):
    stream = tmp_path / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00002.m2ts").write_bytes(bytes(256))
    (stream / "00001.m2ts").write_bytes(b"x")
    (stream / "ignore.txt").write_text("not hashed", encoding="utf-8")
    identity = read_disc_filesystem_identity(tmp_path)

    assert identity.content_hash == "6B613AF0A76E7854CFE9EA151A618800"
    assert identity.global_disc_id is None
    assert identity.file_count == 2
    assert identity.format == "Blu-ray"


def test_lookup_parser_keeps_only_exact_hash_disc_and_playlist_metadata():
    matches = parse_lookup_response(_response(), CONTENT_HASH.lower())

    assert len(matches) == 1
    assert matches[0].media_title == "Example Series"
    assert matches[0].tmdb_id == 123
    assert matches[0].titles[0].source_file == "00800.mpls"
    assert matches[0].titles[0].segment_map == "46,47"
    assert matches[0].titles[0].episode == 1


def test_inventory_enrichment_matches_playlist_and_checks_segment_map():
    disc = select_consistent_disc(parse_lookup_response(_response(), CONTENT_HASH))
    assert disc is not None
    inventory = _inventory(
        MakeMKVTitle(index=4, attributes={16: "00800.mpls", 26: "046,047"}),
        MakeMKVTitle(index=5, attributes={16: "00801.mpls", 26: "999"}),
        MakeMKVTitle(index=6, attributes={16: "00999.mpls", 26: "50"}),
    )

    resolution = enrich_inventory(inventory, disc)

    assert resolution.status == "matched"
    assert resolution.media_title == "Example Series"
    assert resolution.matched_title_indexes == (4,)
    assert resolution.unmatched_title_indexes == (5, 6)
    assert resolution.episode_assignments == (
        {
            "title_index": 4,
            "season": 1,
            "episode": 1,
            "title": "Pilot",
            "identification_source": "thediscdb",
        },
    )


def test_conflicting_copied_disc_records_are_not_selected():
    first = parse_lookup_response(_response(second_episode=2), CONTENT_HASH)[0]
    second = parse_lookup_response(_response(second_episode=3), CONTENT_HASH)[0]

    with pytest.raises(TheDiscDbError, match="conflicting records"):
        select_consistent_disc((first, second))


def test_client_sends_only_content_hash_and_redacts_network_failure():
    calls: list[dict[str, object]] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return _response()

    def post(_url, **kwargs):
        calls.append(kwargs)
        return Response()

    matches = TheDiscDbClient(post=post).lookup(CONTENT_HASH)

    assert len(matches) == 1
    assert calls[0]["json"]["variables"] == {"hash": CONTENT_HASH}
    assert set(calls[0]["json"]) == {"query", "variables"}

    def failed_post(_url, **_kwargs):
        raise requests.Timeout("private network detail")

    with pytest.raises(TheDiscDbError, match=r"failed safely \(Timeout\)") as error:
        TheDiscDbClient(post=failed_post).lookup(CONTENT_HASH)
    assert "private network detail" not in str(error.value)
