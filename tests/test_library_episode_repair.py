import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
    SourceIdentity,
)
from mkv_episode_matcher.backend.library_episode_repair import (
    LibraryEpisodeRepairError,
    LibraryEpisodeRepairStore,
    apply_generic_repairs,
    discover_episode_claims,
    execute_library_episode_audit,
    sequence_derived_episode_keys,
)
from mkv_episode_matcher.backend.routers import scan
from mkv_episode_matcher.core.models import Config, EpisodeInfo, SubtitleFile
from mkv_episode_matcher.media.gemini_matcher import UnmatchedFileEvidence


class _ScoreAsr:
    @staticmethod
    def calculate_match_score(excerpt: str, _reference: str) -> float:
        if excerpt.startswith("confirmed"):
            return 0.84
        if excerpt.startswith("single"):
            return 0.76
        return 0.22


class _Provider:
    def get_subtitles(self, series_name, season, _files, _tmdb_id):
        return [
            SubtitleFile(
                path=Path(f"unused-{episode}.srt"),
                content=(
                    "1\n00:00:00,000 --> 00:00:30,000\n"
                    "a sufficiently long reference subtitle dialogue window\n"
                ),
                episode_info=EpisodeInfo(
                    series_name=series_name,
                    season=season,
                    episode=episode,
                ),
            )
            for episode in (1, 2, 3)
        ]


def _library(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = tmp_path / "Jellyfin TV"
    season = root / "Example Show" / "Season 01"
    season.mkdir(parents=True)
    files = []
    for episode in (1, 2, 3):
        source = season / f"Example Show - S01E{episode:02d} - Title - 1080p.mkv"
        source.write_bytes(f"episode-{episode}".encode())
        files.append(source)
    (season / "RipWeaver Unmatched - old.mkv").write_bytes(b"generic")
    (season / "Example Show - S01E04-E05.mkv").write_bytes(b"multi")
    return root, files


def test_discovery_is_recursive_and_ignores_generic_and_multi_episode_names(tmp_path):
    root, files = _library(tmp_path)

    claims = discover_episode_claims(root)

    assert [claim.source for claim in claims] == files
    assert [claim.episode_id for claim in claims] == ["S01E01", "S01E02", "S01E03"]
    assert all("S01E" not in claim.generic_name for claim in claims)


def test_sequence_scope_uses_matched_tv_local_provenance(tmp_path):
    root, _files = _library(tmp_path)
    contract = tmp_path / "episode.organize.json"
    contract.write_text(
        '{"library_relative":"Example Show/Season 01/Example Show - S01E02 - 1080p.mkv"}',
        encoding="utf-8",
    )
    dossier = IdentificationDossierStore(tmp_path / "evidence")
    identity = SourceIdentity("a" * 16, 0, 10, 20, "small")
    dossier.save_evidence(
        identity,
        UnmatchedFileEvidence("media-2", 1320.0, ("private evidence",)),
    )
    dossier.record_attempt(
        ("media-2",),
        branch="tv-local",
        disposition="matched",
        summary={"reason": "scored"},
    )
    item = SimpleNamespace(
        media_id="media-2",
        artifact=SimpleNamespace(contract_path=contract),
    )

    keys = sequence_derived_episode_keys((item,), tmp_path / "evidence")
    claims = discover_episode_claims(root, episode_keys=keys)

    assert keys == frozenset({("example show", 1, 2)})
    assert [claim.episode_id for claim in claims] == ["S01E02"]


def test_audit_checks_only_claimed_episode_and_keeps_private_paths_private(tmp_path):
    root, _files = _library(tmp_path)
    store = LibraryEpisodeRepairStore(tmp_path / "private-repairs")
    created = store.create(root, discover_episode_claims(root), scope="all-named")
    store.start(created["job_id"], created["candidate_digest"])

    def evidence_collector(items, _config, _asr, _contract_root):
        excerpts = {
            1: ("confirmed first", "confirmed second"),
            2: ("wrong first", "wrong second"),
            3: ("single uncertain",),
        }
        evidence = []
        for item, payload in items:
            match = re.search(r"S01E(\d{2})", Path(payload["source_path"]).stem)
            assert match is not None
            episode = int(match.group(1))
            evidence.append(
                UnmatchedFileEvidence(item.media_id, 1320.0, excerpts[episode])
            )
        return tuple(evidence), object()

    result = execute_library_episode_audit(
        store,
        created["job_id"],
        Config(cache_dir=tmp_path / "cache", min_confidence=0.7),
        _ScoreAsr(),
        tmp_path / "contracts",
        provider_factory=_Provider,
        evidence_collector=evidence_collector,
    )

    assert result["status"] == "completed"
    assert [item["status"] for item in result["candidates"]] == [
        "confirmed",
        "mismatch",
        "inconclusive",
    ]
    assert result["candidates"][0]["qualifying_window_count"] == 2
    assert "library_root" not in result
    assert "source_path" not in str(result)
    assert str(root) not in str(result)


def test_apply_renames_exact_reviewed_mismatch_to_generic_name(tmp_path):
    root, files = _library(tmp_path)
    store = LibraryEpisodeRepairStore(tmp_path / "private-repairs")
    created = store.create(root, discover_episode_claims(root), scope="all-named")
    store.start(created["job_id"], created["candidate_digest"])
    for index, candidate in enumerate(created["candidates"]):
        store.record_result(
            created["job_id"],
            candidate["file_id"],
            {
                "status": "mismatch" if index == 1 else "confirmed",
                "score": 0.2 if index == 1 else 0.9,
                "qualifying_window_count": 0 if index == 1 else 2,
                "evidence_window_count": 2,
                "reason": "synthetic",
            },
        )
    completed = store.finish(created["job_id"])
    mismatch = completed["candidates"][1]

    applied = apply_generic_repairs(
        store,
        created["job_id"],
        result_digest=completed["result_digest"],
        file_ids=(mismatch["file_id"],),
    )

    assert applied["status"] == "applied"
    assert not files[1].exists()
    assert files[1].with_name(mismatch["generic_name"]).read_bytes() == b"episode-2"
    assert files[0].is_file() and files[2].is_file()


def test_apply_preflights_collision_before_changing_any_name(tmp_path):
    root, files = _library(tmp_path)
    store = LibraryEpisodeRepairStore(tmp_path / "private-repairs")
    created = store.create(root, discover_episode_claims(root), scope="all-named")
    store.start(created["job_id"], created["candidate_digest"])
    for candidate in created["candidates"]:
        store.record_result(
            created["job_id"],
            candidate["file_id"],
            {
                "status": "mismatch",
                "score": 0.2,
                "qualifying_window_count": 0,
                "evidence_window_count": 2,
                "reason": "synthetic",
            },
        )
    completed = store.finish(created["job_id"])
    collision = files[1].with_name(completed["candidates"][1]["generic_name"])
    collision.write_bytes(b"existing")

    with pytest.raises(LibraryEpisodeRepairError, match="destination"):
        apply_generic_repairs(
            store,
            created["job_id"],
            result_digest=completed["result_digest"],
            file_ids=tuple(item["file_id"] for item in completed["candidates"][:2]),
        )

    assert files[0].is_file() and files[1].is_file()
    assert collision.read_bytes() == b"existing"


def test_restart_surfaces_interrupted_audit_without_resuming_media_reads(tmp_path):
    root, _files = _library(tmp_path)
    private_root = tmp_path / "private-repairs"
    store = LibraryEpisodeRepairStore(private_root)
    created = store.create(root, discover_episode_claims(root), scope="all-named")
    store.start(created["job_id"], created["candidate_digest"])

    restarted = LibraryEpisodeRepairStore(private_root)
    result = restarted.public(created["job_id"])

    assert result["status"] == "failed"
    assert result["error_code"] == "audit_interrupted"


def test_start_route_requires_both_exact_media_and_provider_confirmations(tmp_path):
    root, _files = _library(tmp_path)
    store = LibraryEpisodeRepairStore(tmp_path / "private-repairs")
    created = store.create(root, discover_episode_claims(root), scope="all-named")

    with pytest.raises(HTTPException, match="confirmations"):
        scan.start_episode_audit(
            created["job_id"],
            scan.EpisodeAuditStartRequest(
                candidate_digest=created["candidate_digest"],
                confirm_media_read=True,
                confirm_provider_lookup=False,
            ),
            store,
            SimpleNamespace(asr=object()),
            tmp_path / "contracts",
        )


def test_start_route_queues_background_worker_without_blocking_request(
    monkeypatch, tmp_path
):
    root, _files = _library(tmp_path)
    store = LibraryEpisodeRepairStore(tmp_path / "private-repairs")
    created = store.create(root, discover_episode_claims(root), scope="all-named")
    started = []
    monkeypatch.setattr(
        scan,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: Config(cache_dir=tmp_path / "cache")),
    )
    monkeypatch.setattr(
        scan.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(
            start=lambda: started.append(kwargs["target"])
        ),
    )

    result = scan.start_episode_audit(
        created["job_id"],
        scan.EpisodeAuditStartRequest(
            candidate_digest=created["candidate_digest"],
            confirm_media_read=True,
            confirm_provider_lookup=True,
        ),
        store,
        SimpleNamespace(asr=object()),
        tmp_path / "contracts",
    )

    assert result["status"] == "running"
    assert len(started) == 1
