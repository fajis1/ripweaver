import json

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.disc.rip_manifest import (
    MediaContext,
    bind_fresh_batch_plans,
    build_rip_manifest,
    load_media_contexts,
    load_rip_manifest,
    write_rip_manifest,
)
from mkv_episode_matcher.disc.ripper import RipError


def _inventory(drive_index, disc_name, durations):
    titles = []
    for index, duration in enumerate(durations):
        titles.append({
            "index": index,
            "attributes": {
                "8": "10",
                "9": (
                    f"{duration // 3600}:"
                    f"{(duration % 3600) // 60:02d}:"
                    f"{duration % 60:02d}"
                ),
                "11": str(duration * 400_000),
                "27": f"private_{disc_name}_{index}.mkv",
            },
            "streams": {
                "1": {
                    "stream_id": 1,
                    "attributes": {
                        "1": "Audio",
                        "3": "eng",
                        "6": "DD",
                        "14": "2",
                        "38": "d",
                        "40": "stereo",
                    },
                }
            },
        })
    return {
        "minimum_length_seconds": 0,
        "drive": {
            "index": drive_index,
            "disc_name": disc_name,
            "drive_name": "private hardware serial",
            "device_name": "Z:",
        },
        "titles": titles,
        "warnings": [],
    }


def _write_report(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_context(tmp_path, payload):
    path = tmp_path / "media-context.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_batch_names(payload):
    for title in payload["titles"]:
        index = title["index"]
        title["attributes"]["27"] = f"Feature_t{index:02d}.mkv"
    return payload


def test_manifest_selects_episodes_and_excludes_combined_title(tmp_path):
    report = _write_report(
        tmp_path,
        "private-disc.json",
        _inventory(2, "Private Disc Label", [3000, 3010, 2990, 3005, 12005]),
    )

    manifest = build_rip_manifest([report])

    assert [job.title_index for job in manifest.jobs] == [0, 1, 2, 3]
    assert all(job.drive_index == 2 for job in manifest.jobs)
    assert all(
        job.relative_output_dir.startswith(".staging/disc-01/") for job in manifest.jobs
    )
    assert all(job.final_relative_dir is None for job in manifest.jobs)
    assert all(
        job.output_basename
        and job.output_basename.startswith("Private-Disc-Label--disc-01-")
        and job.output_basename.endswith(".mkv")
        for job in manifest.jobs
    )
    assert manifest.skipped_discs == ()


def test_movie_extras_stay_in_isolated_staging_with_readable_disc_name(tmp_path):
    report = _write_report(
        tmp_path,
        "movie-extras.json",
        _inventory(0, "PARENT_TRAP_1961_PARENT_TRAP_II", [300, 600, 900]),
    )
    context = MediaContext(
        disc_id="disc-01",
        series_name="Unmatched",
        content_hint="extras",
        selected_title_indexes=(0, 1, 2),
    )

    manifest = build_rip_manifest([report], {"disc-01": context})

    assert all(job.final_relative_dir is None for job in manifest.jobs)
    assert manifest.jobs[0].output_basename.startswith(
        "Parent-Trap-1961-Parent-Trap-II--disc-01-"
    )


def test_manifest_skips_disc_with_review_titles(tmp_path):
    report = _write_report(
        tmp_path,
        "ambiguous.json",
        _inventory(
            0,
            "Ambiguous Disc",
            [1128, 884, 363, 223, 304, 1353, 558, 1040, 1055, 144],
        ),
    )
    good = _write_report(
        tmp_path,
        "good.json",
        _inventory(1, "Good Disc", [1370, 1370, 1370, 1340, 1368, 1368]),
    )

    manifest = build_rip_manifest([report, good])

    assert len(manifest.jobs) == 6
    assert manifest.skipped_discs[0].disc_id == "disc-01"
    assert "review" in manifest.skipped_discs[0].reasons[0]


def test_automatic_mode_falls_back_to_plausible_bonus_titles(tmp_path):
    report = _write_report(
        tmp_path,
        "bonus.json",
        _inventory(3, "Bonus Disc", [99, 144, 360, 540, 900]),
    )
    context = MediaContext(disc_id="disc-01", series_name="Unmatched")

    manifest = build_rip_manifest([report], {"disc-01": context})

    assert [job.title_index for job in manifest.jobs] == [2, 3]
    assert manifest.skipped_discs == ()


def test_explicit_bonus_mode_does_not_select_episode_cluster_or_short_menus(tmp_path):
    report = _write_report(
        tmp_path,
        "bonus.json",
        _inventory(3, "Bonus Disc", [99, 144, 360, 540, 900, 1320, 1325]),
    )
    context = MediaContext(
        disc_id="disc-01",
        series_name="Unmatched",
        content_hint="extras",
    )

    manifest = build_rip_manifest([report], {"disc-01": context})

    assert {job.title_index for job in manifest.jobs}.isdisjoint({0, 1})
    assert {job.title_index for job in manifest.jobs}.issuperset({2, 3})
    assert 4 not in {job.title_index for job in manifest.jobs}


def test_catalogued_bonus_titles_remain_in_isolated_staging(tmp_path):
    report = _write_report(
        tmp_path,
        "bonus.json",
        _inventory(3, "Bonus Disc", [360, 540]),
    )
    context = MediaContext(
        disc_id="disc-01",
        series_name="Unmatched",
        content_hint="extras",
        selected_title_indexes=(0, 1),
        special_feature_catalog_id="reviewed-catalog",
    )

    manifest = build_rip_manifest([report], {"disc-01": context})

    assert all(job.final_relative_dir is None for job in manifest.jobs)


def test_media_context_builds_matcher_native_season_path(tmp_path):
    report = _write_report(
        tmp_path,
        "disc.json",
        _inventory(2, "Disc", [1300, 1300, 1300]),
    )
    context = MediaContext(
        disc_id="disc-01",
        series_name="Dragons: Race to the Edge",
        season=1,
        disc_number=2,
    )

    manifest = build_rip_manifest([report], {"disc-01": context})

    assert all(
        job.relative_output_dir.startswith(".staging/disc-01/") for job in manifest.jobs
    )
    assert {job.final_relative_dir for job in manifest.jobs} == {
        "TV Shows/Dragons - Race to the Edge/Season 01"
    }
    assert manifest.media_contexts == (context,)


def test_unknown_season_uses_series_unmatched_volume_path(tmp_path):
    report = _write_report(
        tmp_path,
        "disc.json",
        _inventory(2, "Disc", [3000, 3000, 3000]),
    )
    context = MediaContext(
        disc_id="disc-01",
        series_name="Faerie Tale Theatre",
        volume_number=5,
    )

    manifest = build_rip_manifest([report], {"disc-01": context})

    assert all(
        job.relative_output_dir.startswith(".staging/disc-01/") for job in manifest.jobs
    )
    assert {job.final_relative_dir for job in manifest.jobs} == {
        "TV Shows/Faerie Tale Theatre/Unmatched"
    }


def test_context_file_must_cover_every_selected_disc(tmp_path):
    report = _write_report(
        tmp_path,
        "disc.json",
        _inventory(2, "Disc", [1300, 1300, 1300]),
    )

    with pytest.raises(RipError, match="missing selected disc"):
        build_rip_manifest([report], {})


def test_duplicate_drive_reports_are_rejected(tmp_path):
    first = _write_report(
        tmp_path,
        "first.json",
        _inventory(1, "First", [1300, 1300, 1300]),
    )
    second = _write_report(
        tmp_path,
        "second.json",
        _inventory(1, "Second", [1400, 1400, 1400]),
    )

    with pytest.raises(RipError, match="same drive"):
        build_rip_manifest([first, second])


def test_written_manifest_is_path_and_identity_redacted(tmp_path):
    report = _write_report(
        tmp_path,
        "private-source-report.json",
        _inventory(3, "Private Disc Label", [1300, 1300, 1300]),
    )
    manifest_path = tmp_path / "manifest.json"

    write_rip_manifest(manifest_path, build_rip_manifest([report]))
    serialized = manifest_path.read_text(encoding="utf-8")
    loaded = load_rip_manifest(manifest_path)

    assert str(report) not in serialized
    assert report.name not in serialized
    assert "Private Disc Label" not in serialized
    assert "private hardware serial" not in serialized
    assert "private_Private Disc Label" not in serialized
    assert len(loaded.jobs) == 3


def test_manifest_records_redacted_single_open_proof(tmp_path):
    payload = _make_batch_names(_inventory(3, "Private Disc Label", [1300] * 3))
    report = _write_report(tmp_path, "fresh.json", payload)

    manifest = build_rip_manifest([report])
    proof = manifest.disc_proofs[0]
    serialized = json.dumps(manifest.to_dict())

    assert proof.batch_eligible is True
    assert proof.minimum_length_seconds == 0
    assert proof.selected_title_indexes == (0, 1, 2)
    assert "Feature_t00.mkv" not in serialized
    assert "Private Disc Label" not in serialized


def test_fresh_inventory_binds_exact_single_open_plan(tmp_path):
    payload = _make_batch_names(_inventory(3, "Disc", [1300] * 3))
    report = _write_report(tmp_path, "fresh.json", payload)
    manifest = build_rip_manifest([report])

    plans = bind_fresh_batch_plans(manifest, [report])

    assert set(plans) == {3}
    assert plans[3].minimum_length_seconds == 0
    assert [job.title_index for job in plans[3].jobs] == [0, 1, 2]


def test_changed_fresh_inventory_stops_before_execution(tmp_path):
    original = _make_batch_names(_inventory(3, "Disc", [1300] * 3))
    report = _write_report(tmp_path, "original.json", original)
    manifest = build_rip_manifest([report])
    changed = _make_batch_names(_inventory(3, "Disc", [1300, 1300, 1301]))
    changed_report = _write_report(tmp_path, "changed.json", changed)

    with pytest.raises(RipError, match="no longer matches"):
        bind_fresh_batch_plans(manifest, [changed_report])


def test_ineligible_fresh_inventory_keeps_per_title_fallback(tmp_path):
    payload = _inventory(3, "Disc", [1300] * 3)
    report = _write_report(tmp_path, "fresh.json", payload)
    manifest = build_rip_manifest([report])

    assert manifest.disc_proofs[0].batch_eligible is False
    assert bind_fresh_batch_plans(manifest, [report]) == {}


def test_plan_rip_cli_writes_reviewable_manifest(tmp_path):
    report = _write_report(
        tmp_path,
        "private-source-report.json",
        _inventory(3, "Private Disc Label", [1300, 1300, 1300]),
    )
    manifest_path = tmp_path / "approved-plan.json"
    context_path = _write_context(
        tmp_path,
        {
            "disc-01": {
                "series_name": "Test Series",
                "season": 1,
                "disc_number": 1,
            }
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "plan-rip",
            str(report),
            "--manifest-out",
            str(manifest_path),
            "--media-context",
            str(context_path),
        ],
    )

    assert result.exit_code == 0
    assert manifest_path.is_file()
    assert "disc-01-title-000" in result.output
    assert report.name not in result.output
    assert "Private Disc Label" not in manifest_path.read_text(encoding="utf-8")
    assert "TV Shows/Test Series/Season 01" in manifest_path.read_text(encoding="utf-8")


def test_load_media_contexts_rejects_invalid_season(tmp_path):
    context_path = _write_context(
        tmp_path,
        {"disc-01": {"series_name": "Show", "season": 100}},
    )

    with pytest.raises(RipError, match="invalid season"):
        load_media_contexts(context_path)


def test_execute_rip_cli_requires_explicit_confirmation(tmp_path):
    report = _write_report(
        tmp_path,
        "report.json",
        _inventory(3, "Disc", [1300, 1300, 1300]),
    )
    manifest_path = tmp_path / "approved-plan.json"
    write_rip_manifest(manifest_path, build_rip_manifest([report]))

    result = CliRunner().invoke(
        app,
        [
            "execute-rip",
            str(manifest_path),
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "--confirm-rip" in result.output


def test_execute_rip_cli_defaults_to_parallel_across_drives(
    tmp_path,
    monkeypatch,
):
    report = _write_report(
        tmp_path,
        "report.json",
        _inventory(3, "Disc", [1300, 1300, 1300]),
    )
    manifest_path = tmp_path / "approved-plan.json"
    write_rip_manifest(manifest_path, build_rip_manifest([report]))
    calls = []

    monkeypatch.setattr(
        "mkv_episode_matcher.disc.preflight.resolve_makemkv_path",
        lambda _path: tmp_path / "makemkvcon64.exe",
    )

    def fake_parallel(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "mkv_episode_matcher.disc.ripper.run_parallel_rip_queue",
        fake_parallel,
    )

    result = CliRunner().invoke(
        app,
        [
            "execute-rip",
            str(manifest_path),
            "--output-root",
            str(tmp_path),
            "--confirm-rip",
            "--log-dir",
            str(tmp_path / "logs"),
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1]["max_drives"] is None
    assert "parallel across all physical drives" in result.output


def test_execute_rip_cli_routes_bound_inventory_to_single_open(
    tmp_path,
    monkeypatch,
):
    payload = _make_batch_names(_inventory(3, "Disc", [1300] * 3))
    report = _write_report(tmp_path, "fresh.json", payload)
    manifest_path = tmp_path / "approved-plan.json"
    write_rip_manifest(manifest_path, build_rip_manifest([report]))
    calls = []

    monkeypatch.setattr(
        "mkv_episode_matcher.disc.preflight.resolve_makemkv_path",
        lambda _path: tmp_path / "makemkvcon64.exe",
    )

    def fake_auto(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "mkv_episode_matcher.disc.rip_orchestrator.run_parallel_auto_rip_queue",
        fake_auto,
    )

    result = CliRunner().invoke(
        app,
        [
            "execute-rip",
            str(manifest_path),
            "--output-root",
            str(tmp_path),
            "--confirm-rip",
            "--fresh-inventory",
            str(report),
            "--log-dir",
            str(tmp_path / "logs"),
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert set(calls[0][1]["batch_plans"]) == {3}
    assert "single-open" in result.output
