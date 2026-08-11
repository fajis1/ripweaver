from __future__ import annotations

import pytest

from mkv_episode_matcher.disc.preflight import (
    DiscInventory,
    MakeMKVDrive,
    MakeMKVTitle,
)
from mkv_episode_matcher.disc.ripweaver_catalogue import (
    RipWeaverCatalogueClient,
    RipWeaverCatalogueError,
    RipWeaverCatalogueSupportRequiredError,
    validate_catalogue_url,
)

CONTENT_HASH = "8B6FCE0775F77E41B1EB2E293BA9BA80"


def inventory() -> DiscInventory:
    return DiscInventory(
        drive=MakeMKVDrive(
            index=0,
            visible=2,
            enabled=999,
            flags=1,
            drive_name="<hardware-redacted>",
            disc_name="Synthetic",
            device_name="<device-redacted>",
        ),
        disc_attributes={},
        titles=[MakeMKVTitle(index=4, attributes={16: "00800.mpls", 26: "046,047"})],
        return_code=0,
        started_at="2026-08-10T00:00:00+00:00",
        finished_at="2026-08-10T00:00:01+00:00",
        warnings=[],
    )


def lookup_payload() -> dict[str, object]:
    return {
        "disc": {
            "schema_version": 1,
            "content_hash": CONTENT_HASH,
            "media_type": "bluray",
            "release_name": "Synthetic release",
            "edition": None,
            "titles": [
                {
                    "title_index": 7,
                    "source_file": "00800.mpls",
                    "segment_map": ["46", "47"],
                    "duration_seconds": 1320,
                    "size_bytes": 2_000_000_000,
                    "classification": "episode",
                    "series_name": "Synthetic Series",
                    "season_number": 1,
                    "episode_number": 2,
                    "episode_title": "Second",
                    "movie_title": None,
                    "movie_year": None,
                }
            ],
            "revision": 1,
            "payload_sha256": "a" * 64,
            "status": "reviewed",
        },
        "usage": {
            "monthly_limit": 10,
            "monthly_used": 1,
            "monthly_remaining": 9,
            "contribution_credits": 0,
            "purchased_credits": 0,
            "total_automatic_remaining": 9,
        },
        "credit_source": "monthly",
    }


class Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_lookup_sends_only_hash_token_mode_and_idempotency() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response(200, lookup_payload())

    result = RipWeaverCatalogueClient(
        base_url="https://api.ripweaver.com", request=request
    ).lookup(
        CONTENT_HASH,
        inventory(),
        token="rwc_synthetic-token-value",
        idempotency_key="stable-lookup-key-0001",
    )

    assert result is not None
    assert result.credit_source == "monthly"
    assert result.resolution.episode_assignments[0]["episode"] == 2
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith(CONTENT_HASH)
    assert calls[0][2]["json"] == {"mode": "automatic"}
    assert calls[0][2]["headers"]["Idempotency-Key"] == "stable-lookup-key-0001"


def test_support_required_is_structured_without_response_details() -> None:
    policy = {
        "policy_version": "2026-08-10",
        "terms_version": "2026-08-10",
        "minimum_amount_cents": 1000,
        "minimum_rate_cents": 1,
        "maximum_rate_cents": 100,
        "default_rate_cents": 10,
        "payments_enabled": False,
        "support_message": "Support or continue manually.",
        "availability_disclosure": "Best effort; future availability is not guaranteed.",
        "refund_disclosure": "Generally final, except where required.",
    }
    usage = lookup_payload()["usage"]

    def request(_method, _url, **_kwargs):
        return Response(402, {"detail": {"usage": usage, "policy": policy}})

    client = RipWeaverCatalogueClient(request=request)
    try:
        client.lookup(
            CONTENT_HASH,
            inventory(),
            token="rwc_synthetic-token-value",
            idempotency_key="stable-lookup-key-0002",
        )
    except RipWeaverCatalogueSupportRequiredError as exc:
        assert exc.policy.minimum_rate_cents == 1
        assert exc.usage.monthly_remaining == 9
        assert "response" not in str(exc).casefold()
    else:
        raise AssertionError("support confirmation was not required")


def test_manual_lookup_requires_and_forwards_the_displayed_policy_version() -> None:
    calls: list[dict[str, object]] = []

    def request(_method, _url, **kwargs):
        calls.append(kwargs)
        payload = lookup_payload()
        payload["credit_source"] = "manual"
        return Response(200, payload)

    client = RipWeaverCatalogueClient(request=request)
    with pytest.raises(RipWeaverCatalogueError, match="displayed support prompt"):
        client.lookup(
            CONTENT_HASH,
            inventory(),
            token="rwc_synthetic-token-value",
            idempotency_key="stable-manual-key-0001",
            mode="manual",
        )

    result = client.lookup(
        CONTENT_HASH,
        inventory(),
        token="rwc_synthetic-token-value",
        idempotency_key="stable-manual-key-0002",
        mode="manual",
        support_prompt_version="future-policy-version",
    )

    assert result is not None
    assert result.credit_source == "manual"
    assert calls == [
        {
            "timeout": 15,
            "json": {
                "mode": "manual",
                "support_prompt_version": "future-policy-version",
            },
            "headers": {
                "Authorization": "Bearer rwc_synthetic-token-value",
                "Idempotency-Key": "stable-manual-key-0002",
                "User-Agent": "RipWeaver/catalogue-lookup",
            },
        }
    ]


def test_catalogue_url_requires_https_except_loopback() -> None:
    assert validate_catalogue_url("https://api.ripweaver.com/") == (
        "https://api.ripweaver.com"
    )
    assert validate_catalogue_url("http://127.0.0.1:8080") == ("http://127.0.0.1:8080")


def test_piecewise_consensus_metadata_and_contribution_protocol() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []
    payload = lookup_payload()
    payload["disc"]["schema_version"] = 2
    payload["disc"]["status"] = "consensus"
    payload["disc"]["consensus"] = {
        "quorum": 2,
        "total_items": 2,
        "confirmed_items": 1,
        "unresolved_items": 1,
        "whole_disc_consistent": True,
        "complete": False,
        "items": [
            {
                "title_index": 7,
                "state": "confirmed",
                "support_count": 2,
                "runner_up_count": 0,
                "candidate_count": 1,
            },
            {
                "title_index": 9,
                "state": "disputed",
                "support_count": 1,
                "runner_up_count": 1,
                "candidate_count": 2,
            },
        ],
    }

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/submissions"):
            return Response(
                202,
                {
                    "submission_id": "synthetic-submission",
                    "content_hash": CONTENT_HASH,
                    "payload_sha256": "b" * 64,
                    "status": "accepted",
                    "consensus": {
                        "confirmed_items": 1,
                        "unresolved_items": 1,
                    },
                },
            )
        return Response(200, payload)

    client = RipWeaverCatalogueClient(request=request)
    result = client.lookup(
        CONTENT_HASH,
        inventory(),
        token="private-token",
        idempotency_key="piecewise-lookup-key-0001",
    )
    assert result is not None
    assert result.confirmed_title_count == 1
    assert result.unresolved_title_indexes == (9,)
    assert result.consensus_complete is False
    assert result.resolution.episode_assignments[0]["identification_source"] == (
        "ripweaver-catalogue"
    )

    contribution = {"schema_version": 2, "content_hash": CONTENT_HASH, "titles": []}
    receipt = client.contribute(
        contribution,
        token="private-token",
        idempotency_key="contribution-key-0001",
        client_version="test",
    )
    assert receipt.status == "accepted"
    assert calls[-1][1].endswith("/v1/submissions")
    assert calls[-1][2]["json"] is contribution
    assert calls[-1][2]["headers"]["X-RipWeaver-Version"] == "test"


def test_single_candidate_help_is_returned_only_as_a_non_consensus_hint() -> None:
    help_payload = {
        "schema_version": 2,
        "content_hash": CONTENT_HASH,
        "media_type": "bluray",
        "total_items": 1,
        "items": [
            {
                "title_index": 7,
                "state": "candidate",
                "candidates": [
                    {
                        "title": lookup_payload()["disc"]["titles"][0],
                        "independent_support": 1,
                        "total_observations": 1,
                        "best_match_source": "local_evidence",
                    }
                ],
            }
        ],
    }
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response(200, help_payload)

    result = RipWeaverCatalogueClient(request=request).help(
        CONTENT_HASH, inventory(), token="private-token"
    )

    assert result is not None
    assert result.status == "candidate-help"
    assert result.episode_assignments == (
        {
            "title_index": 4,
            "season": 1,
            "episode": 2,
            "title": "Second",
            "identification_source": "ripweaver-catalogue-help",
        },
    )
    assert calls[0][0] == "GET"
    assert calls[0][2]["headers"]["Authorization"] == "Bearer private-token"
