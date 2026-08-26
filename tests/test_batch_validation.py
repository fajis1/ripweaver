import json
from pathlib import Path

import pytest

from mkv_episode_matcher.disc.batch_validation import (
    BatchValidationPlanError,
    plan_batch_physical_validation,
    write_batch_validation_manifest,
)


def _title(index: int, duration: str, size: int, output_name: str) -> dict:
    return {
        "index": index,
        "attributes": {
            "9": duration,
            "11": str(size),
            "27": output_name,
        },
        "streams": [],
    }


def _inventory(tmp_path: Path, titles: list[dict]) -> Path:
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps({"drive": {"index": 2}, "titles": titles}),
        encoding="utf-8",
    )
    return path


def test_plans_smallest_two_title_exact_cutoff(tmp_path):
    inventory = _inventory(
        tmp_path,
        [
            _title(0, "0:10:00", 10_000_000, "title_t00.mkv"),
            _title(1, "0:05:00", 5_000_000, "title_t01.mkv"),
            _title(2, "0:00:07", 1_000_000, "title_t02.mkv"),
        ],
    )

    manifest = plan_batch_physical_validation(inventory)

    assert manifest.minimum_length_seconds == 8
    assert [item.title_index for item in manifest.expected_outputs] == [0, 1]
    assert manifest.estimated_bytes == 15_000_000
    assert manifest.relative_staging_dir.startswith("batch-validation/")
    assert manifest.execution_authorized is False


def test_boundary_tie_is_kept_in_exact_selected_set(tmp_path):
    inventory = _inventory(
        tmp_path,
        [
            _title(0, "0:10:00", 10_000_000, "title_t00.mkv"),
            _title(1, "0:05:00", 5_000_000, "title_t01.mkv"),
            _title(2, "0:05:00", 5_000_000, "title_t02.mkv"),
            _title(3, "0:01:00", 1_000_000, "title_t03.mkv"),
        ],
    )

    manifest = plan_batch_physical_validation(inventory)

    assert manifest.minimum_length_seconds == 61
    assert [item.title_index for item in manifest.expected_outputs] == [0, 1, 2]


@pytest.mark.parametrize(
    "titles, error",
    [
        (
            [_title(0, "0:10:00", 10_000_000, "title_t00.mkv")],
            "at least two inventory titles",
        ),
        (
            [
                _title(0, "0:10:00", 10_000_000, "same.mkv"),
                _title(1, "0:05:00", 5_000_000, "SAME.MKV"),
            ],
            "duplicate output names",
        ),
    ],
)
def test_rejects_incomplete_or_unsafe_inventory(tmp_path, titles, error):
    inventory = _inventory(tmp_path, titles)

    with pytest.raises(BatchValidationPlanError, match=error):
        plan_batch_physical_validation(inventory)


def test_unsafe_name_is_rejected_before_title_count_check(tmp_path):
    inventory = _inventory(
        tmp_path,
        [
            _title(0, "0:10:00", 10_000_000, "../unsafe.mkv"),
            _title(1, "0:05:00", 5_000_000, "title_t01.mkv"),
        ],
    )

    with pytest.raises(BatchValidationPlanError, match="lacks safe"):
        plan_batch_physical_validation(inventory)


def test_writer_refuses_overwrite_and_returns_file_digest(tmp_path):
    inventory = _inventory(
        tmp_path,
        [
            _title(0, "0:10:00", 10_000_000, "title_t00.mkv"),
            _title(1, "0:05:00", 5_000_000, "title_t01.mkv"),
        ],
    )
    manifest = plan_batch_physical_validation(inventory)
    destination = tmp_path / "validation.json"

    written, digest = write_batch_validation_manifest(destination, manifest)

    assert written == destination
    assert len(digest) == 64
    assert (
        json.loads(destination.read_text(encoding="utf-8"))["execution_authorized"]
        is False
    )
    with pytest.raises(BatchValidationPlanError, match="refusing overwrite"):
        write_batch_validation_manifest(destination, manifest)
