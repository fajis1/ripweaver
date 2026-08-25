import hashlib
import json
from dataclasses import asdict

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.handbrake import HandBrakeProfile


def _write_completed_handbrake_batch(manifest, events, encoded_root):
    relative = "encoded-staging/Test Series/Season 03/encoded-disc-title000.mkv"
    encoded = encoded_root / relative
    encoded.parent.mkdir(parents=True)
    encoded.write_bytes(b"\x00" * 16)
    payload = {
        "schema_version": 2,
        "mode": "handbrake-batch-manifest",
        "status": "ready-after-directory-creation",
        "profile": asdict(HandBrakeProfile()),
        "job_count": 1,
        "total_source_bytes": 16,
        "required_free_bytes": 16,
        "available_free_bytes": 1024,
        "missing_directories": [
            "encoded-staging/Test Series/Season 03",
        ],
        "jobs": [
            {
                "media_id": "disc-title000",
                "source_name": "raw-disc-title000.mkv",
                "source_size_bytes": 16,
                "destination_relative": relative,
            }
        ],
    }
    manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    events.write_text(
        "\n".join((
            json.dumps({"event": "batch-started", "manifest_sha256": digest}),
            json.dumps({
                "event": "job-completed",
                "media_id": "disc-title000",
                "output_bytes": 16,
            }),
        ))
        + "\n",
        encoding="utf-8",
    )
    return encoded


def test_cli_plans_relative_tv_destinations_without_mutation(tmp_path):
    sequence = tmp_path / "sequence.json"
    catalog = tmp_path / "catalog.json"
    library = tmp_path / "library"
    report = tmp_path / "organization.json"
    library.mkdir()
    sequence.write_text(
        json.dumps({
            "mode": "saved-disc-sequence-plan",
            "disposition": "proposed",
            "groups": [
                {
                    "group_id": "disc",
                    "disposition": "proposed",
                    "items": [
                        {
                            "file_id": "disc-title000",
                            "proposed_episode": "S03E01",
                        }
                    ],
                }
            ],
        }),
        encoding="utf-8",
    )
    catalog.write_text(
        json.dumps({
            "episodes": [
                {
                    "episode_id": "S03E01",
                    "season": 3,
                    "episode": 1,
                    "title": "Test Story",
                    "overview": "",
                    "runtime_seconds": 1800,
                }
            ]
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "plan-tv-organization",
            str(sequence),
            str(catalog),
            "--library-root",
            str(library),
            "--series-name",
            "Test Series",
            "--report-out",
            str(report),
        ],
    )

    assert result.exit_code == 0
    assert "No media-content read" in result.stdout
    assert not (library / "Test Series").exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["items"][0]["relative_destination"].startswith(
        "Test Series/Season 03/"
    )
    assert str(library) not in report.read_text(encoding="utf-8")


def test_execute_tv_organization_dry_run_no_mutation(tmp_path):
    organization = tmp_path / "organization.json"
    manifest = tmp_path / "handbrake-manifest.json"
    events = tmp_path / "handbrake-events.jsonl"
    source_root = tmp_path / "encoded"
    destination_root = tmp_path / "tv"
    source_root.mkdir()
    destination_root.mkdir()

    organization.write_text(
        json.dumps({
            "mode": "tv-organization-plan",
            "item_count": 1,
            "proposed_count": 1,
            "review_count": 0,
            "items": [
                {
                    "file_id": "disc-title000",
                    "episode_id": "S03E01",
                    "relative_destination": (
                        "Test Series/Season 03/Test Series - S03E01 - First.mkv"
                    ),
                    "status": "proposed",
                    "conflicts": [],
                }
            ],
        })
    )
    source_file = _write_completed_handbrake_batch(manifest, events, source_root)

    result = CliRunner().invoke(
        app,
        [
            "execute-tv-organization",
            str(organization),
            str(manifest),
            str(events),
            "--encoded-root",
            str(source_root),
            "--destination-root",
            str(destination_root),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Dry-run mode" in result.stdout
    assert source_file.exists()
    assert not (
        destination_root
        / "Test Series"
        / "Season 03"
        / "Test Series - S03E01 - First.mkv"
    ).exists()


def test_execute_tv_organization_can_move_with_confirm_and_match_sources(tmp_path):
    organization = tmp_path / "organization.json"
    manifest = tmp_path / "handbrake-manifest.json"
    events = tmp_path / "handbrake-events.jsonl"
    source_root = tmp_path / "encoded"
    destination_root = tmp_path / "tv"
    source_root.mkdir()
    destination_root.mkdir()

    organization.write_text(
        json.dumps({
            "mode": "tv-organization-plan",
            "item_count": 1,
            "proposed_count": 1,
            "review_count": 0,
            "items": [
                {
                    "file_id": "disc-title000",
                    "episode_id": "S03E01",
                    "relative_destination": (
                        "Test Series/Season 03/Test Series - S03E01 - First.mkv"
                    ),
                    "status": "proposed",
                    "conflicts": [],
                }
            ],
        })
    )
    source_file = _write_completed_handbrake_batch(manifest, events, source_root)

    destination = (
        destination_root
        / "Test Series"
        / "Season 03"
        / "Test Series - S03E01 - First.mkv"
    )

    result = CliRunner().invoke(
        app,
        [
            "execute-tv-organization",
            str(organization),
            str(manifest),
            str(events),
            "--encoded-root",
            str(source_root),
            "--destination-root",
            str(destination_root),
            "--confirm-move",
        ],
    )

    assert result.exit_code == 0
    assert destination.exists()
    assert not source_file.exists()
