"""Loopback-only proxy for RipWeaver Catalogue account and support actions."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from mkv_episode_matcher.backend.control_access import require_local_control
from mkv_episode_matcher.backend.dependencies import get_catalogue_contribution_store
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.core.credentials import store_credential
from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.disc.ripweaver_catalogue import (
    RipWeaverCatalogueClient,
    RipWeaverCatalogueError,
)

router = APIRouter(
    prefix="/catalogue",
    tags=["catalogue"],
    dependencies=[Depends(require_local_control)],
)


class SupportCheckoutRequest(BaseModel):
    amount_cents: int = Field(ge=100, le=100_000)
    support_rate_cents: int = Field(ge=1, le=100)
    terms_version: str = Field(min_length=1, max_length=32)
    accept_best_effort_terms: bool = False


def _client_and_token(*, register: bool) -> tuple[RipWeaverCatalogueClient, str | None]:
    config = get_config_manager().load()
    client = RipWeaverCatalogueClient(base_url=config.ripweaver_catalogue_url)
    token = load_environment_settings().ripweaver_catalogue_token
    if not token and register:
        registration = client.register()
        store_credential("ripweaver-catalogue", registration.access_token)
        token = registration.access_token
    return client, token


@router.get("/status")
def catalogue_status() -> dict[str, object]:
    config = get_config_manager().load()
    if not config.ripweaver_catalogue_enabled:
        return {
            "enabled": False,
            "connected": False,
            "registered": False,
            "contributions_enabled": False,
            "contribution_outbox": None,
            "policy": None,
            "usage": None,
        }
    try:
        client, token = _client_and_token(register=False)
        policy = client.support_policy()
        usage = client.usage(token) if token else None
        return {
            "enabled": True,
            "connected": True,
            "registered": token is not None,
            "contributions_enabled": (config.ripweaver_catalogue_contributions_enabled),
            "contribution_outbox": get_catalogue_contribution_store().status(),
            "policy": policy.__dict__,
            "usage": usage.__dict__ if usage else None,
        }
    except RipWeaverCatalogueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/register")
def register_catalogue_installation() -> dict[str, object]:
    config = get_config_manager().load()
    if not config.ripweaver_catalogue_enabled:
        raise HTTPException(status_code=409, detail="RipWeaver Catalogue is disabled")
    try:
        _client, token = _client_and_token(register=True)
    except RipWeaverCatalogueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"registered": token is not None}


@router.post("/support/checkout")
def create_support_checkout(
    request: SupportCheckoutRequest,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=200)
    ],
) -> dict[str, object]:
    if not request.accept_best_effort_terms:
        raise HTTPException(
            status_code=400,
            detail="Best-effort support terms must be accepted before checkout",
        )
    config = get_config_manager().load()
    if not config.ripweaver_catalogue_enabled:
        raise HTTPException(status_code=409, detail="RipWeaver Catalogue is disabled")
    try:
        client, token = _client_and_token(register=True)
        if token is None:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue installation registration failed"
            )
        checkout = client.create_support_checkout(
            token=token,
            amount_cents=request.amount_cents,
            support_rate_cents=request.support_rate_cents,
            terms_version=request.terms_version,
            idempotency_key=idempotency_key,
        )
    except RipWeaverCatalogueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return checkout.__dict__
