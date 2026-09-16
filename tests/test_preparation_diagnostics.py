import json

from fastapi import HTTPException

from mkv_episode_matcher.backend.preparation_diagnostics import safe_preparation_failure


def test_known_reason_survives_http_wrapper():
    try:
        try:
            raise ValueError("Saved inventory lacks complete batch title metadata")
        except ValueError as exc:
            raise HTTPException(409, "private wrapper") from exc
    except HTTPException as exc:
        assert (
            safe_preparation_failure(exc)["reason_code"] == "incomplete-batch-metadata"
        )


def test_unknown_details_and_nested_payloads_are_never_logged():
    for detail in ["secret-token /private/media", {"secret": "private-dialogue"}]:
        result = safe_preparation_failure(HTTPException(409, detail))
        assert result == {"reason_code": "unclassified", "code_locations": []}
        assert "secret" not in json.dumps(result)


def test_application_frame_is_recorded_without_locals_or_paths():
    from mkv_episode_matcher.disc.rip_manifest import media_context_from_dict

    try:
        media_context_from_dict({"selected_title_indexes": "private-media-path"})
    except Exception as exc:
        result = safe_preparation_failure(exc)
    assert any(
        "media_context_from_dict:" in entry for entry in result["code_locations"]
    )
    assert "private-media-path" not in json.dumps(result)
