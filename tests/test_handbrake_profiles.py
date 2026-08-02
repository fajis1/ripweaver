import json

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.handbrake import HandBrakeProfile
from mkv_episode_matcher.media.handbrake_profiles import (
    HandBrakeProfileStore,
    HandBrakeProfileStoreError,
)


def test_profile_library_includes_each_encoder_family(tmp_path):
    profiles = HandBrakeProfileStore(tmp_path / "profiles.json").list()

    assert {item.profile.encoder for item in profiles} == {
        "vce_h265",
        "nvenc_h265",
        "qsv_h265",
        "x265",
    }
    assert all(item.built_in for item in profiles)


def test_custom_profile_round_trip_and_replace(tmp_path):
    path = tmp_path / "profiles.json"
    store = HandBrakeProfileStore(path)
    stored = store.save_custom(
        "archive-cpu",
        "Archive CPU",
        HandBrakeProfile(encoder="x265", encoder_preset="slow", quality=20),
    )

    assert stored.built_in is False
    loaded = {item.profile_id: item for item in HandBrakeProfileStore(path).list()}
    assert loaded["archive-cpu"].profile.quality == 20
    assert json.loads(path.read_text())[0]["profile_id"] == "archive-cpu"


def test_custom_profile_round_trips_all_audio_policy(tmp_path):
    path = tmp_path / "profiles.json"
    store = HandBrakeProfileStore(path)
    store.save_custom(
        "all-audio",
        "All Audio",
        HandBrakeProfile(additional_audio="all"),
    )

    loaded = {item.profile_id: item for item in HandBrakeProfileStore(path).list()}
    assert loaded["all-audio"].profile.additional_audio == "all"


def test_builtin_cannot_be_replaced(tmp_path):
    store = HandBrakeProfileStore(tmp_path / "profiles.json")

    with pytest.raises(HandBrakeProfileStoreError, match="Built-in"):
        store.save_custom("balanced", "Changed", HandBrakeProfile())


def test_invalid_profile_file_is_rejected_without_rewrite(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("not-json", encoding="utf-8")
    store = HandBrakeProfileStore(path)

    with pytest.raises(HandBrakeProfileStoreError, match="invalid"):
        store.list()
    assert path.read_text(encoding="utf-8") == "not-json"


def test_default_profile_selection_is_persisted_for_future_sessions(
    tmp_path, monkeypatch
):
    store = HandBrakeProfileStore(tmp_path / "profiles.json")
    original = Config(default_handbrake_profile="balanced")
    saved = []

    class FakeConfigManager:
        def load(self):
            return original

        def save(self, config):
            saved.append(config)

    monkeypatch.setattr(rip, "get_config_manager", FakeConfigManager)

    result = rip.set_default_handbrake_profile(
        rip.SetDefaultHandBrakeProfileRequest(profile_id="cpu-balanced"), store
    )

    assert result == {
        "status": "success",
        "profile_id": "cpu-balanced",
        "display_name": "CPU x265 Balanced",
        "scope": "general",
    }
    assert original.default_handbrake_profile == "balanced"
    assert [config.default_handbrake_profile for config in saved] == ["cpu-balanced"]


def test_default_profile_selection_rejects_unknown_profile(tmp_path, monkeypatch):
    store = HandBrakeProfileStore(tmp_path / "profiles.json")

    class UnexpectedConfigManager:
        def load(self):
            raise AssertionError("configuration must not be loaded")

    monkeypatch.setattr(rip, "get_config_manager", UnexpectedConfigManager)

    with pytest.raises(HTTPException) as exc_info:
        rip.set_default_handbrake_profile(
            rip.SetDefaultHandBrakeProfileRequest(profile_id="missing-profile"),
            store,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == (
        "Default HandBrake profile was not saved (HandBrakeProfileStoreError)"
    )
