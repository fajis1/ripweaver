import json
from pathlib import Path

from mkv_episode_matcher.disc.feature_catalog_registry import select_feature_catalog
from tests.test_rip_manifest import _inventory, _write_report


def _catalogue_directory() -> Path:
    return Path(__file__).parents[1] / "mkv_episode_matcher" / "feature_catalogs"


def test_registry_selects_reviewed_parent_trap_release(tmp_path):
    report = _write_report(
        tmp_path,
        "inventory.json",
        _inventory(
            2,
            "Private label",
            [1123, 882, 95, 360, 220, 300, 1352, 557, 1037, 1052, 97, 420, 142],
        ),
    )

    selected = select_feature_catalog(
        report,
        (_catalogue_directory(),),
        report_id="disc-01",
    )

    assert selected is not None
    assert selected.plan.catalog_id == "parent-trap-2005-r1-disc2-v1"
    assert selected.plan.library_title == "The Parent Trap"
    assert selected.plan.library_year == 1961
    assert selected.strong_match_count >= 10


def test_registry_does_not_force_unrelated_inventory(tmp_path):
    report = tmp_path / "inventory.json"
    report.write_text(
        json.dumps(_inventory(1, "Unrelated", [1800, 2400, 3600])),
        encoding="utf-8",
    )

    assert (
        select_feature_catalog(
            report,
            (_catalogue_directory(),),
            report_id="disc-01",
        )
        is None
    )
