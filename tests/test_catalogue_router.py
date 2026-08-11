from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.routers import catalogue
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.disc.ripweaver_catalogue import (
    CatalogueCapabilities,
    CatalogueUsage,
    InstallationRegistration,
    SupportCheckout,
    SupportPolicy,
)


class _ConfigManager:
    def __init__(self, config: Config) -> None:
        self._config = config

    def load(self) -> Config:
        return self._config


def _policy() -> SupportPolicy:
    return SupportPolicy(
        policy_version="2026-08-10",
        terms_version="2026-08-10",
        minimum_amount_cents=1_000,
        minimum_rate_cents=1,
        maximum_rate_cents=100,
        default_rate_cents=10,
        payments_enabled=False,
        support_message="Support RipWeaver and receive automatic lookup credits.",
        availability_disclosure="Best-effort synthetic disclosure.",
        refund_disclosure="Synthetic final-payment disclosure.",
    )


def _usage() -> CatalogueUsage:
    return CatalogueUsage(
        monthly_limit=10,
        monthly_used=2,
        monthly_remaining=8,
        contribution_credits=1,
        purchased_credits=0,
        total_automatic_remaining=9,
    )


def _capabilities(**overrides) -> CatalogueCapabilities:
    values = {
        "schema_version": 3,
        "service_version": "0.1.0",
        "public_lookup": False,
        "installation_registration": True,
        "metered_lookup": True,
        "manual_lookup_after_prompt": True,
        "contribution_credits": True,
        "support_checkout": False,
        "authenticated_submissions": True,
        "automatic_piecewise_consensus": True,
        "provisional_help": True,
        "independent_quorum": 2,
        "human_moderation_required": False,
        "attachments_accepted": False,
        "media_accepted": False,
    }
    values.update(overrides)
    return CatalogueCapabilities(**values)


def test_catalogue_status_is_inert_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        catalogue,
        "get_config_manager",
        lambda: _ConfigManager(Config(ripweaver_catalogue_enabled=False)),
    )
    monkeypatch.setattr(
        catalogue,
        "_client_and_token",
        lambda **_kwargs: pytest.fail("disabled status must not contact the service"),
    )

    assert catalogue.catalogue_status() == {
        "enabled": False,
        "connected": False,
        "compatible": None,
        "registered": False,
        "contributions_enabled": False,
        "contribution_outbox": None,
        "capabilities": None,
        "policy": None,
        "usage": None,
    }


def test_registration_stores_server_token_only_through_credential_store(
    monkeypatch,
) -> None:
    stored: list[tuple[str, str]] = []

    class _Client:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "https://api.ripweaver.com"

        def register(self) -> InstallationRegistration:
            return InstallationRegistration(
                installation_id="synthetic-installation",
                access_token="rwc_synthetic_token_that_is_long_enough_1234567890",
            )

    monkeypatch.setattr(
        catalogue,
        "get_config_manager",
        lambda: _ConfigManager(Config(ripweaver_catalogue_enabled=True)),
    )
    monkeypatch.setattr(
        catalogue,
        "load_environment_settings",
        lambda: SimpleNamespace(ripweaver_catalogue_token=None),
    )
    monkeypatch.setattr(catalogue, "RipWeaverCatalogueClient", _Client)
    monkeypatch.setattr(
        catalogue, "store_credential", lambda name, value: stored.append((name, value))
    )

    _client, token = catalogue._client_and_token(register=True)

    assert token == "rwc_synthetic_token_that_is_long_enough_1234567890"
    assert stored == [("ripweaver-catalogue", token)]


def test_status_returns_policy_and_usage_without_exposing_token(monkeypatch) -> None:
    class _Client:
        def capabilities(self) -> CatalogueCapabilities:
            return _capabilities()

        def support_policy(self) -> SupportPolicy:
            return _policy()

        def usage(self, token: str) -> CatalogueUsage:
            assert token == "synthetic-private-token"
            return _usage()

    monkeypatch.setattr(
        catalogue,
        "get_config_manager",
        lambda: _ConfigManager(Config(ripweaver_catalogue_enabled=True)),
    )
    monkeypatch.setattr(
        catalogue,
        "_client_and_token",
        lambda **_kwargs: (_Client(), "synthetic-private-token"),
    )
    monkeypatch.setattr(
        catalogue,
        "get_catalogue_contribution_store",
        lambda: SimpleNamespace(
            status=lambda: {
                "snapshots": 1,
                "pending": 1,
                "sent": 0,
                "superseded": 0,
            }
        ),
    )

    result = catalogue.catalogue_status()

    assert result["connected"] is True
    assert result["compatible"] is True
    assert result["registered"] is True
    assert result["contributions_enabled"] is False
    assert result["contribution_outbox"]["pending"] == 1
    assert result["usage"] == _usage().__dict__
    assert "synthetic-private-token" not in repr(result)


def test_status_reports_reachable_but_incompatible_schema_without_account_calls(
    monkeypatch,
) -> None:
    class _Client:
        def capabilities(self) -> CatalogueCapabilities:
            return _capabilities(schema_version=4)

        def support_policy(self) -> SupportPolicy:
            pytest.fail("incompatible schema must not continue to account metadata")

    monkeypatch.setattr(
        catalogue,
        "get_config_manager",
        lambda: _ConfigManager(
            Config(
                ripweaver_catalogue_enabled=True,
                ripweaver_catalogue_contributions_enabled=True,
            )
        ),
    )
    monkeypatch.setattr(
        catalogue,
        "_client_and_token",
        lambda **_kwargs: (_Client(), "synthetic-private-token"),
    )
    monkeypatch.setattr(
        catalogue,
        "get_catalogue_contribution_store",
        lambda: SimpleNamespace(
            status=lambda: {
                "snapshots": 1,
                "pending": 0,
                "sent": 0,
                "superseded": 0,
            }
        ),
    )

    result = catalogue.catalogue_status()

    assert result["connected"] is True
    assert result["compatible"] is False
    assert result["contributions_enabled"] is False
    assert result["usage"] is None


def test_checkout_requires_terms_and_forwards_only_validated_values(
    monkeypatch,
) -> None:
    rejected = catalogue.SupportCheckoutRequest(
        amount_cents=1_000,
        support_rate_cents=1,
        terms_version="2026-08-10",
        accept_best_effort_terms=False,
    )
    with pytest.raises(HTTPException) as exc_info:
        catalogue.create_support_checkout(
            rejected, idempotency_key="synthetic-checkout-key-0001"
        )
    assert exc_info.value.status_code == 400

    class _Client:
        def create_support_checkout(self, **kwargs) -> SupportCheckout:
            assert kwargs == {
                "token": "synthetic-private-token",
                "amount_cents": 1_000,
                "support_rate_cents": 1,
                "terms_version": "2026-08-10",
                "idempotency_key": "synthetic-checkout-key-0002",
            }
            return SupportCheckout(
                order_id="synthetic-order",
                amount_cents=1_000,
                support_rate_cents=1,
                automatic_lookup_credits=1_000,
                checkout_url="https://checkout.stripe.test/synthetic",
            )

    monkeypatch.setattr(
        catalogue,
        "get_config_manager",
        lambda: _ConfigManager(Config(ripweaver_catalogue_enabled=True)),
    )
    monkeypatch.setattr(
        catalogue,
        "_client_and_token",
        lambda **_kwargs: (_Client(), "synthetic-private-token"),
    )
    accepted = catalogue.SupportCheckoutRequest(
        amount_cents=1_000,
        support_rate_cents=1,
        terms_version="2026-08-10",
        accept_best_effort_terms=True,
    )

    result = catalogue.create_support_checkout(
        accepted, idempotency_key="synthetic-checkout-key-0002"
    )

    assert result["automatic_lookup_credits"] == 1_000
    assert "synthetic-private-token" not in repr(result)
