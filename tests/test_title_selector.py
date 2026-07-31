import json

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.disc.title_selector import (
    build_title_plan,
    normalize_title,
    parse_duration_seconds,
)


def _stream(
    stream_id: int,
    *,
    channels: int,
    layout: str,
    default: bool = False,
    name: str = "",
):
    return {
        "stream_id": stream_id,
        "attributes": {
            "1": "Audio",
            "2": name,
            "3": "eng",
            "6": "DD",
            "14": str(channels),
            "38": "d" if default else "",
            "40": layout,
        },
    }


def _title(
    index: int,
    duration: str,
    *,
    chapters: int = 5,
    size_bytes: int = 750_000_000,
    streams=None,
):
    return {
        "index": index,
        "attributes": {
            "8": str(chapters),
            "9": duration,
            "11": str(size_bytes),
            "27": f"title_t{index:02d}.mkv",
        },
        "streams": streams
        if streams is not None
        else {
            "1": _stream(
                1,
                channels=6,
                layout="5.1(side)",
                default=True,
            )
        },
    }


def _inventory(titles):
    return {"titles": titles, "warnings": []}


def test_duration_parser_is_strict():
    assert parse_duration_seconds("0:22:50") == 1370
    assert parse_duration_seconds("1:41:33") == 6093
    assert parse_duration_seconds("22:50") is None
    assert parse_duration_seconds("0:99:00") is None


def test_normalization_and_stereo_diagnostic_preference():
    raw = _title(
        7,
        "0:50:00",
        streams={
            "1": _stream(
                1,
                channels=6,
                layout="5.1(side)",
                default=True,
            ),
            "2": _stream(2, channels=2, layout="stereo"),
            "3": _stream(
                3,
                channels=2,
                layout="stereo",
                name="Director Commentary",
            ),
        },
    )

    title = normalize_title(raw)
    plan = build_title_plan(
        _inventory([raw, _title(8, "0:50:10")]),
        report_id="audio-fixture",
    )
    decision = plan.decisions[0]

    assert title.duration_seconds == 3000
    assert title.size_bytes == 750_000_000
    assert title.chapters == 5
    assert decision.diagnostic_audio_stream == 2
    assert decision.alternate_audio_streams == (1, 3)


def test_four_episode_fixture_selects_all_four():
    inventory = _inventory([
        _title(0, "0:48:38"),
        _title(1, "0:47:38"),
        _title(2, "0:53:31"),
        _title(3, "0:51:25"),
    ])

    plan = build_title_plan(inventory, report_id="four-episode-fixture")

    assert plan.mode == "plan-only"
    assert [d.title.index for d in plan.decisions if d.selected] == [0, 1, 2, 3]
    assert all(d.classification == "episode" for d in plan.decisions)


def test_six_episode_control_fixture_selects_all_six_and_uses_5_1_fallback():
    inventory = _inventory([
        _title(0, "0:22:50"),
        _title(1, "0:22:50"),
        _title(2, "0:22:50"),
        _title(3, "0:22:20"),
        _title(4, "0:22:48"),
        _title(5, "0:22:48"),
    ])

    plan = build_title_plan(inventory, report_id="known-good-control")

    assert len([decision for decision in plan.decisions if decision.selected]) == 6
    assert all(decision.diagnostic_audio_stream == 1 for decision in plan.decisions)
    assert all(not decision.alternate_audio_streams for decision in plan.decisions)


def test_combined_title_and_short_extra_are_excluded_with_reasons():
    inventory = _inventory([
        _title(0, "0:50:37"),
        _title(1, "0:50:56"),
        _title(2, "1:41:33", chapters=25, size_bytes=2_400_000_000),
        _title(3, "0:04:23"),
    ])

    plan = build_title_plan(inventory, report_id="combined-fixture")
    decisions = {decision.title.index: decision for decision in plan.decisions}

    assert decisions[0].selected is True
    assert decisions[1].selected is True
    assert decisions[2].classification == "combined"
    assert decisions[2].combined_from_titles == (0, 1)
    assert decisions[2].selected is False
    assert "avoid" in decisions[2].reasons[1]
    assert decisions[3].classification == "extra"
    assert decisions[3].selected is False


def test_expected_episode_count_can_expand_an_ambiguous_cluster():
    inventory = _inventory([
        _title(0, "0:18:48"),
        _title(1, "0:14:44"),
        _title(2, "0:22:33"),
        _title(3, "0:17:20"),
        _title(4, "0:17:35"),
    ])

    automatic = build_title_plan(inventory, report_id="mixed-auto")
    hinted = build_title_plan(
        inventory,
        report_id="mixed-hinted",
        expected_episode_count=5,
    )

    assert len([decision for decision in automatic.decisions if decision.selected]) == 3
    assert len([decision for decision in hinted.decisions if decision.selected]) == 5
    assert hinted.expected_episode_count == 5


def test_runtime_and_count_hints_resolve_long_form_ambiguity():
    inventory = _inventory([
        _title(0, "0:50:37"),
        _title(1, "0:50:56"),
        _title(2, "1:41:33"),
        _title(3, "0:52:49"),
    ])

    plan = build_title_plan(
        inventory,
        report_id="long-form-hinted",
        expected_episode_count=2,
        expected_runtime_seconds=50 * 60,
        runtime_tolerance_seconds=3 * 60,
    )
    decisions = {decision.title.index: decision for decision in plan.decisions}

    assert [
        decision.title.index for decision in plan.decisions if decision.selected
    ] == [
        0,
        1,
    ]
    assert decisions[2].classification == "combined"
    assert decisions[3].classification == "review"


def test_expected_count_is_not_forced_when_runtime_evidence_is_missing():
    plan = build_title_plan(
        _inventory([_title(0, "0:22:00"), _title(1, "0:50:00")]),
        report_id="insufficient-hint",
        expected_episode_count=3,
        expected_runtime_seconds=22 * 60,
        runtime_tolerance_seconds=60,
    )

    assert len([decision for decision in plan.decisions if decision.selected]) == 1
    assert any("was not forced" in note for note in plan.planning_notes)


def test_plan_contains_no_execution_command_or_media_action():
    plan = build_title_plan(
        _inventory([_title(0, "0:22:50"), _title(1, "0:22:48")]),
        report_id="safety-fixture",
    )
    serialized = str(plan.to_dict()).lower()

    assert "command" not in serialized
    assert "makemkv" not in serialized
    assert "handbrake" not in serialized
    assert "eject" not in serialized


def test_cli_json_is_plan_only_and_does_not_expose_report_path(tmp_path):
    report = tmp_path / "private-disc-label.json"
    report.write_text(
        json.dumps(_inventory([_title(0, "0:22:50"), _title(1, "0:22:48")])),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["plan-titles", str(report), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "plan-only"
    assert payload["plans"][0]["report_id"] == "report-1"
    assert str(report) not in result.output
    assert all(decision["selected"] for decision in payload["plans"][0]["decisions"])
