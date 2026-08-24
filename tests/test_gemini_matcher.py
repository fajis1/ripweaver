import json
from unittest.mock import Mock, patch

import pytest
import requests

from mkv_episode_matcher.core.credentials import (
    ApiCredentialError,
    ApiServiceError,
    set_credential_recovery_handler,
)
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiDescriptiveRanker,
    GeminiEpisodeRanker,
    GeminiMatchError,
    GeminiSubtitleComparisonEvidence,
    RequestsGeminiTransport,
    TransportResponse,
    UnmatchedFileEvidence,
    build_descriptive_gemini_request,
    build_gemini_request,
    load_gemini_bundle,
    plan_gemini_request,
    write_safe_request_plan,
)


@pytest.fixture(autouse=True)
def reset_recovery_handler():
    set_credential_recovery_handler(None)
    yield
    set_credential_recovery_handler(None)


def test_runtime_only_evidence_requests_a_provisional_best_choice():
    request = build_gemini_request(
        "gemini-test",
        (UnmatchedFileEvidence("disc-title-001", 95.0, ()),),
        (EpisodeCatalogEntry("feature-1", 0, 1, "Feature One", "", 97.0),),
    )
    prompt = json.loads(request["input"])
    assert prompt["files"][0]["transcript_excerpts"] == []
    assert "best provisional" in prompt["task"]
    assert (
        request["response_format"]["schema"]["properties"]["matches"]["items"][
            "properties"
        ]["episode_id"]["type"]
        == "string"
    )


def test_episode_request_accepts_six_bounded_excerpts_but_not_seven():
    catalog = (EpisodeCatalogEntry("S01E01", 1, 1, "Pilot", "", 1200),)
    build_gemini_request(
        "gemini-test",
        (UnmatchedFileEvidence("title-001", 1200, tuple("text" for _ in range(6))),),
        catalog,
    )

    with pytest.raises(GeminiMatchError, match="up to six"):
        build_gemini_request(
            "gemini-test",
            (
                UnmatchedFileEvidence(
                    "title-001", 1200, tuple("text" for _ in range(7))
                ),
            ),
            catalog,
        )


def test_episode_request_includes_bounded_paired_subtitle_evidence():
    files = (UnmatchedFileEvidence("title-001", 1200, ("whisper dialogue",)),)
    catalog = (EpisodeCatalogEntry("S04E11", 4, 11, "Survivor Man", "Trip", 1200),)
    request = build_gemini_request(
        "gemini-test",
        files,
        catalog,
        subtitle_comparisons={
            "title-001": (
                GeminiSubtitleComparisonEvidence(
                    "S04E11",
                    "whisper dialogue",
                    "regular subtitle dialogue",
                    0.62,
                ),
            )
        },
    )

    prompt = json.loads(request["input"])
    comparison = prompt["files"][0]["subtitle_comparisons"][0]
    assert comparison == {
        "candidate_episode_id": "S04E11",
        "whisper_excerpt": "whisper dialogue",
        "subtitle_excerpt": "regular subtitle dialogue",
        "local_score": 0.62,
    }
    assert "extended-cut dialogue" in prompt["task"]


def test_confirmation_request_audits_complete_proposal_map():
    files = (
        UnmatchedFileEvidence("title-001", 1200, ("one",)),
        UnmatchedFileEvidence("title-002", 1200, ("two",)),
    )
    catalog = (
        EpisodeCatalogEntry("S04E11", 4, 11, "Eleven", "", 1200),
        EpisodeCatalogEntry("S04E12", 4, 12, "Twelve", "", 1200),
    )
    request = build_gemini_request(
        "gemini-test",
        files,
        catalog,
        review_phase="confirmation",
        proposed_assignments={"title-001": "S04E11", "title-002": None},
    )

    prompt = json.loads(request["input"])
    assert prompt["review_phase"] == "confirmation"
    assert prompt["proposed_assignments"] == {
        "title-001": "S04E11",
        "title-002": None,
    }
    assert "complete proposed one-to-one assignment map" in prompt[
        "confirmation_instruction"
    ]


def test_episode_request_includes_explicit_reviewer_scene_description():
    files = (UnmatchedFileEvidence("title-001", 1200, ("bounded dialogue",)),)
    catalog = (
        EpisodeCatalogEntry(
            "S02E12",
            2,
            12,
            "The Injury",
            "Michael burns his foot and Dwight rushes to help.",
            1200,
        ),
    )

    request = build_gemini_request(
        "gemini-test",
        files,
        catalog,
        reviewer_scene_descriptions={
            "title-001": "Michael burned his foot on a George Foreman grill."
        },
    )
    prompt = json.loads(request["input"])

    assert prompt["files"][0]["reviewer_scene_description"] == (
        "Michael burned his foot on a George Foreman grill."
    )
    assert "strong plot evidence" in prompt["task"]


def test_reviewer_scene_description_rejects_unknown_file_or_local_path():
    files = (UnmatchedFileEvidence("title-001", 1200, ("bounded dialogue",)),)
    catalog = (EpisodeCatalogEntry("S02E12", 2, 12, "The Injury", "", 1200),)

    with pytest.raises(GeminiMatchError, match="unknown file ID"):
        build_gemini_request(
            "gemini-test",
            files,
            catalog,
            reviewer_scene_descriptions={"title-999": "Different title"},
        )
    with pytest.raises(GeminiMatchError, match="unsafe"):
        build_gemini_request(
            "gemini-test",
            files,
            catalog,
            reviewer_scene_descriptions={"title-001": "Review G:\\private\\clip.mkv"},
        )


def test_descriptive_request_is_path_free_and_classifies_mixed_titles():
    files = (
        UnmatchedFileEvidence("title-000", 7727, ("Two sisters meet at camp.",)),
        UnmatchedFileEvidence("title-003", 321, ("A short behind the scenes clip.",)),
    )

    request = build_descriptive_gemini_request(
        "gemini-test", files, release_hint="Parent Trap 1961 Parent Trap II"
    )
    serialized = json.dumps(request)

    assert "movie" in serialized
    assert "extra" in serialized
    assert "G:\\" not in serialized
    assert "api_key" not in serialized.lower()


def test_final_requests_include_only_safe_prior_attempt_summaries():
    files = (UnmatchedFileEvidence("title-000", 1200, ("bounded dialogue",)),)
    attempts = {
        "title-000": (
            {
                "branch": "tv-local",
                "disposition": "review",
                "summary": {"best_score": 0.48, "reason": "weak_margin"},
            },
        )
    }

    request = build_descriptive_gemini_request(
        "gemini-test",
        files,
        release_hint="Example disc",
        prior_attempts=attempts,
    )
    prompt = json.loads(request["input"])

    assert prompt["files"][0]["prior_attempts"] == [attempts["title-000"][0]]
    assert "prior-attempt summaries" in prompt["task"]


def test_prior_attempt_summary_rejects_paths_and_nested_private_data():
    files = (UnmatchedFileEvidence("title-000", 1200, ("bounded dialogue",)),)
    catalog = (EpisodeCatalogEntry("S01E01", 1, 1, "One", "", 1200),)
    with pytest.raises(GeminiMatchError, match="unsafe"):
        build_gemini_request(
            "gemini-test",
            files,
            catalog,
            prior_attempts={
                "title-000": (
                    {
                        "branch": "tv-local",
                        "disposition": "failed",
                        "summary": {"source": "G:\\private\\episode.mkv"},
                    },
                )
            },
        )


def test_descriptive_ranker_validates_safe_provisional_results():
    files = (
        UnmatchedFileEvidence("title-000", 7727, ("Two sisters meet at camp.",)),
        UnmatchedFileEvidence("title-003", 321, ("A short production clip.",)),
    )
    transport = FakeTransport(
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": "title-000",
                    "content_kind": "movie",
                    "suggested_title": "The Parent Trap",
                    "year": 1961,
                    "confidence": 0.91,
                    "evidence": ["Feature runtime and dialogue agree."],
                    "summary": "A feature-length story about sisters meeting at camp.",
                },
                {
                    "file_id": "title-003",
                    "content_kind": "extra",
                    "suggested_title": "Parent Trap Production Featurette",
                    "year": None,
                    "confidence": 0.66,
                    "evidence": ["Short production-focused material."],
                    "summary": "A short look at the production of The Parent Trap.",
                },
            ]),
        )
    )

    plan = GeminiDescriptiveRanker(
        model="gemini-test", transport=transport, max_retries=0
    ).describe_with_key(
        files,
        release_hint="Parent Trap 1961 Parent Trap II",
        api_key="fake-key",
        credential="gemini-primary",
    )

    assert [(item.content_kind, item.suggested_title) for item in plan.matches] == [
        ("movie", "The Parent Trap"),
        ("extra", "Parent Trap Production Featurette"),
    ]


def test_descriptive_ranker_rejects_unsafe_title():
    files = (UnmatchedFileEvidence("title-000", 300, ("Evidence.",)),)
    transport = FakeTransport(
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": "title-000",
                    "content_kind": "extra",
                    "suggested_title": "..\\private\\title",
                    "year": None,
                    "confidence": 0.8,
                    "evidence": ["Unsafe title test."],
                    "summary": "A short supplemental video.",
                }
            ]),
        )
    )

    with pytest.raises(GeminiMatchError, match="invalid descriptive output"):
        GeminiDescriptiveRanker(
            model="gemini-test", transport=transport, max_retries=0
        ).describe_with_key(
            files,
            release_hint="Example release",
            api_key="fake-key",
            credential="gemini-primary",
        )


def test_descriptive_ranker_rejects_generic_extra_title():
    files = (UnmatchedFileEvidence("title-000", 300, ("Evidence.",)),)
    transport = FakeTransport(
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": "title-000",
                    "content_kind": "extra",
                    "suggested_title": "Bonus Feature",
                    "year": None,
                    "confidence": 0.8,
                    "evidence": ["A production interview is visible."],
                    "summary": "Cast members discuss production of the series.",
                }
            ]),
        )
    )

    with pytest.raises(GeminiMatchError, match="generic extra title"):
        GeminiDescriptiveRanker(
            model="gemini-test", transport=transport, max_retries=0
        ).describe_with_key(
            files,
            release_hint="Example release",
            api_key="fake-key",
            credential="gemini-primary",
        )


@pytest.fixture
def theatre_bundle():
    files = (
        UnmatchedFileEvidence(
            "disc-03-title-000",
            2923,
            ("An evil goblin roamed the universe creating mischief.",),
        ),
        UnmatchedFileEvidence(
            "disc-03-title-001",
            2864,
            ("Willie asks why he cannot stay up while on holiday.",),
        ),
    )
    catalog = (
        EpisodeCatalogEntry(
            "S04E02",
            4,
            2,
            "The Snow Queen",
            "A wicked goblin separates two friends.",
            2940,
        ),
        EpisodeCatalogEntry(
            "S04E03",
            4,
            3,
            "The Pied Piper of Hamelin",
            "A piper is hired to rid Hamelin of rats.",
            2880,
        ),
    )
    return files, catalog


def _payload(matches):
    return {"output_text": json.dumps({"matches": matches})}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_request_is_path_free_and_schema_constrained(theatre_bundle):
    files, catalog = theatre_bundle

    request = build_gemini_request(
        "gemini-test",
        files,
        catalog,
        existing_episode_ids=frozenset({catalog[0].episode_id}),
    )
    serialized = json.dumps(request)
    matches_schema = request["response_format"]["schema"]["properties"]["matches"]

    assert request["response_format"]["mime_type"] == "application/json"
    assert matches_schema["minItems"] == len(files)
    assert matches_schema["maxItems"] == len(files)
    assert matches_schema["items"]["properties"]["file_id"]["enum"] == [
        item.file_id for item in files
    ]
    assert matches_schema["items"]["properties"]["episode_id"]["enum"] == [
        *(item.episode_id for item in catalog),
        None,
    ]
    prompt = json.loads(request["input"])
    assert prompt["allowed_episodes"][0]["library_status"] == "present"
    assert prompt["allowed_episodes"][1]["library_status"] == "missing"
    assert "S04E02" in serialized
    assert "disc-03-title-000" in serialized
    assert "G:\\" not in serialized
    assert "api_key" not in serialized.lower()


def test_request_plan_and_saved_report_omit_dialogue(
    theatre_bundle,
    tmp_path,
):
    files, catalog = theatre_bundle
    plan = plan_gemini_request("gemini-test", files, catalog)
    report = tmp_path / "plan.json"

    write_safe_request_plan(report, plan)
    serialized = report.read_text(encoding="utf-8")

    assert "evil goblin" not in serialized.lower()
    assert "transcript_excerpts" not in serialized
    assert "disc-03-title-000" in serialized
    with pytest.raises(GeminiMatchError, match="refusing overwrite"):
        write_safe_request_plan(report, plan)


def test_load_bundle_validates_transient_json(theatre_bundle, tmp_path):
    files, catalog = theatre_bundle
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps({
            "files": [item.__dict__ for item in files],
            "episodes": [item.to_dict() for item in catalog],
        }),
        encoding="utf-8",
    )

    loaded_files, loaded_catalog = load_gemini_bundle(path)

    assert loaded_files == files
    assert loaded_catalog == catalog


def test_theatre_regression_accepts_only_supplied_one_to_one_matches(
    theatre_bundle,
):
    files, catalog = theatre_bundle
    transport = FakeTransport(
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": "disc-03-title-000",
                    "episode_id": "S04E02",
                    "confidence": 0.96,
                    "evidence": ["Goblin and friendship plot agree."],
                },
                {
                    "file_id": "disc-03-title-001",
                    "episode_id": "S04E03",
                    "confidence": 0.98,
                    "evidence": ["Willie holiday dialogue agrees."],
                },
            ]),
        )
    )

    plan = GeminiEpisodeRanker(
        model="gemini-test",
        transport=transport,
        sleep=lambda _seconds: None,
    ).rank_with_key(
        files,
        catalog,
        api_key="fake-test-key",
        credential="gemini-primary",
    )

    assert [(item.file_id, item.episode_id) for item in plan.matches] == [
        ("disc-03-title-000", "S04E02"),
        ("disc-03-title-001", "S04E03"),
    ]
    assert "transcript" not in json.dumps(plan.safe_report()).lower()
    assert transport.calls[0]["api_key"] == "fake-test-key"


def test_full_theatre_aired_order_regression_is_preserved():
    expected = (
        ("disc-03-title-000", "S04E02", "The Snow Queen"),
        ("disc-03-title-001", "S04E03", "The Pied Piper of Hamelin"),
        ("disc-03-title-002", "S04E04", "Cinderella"),
        ("disc-03-title-003", "S04E05", "Puss in Boots"),
        ("disc-04-title-000", "S03E05", "Snow White and the Seven Dwarfs"),
        ("disc-04-title-001", "S03E06", "Beauty and the Beast"),
        (
            "disc-04-title-002",
            "S03E07",
            "The Boy Who Left Home to Find Out About the Shivers",
        ),
        ("disc-04-title-003", "S04E01", "The Three Little Pigs"),
        ("disc-05-title-000", "S03E01", "Goldilocks and the Three Bears"),
        ("disc-05-title-001", "S03E02", "The Princess and the Pea"),
        ("disc-05-title-002", "S03E03", "Pinocchio"),
        ("disc-05-title-003", "S03E04", "Thumbelina"),
    )
    files = tuple(
        UnmatchedFileEvidence(file_id, 3000, (f"Evidence for {title}.",))
        for file_id, _episode_id, title in expected
    )
    catalog = tuple(
        EpisodeCatalogEntry(
            episode_id,
            int(episode_id[1:3]),
            int(episode_id[4:]),
            title,
            f"Overview for {title}.",
            3000,
        )
        for _file_id, episode_id, title in expected
    )
    transport = FakeTransport(
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": file_id,
                    "episode_id": episode_id,
                    "confidence": 0.95,
                    "evidence": ["Title and plot agree."],
                }
                for file_id, episode_id, _title in expected
            ]),
        )
    )

    plan = GeminiEpisodeRanker(
        model="gemini-test",
        transport=transport,
        max_retries=0,
    ).rank_with_key(
        files,
        catalog,
        api_key="fake-key",
        credential="gemini-primary",
    )

    assert {(item.file_id, item.episode_id) for item in plan.matches} == {
        (file_id, episode_id) for file_id, episode_id, _title in expected
    }


@pytest.mark.parametrize(
    "matches, message",
    [
        (
            [
                {
                    "file_id": "disc-03-title-000",
                    "episode_id": "S99E99",
                    "confidence": 0.9,
                    "evidence": ["Invented candidate."],
                },
                {
                    "file_id": "disc-03-title-001",
                    "episode_id": "S04E03",
                    "confidence": 0.9,
                    "evidence": ["Allowed candidate."],
                },
            ],
            "outside the catalogue",
        ),
        (
            [
                {
                    "file_id": "disc-03-title-000",
                    "episode_id": "S04E02",
                    "confidence": 0.9,
                    "evidence": ["First assignment."],
                },
                {
                    "file_id": "disc-03-title-001",
                    "episode_id": "S04E02",
                    "confidence": 0.9,
                    "evidence": ["Duplicate assignment."],
                },
            ],
            "more than once",
        ),
    ],
)
def test_semantic_validation_rejects_untrusted_assignments(
    theatre_bundle,
    matches,
    message,
):
    files, catalog = theatre_bundle
    ranker = GeminiEpisodeRanker(
        model="gemini-test",
        transport=FakeTransport(TransportResponse(200, _payload(matches))),
        max_retries=0,
    )

    with pytest.raises(GeminiMatchError, match=message):
        ranker.rank_with_key(
            files,
            catalog,
            api_key="fake-test-key",
            credential="gemini-primary",
        )


def test_semantic_validation_retries_with_bounded_correction(theatre_bundle):
    files, catalog = theatre_bundle
    invalid = [
        {
            "file_id": item.file_id,
            "episode_id": "S04E02",
            "confidence": 0.8,
            "evidence": ["Candidate evidence."],
        }
        for item in files
    ]
    valid = [
        {
            "file_id": files[0].file_id,
            "episode_id": "S04E02",
            "confidence": 0.9,
            "evidence": ["First candidate."],
        },
        {
            "file_id": files[1].file_id,
            "episode_id": "S04E03",
            "confidence": 0.9,
            "evidence": ["Second candidate."],
        },
    ]
    transport = FakeTransport(
        TransportResponse(200, _payload(invalid)),
        TransportResponse(200, _payload(valid)),
    )

    plan = GeminiEpisodeRanker(
        model="gemini-test", transport=transport, max_retries=1
    ).rank_with_key(
        files,
        catalog,
        api_key="fake-test-key",
        credential="gemini-primary",
    )

    assert len(plan.matches) == 2
    assert len(transport.calls) == 2
    retry_prompt = json.loads(transport.calls[1]["body"]["input"])
    assert "validation_retry" in retry_prompt
    assert (
        "assigned one episode more than once"
        in retry_prompt["validation_retry"]["instruction"]
    )


def test_rejected_key_raises_recoverable_credential_error(theatre_bundle):
    files, catalog = theatre_bundle
    ranker = GeminiEpisodeRanker(
        model="gemini-test",
        transport=FakeTransport(TransportResponse(401, {})),
        max_retries=0,
    )

    with pytest.raises(ApiCredentialError) as raised:
        ranker.rank_with_key(
            files,
            catalog,
            api_key="fake-old-key",
            credential="gemini-primary",
        )

    assert raised.value.credential == "gemini-primary"
    assert raised.value.status_code == 401


def test_rate_limit_retries_with_backoff_then_stops(theatre_bundle):
    files, catalog = theatre_bundle
    sleeps = []
    transport = FakeTransport(
        TransportResponse(429, {}),
        TransportResponse(429, {}),
        TransportResponse(429, {}),
    )
    ranker = GeminiEpisodeRanker(
        model="gemini-test",
        transport=transport,
        max_retries=2,
        sleep=sleeps.append,
    )

    with pytest.raises(ApiServiceError, match="rate or quota"):
        ranker.rank_with_key(
            files,
            catalog,
            api_key="fake-key",
            credential="gemini-primary",
        )

    assert sleeps == [1, 2]
    assert len(transport.calls) == 3


def test_network_timeout_is_bounded(theatre_bundle):
    files, catalog = theatre_bundle
    transport = FakeTransport(
        requests.Timeout("private request detail"),
        requests.Timeout("private request detail"),
    )
    ranker = GeminiEpisodeRanker(
        model="gemini-test",
        transport=transport,
        max_retries=1,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(ApiServiceError, match="network failure: Timeout") as raised:
        ranker.rank_with_key(
            files,
            catalog,
            api_key="fake-key",
            credential="gemini-primary",
        )

    assert "private request detail" not in str(raised.value)


@patch("mkv_episode_matcher.media.gemini_matcher.load_environment_settings")
def test_configured_keys_recover_rejected_primary_once(
    mock_settings,
    theatre_bundle,
):
    files, catalog = theatre_bundle
    mock_settings.side_effect = [
        Mock(gemini_primary_api_key="fake-old", gemini_paid_api_key=None),
        Mock(gemini_primary_api_key="fake-new", gemini_paid_api_key=None),
    ]
    transport = FakeTransport(
        TransportResponse(401, {}),
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": item.file_id,
                    "episode_id": episode.episode_id,
                    "confidence": 0.9,
                    "evidence": ["Allowed evidence."],
                }
                for item, episode in zip(files, catalog, strict=True)
            ]),
        ),
    )
    set_credential_recovery_handler(lambda _error: True)

    plan = GeminiEpisodeRanker(
        model="gemini-test",
        transport=transport,
        max_retries=0,
    ).rank_with_configured_keys(files, catalog)

    assert len(plan.matches) == 2
    assert [call["api_key"] for call in transport.calls] == [
        "fake-old",
        "fake-new",
    ]


@patch("mkv_episode_matcher.media.gemini_matcher.load_environment_settings")
def test_configured_keys_fall_back_to_paid_on_service_failure(
    mock_settings,
    theatre_bundle,
):
    files, catalog = theatre_bundle
    mock_settings.side_effect = [
        Mock(gemini_primary_api_key="fake-primary", gemini_paid_api_key="fake-paid"),
        Mock(gemini_primary_api_key="fake-primary", gemini_paid_api_key="fake-paid"),
    ]
    transport = FakeTransport(
        TransportResponse(503, {}),
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": item.file_id,
                    "episode_id": episode.episode_id,
                    "confidence": 0.9,
                    "evidence": ["Allowed evidence."],
                }
                for item, episode in zip(files, catalog, strict=True)
            ]),
        ),
    )

    plan = GeminiEpisodeRanker(
        model="gemini-test",
        transport=transport,
        max_retries=0,
    ).rank_with_configured_keys(files, catalog)

    assert len(plan.matches) == 2
    assert [call["api_key"] for call in transport.calls] == [
        "fake-primary",
        "fake-paid",
    ]


@patch("mkv_episode_matcher.media.gemini_matcher.load_environment_settings")
def test_configured_keys_fall_back_to_paid_on_invalid_provider_response(
    mock_settings,
    theatre_bundle,
):
    files, catalog = theatre_bundle
    mock_settings.side_effect = [
        Mock(gemini_primary_api_key="fake-primary", gemini_paid_api_key="fake-paid"),
        Mock(gemini_primary_api_key="fake-primary", gemini_paid_api_key="fake-paid"),
    ]
    transport = FakeTransport(
        TransportResponse(200, {"unexpected": "response"}),
        TransportResponse(
            200,
            _payload([
                {
                    "file_id": item.file_id,
                    "episode_id": episode.episode_id,
                    "confidence": 0.9,
                    "evidence": ["Allowed evidence."],
                }
                for item, episode in zip(files, catalog, strict=True)
            ]),
        ),
    )

    plan = GeminiEpisodeRanker(
        model="gemini-test",
        transport=transport,
        max_retries=0,
    ).rank_with_configured_keys(files, catalog)

    assert len(plan.matches) == 2
    assert [call["api_key"] for call in transport.calls] == [
        "fake-primary",
        "fake-paid",
    ]


@patch("mkv_episode_matcher.media.gemini_matcher.requests.post")
def test_requests_transport_uses_header_not_url(mock_post):
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"output_text": "{}"},
    )

    RequestsGeminiTransport().send(
        api_key="fake-secret-key",
        body={"model": "test"},
        timeout_seconds=10,
    )

    assert "fake-secret-key" not in mock_post.call_args.args[0]
    assert mock_post.call_args.kwargs["headers"]["x-goog-api-key"] == "fake-secret-key"
