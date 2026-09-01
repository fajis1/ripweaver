"""Privacy-bounded client for the public RipWeaver disc catalogue."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import requests

from mkv_episode_matcher.disc.preflight import DiscInventory
from mkv_episode_matcher.disc.thediscdb import (
    CONTENT_HASH_PATTERN,
    TheDiscDbDisc,
    TheDiscDbResolution,
    TheDiscDbTitle,
    enrich_inventory,
)


class RipWeaverCatalogueError(RuntimeError):
    """A path-, token-, and response-body-free catalogue failure."""


class RipWeaverCatalogueAuthenticationError(RipWeaverCatalogueError):
    """The installation token is missing or no longer accepted."""


@dataclass(frozen=True)
class CatalogueUsage:
    monthly_limit: int
    monthly_used: int
    monthly_remaining: int
    contribution_credits: int
    purchased_credits: int
    total_automatic_remaining: int


@dataclass(frozen=True)
class SupportPolicy:
    policy_version: str
    terms_version: str
    minimum_amount_cents: int
    minimum_rate_cents: int
    maximum_rate_cents: int
    default_rate_cents: int
    payments_enabled: bool
    support_message: str
    availability_disclosure: str
    refund_disclosure: str


class RipWeaverCatalogueSupportRequiredError(RipWeaverCatalogueError):
    def __init__(self, *, usage: CatalogueUsage, policy: SupportPolicy) -> None:
        self.usage = usage
        self.policy = policy
        super().__init__("Automatic catalogue lookup requires visible confirmation")


@dataclass(frozen=True)
class InstallationRegistration:
    installation_id: str
    access_token: str


@dataclass(frozen=True)
class CatalogueCapabilities:
    schema_version: int
    service_version: str
    public_lookup: bool
    installation_registration: bool
    metered_lookup: bool
    manual_lookup_after_prompt: bool
    contribution_credits: bool
    support_checkout: bool
    authenticated_submissions: bool
    automatic_piecewise_consensus: bool
    provisional_help: bool
    independent_quorum: int
    human_moderation_required: bool
    submissions_quarantined: bool
    quarantine_publication_enabled: bool
    attachments_accepted: bool
    media_accepted: bool

    @property
    def compatible(self) -> bool:
        return (
            self.schema_version == 4
            and self.public_lookup is False
            and self.installation_registration is True
            and self.metered_lookup is True
            and self.manual_lookup_after_prompt is True
            and self.contribution_credits is False
            and self.authenticated_submissions is True
            and self.automatic_piecewise_consensus is False
            and self.provisional_help is True
            and self.independent_quorum == 2
            and self.human_moderation_required is True
            and self.submissions_quarantined is True
            and self.quarantine_publication_enabled is False
            and self.attachments_accepted is False
            and self.media_accepted is False
        )


@dataclass(frozen=True)
class CatalogueLookup:
    resolution: TheDiscDbResolution
    usage: CatalogueUsage
    credit_source: Literal["monthly", "contribution", "purchased", "manual", "cached"]
    confirmed_title_count: int = 0
    unresolved_title_indexes: tuple[int, ...] = ()
    consensus_complete: bool = True
    whole_disc_consistent: bool = True


@dataclass(frozen=True)
class ContributionReceipt:
    submission_id: str
    content_hash: str
    payload_sha256: str
    status: Literal["pending", "rejected"]
    validation_version: int
    publication_eligible: Literal[False]


@dataclass(frozen=True)
class SupportCheckout:
    order_id: str
    amount_cents: int
    support_rate_cents: int
    automatic_lookup_credits: int
    checkout_url: str


def validate_catalogue_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    local_development = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
    }
    if not (parsed.scheme == "https" or local_development):
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue requires HTTPS outside local development"
        )
    if parsed.username or parsed.password or not parsed.hostname or parsed.query:
        raise RipWeaverCatalogueError("RipWeaver Catalogue URL is invalid")
    return normalized


def _required_int(value: object, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RipWeaverCatalogueError(
            f"RipWeaver Catalogue returned invalid {field} metadata"
        )
    return value


def _required_string(value: object, field: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RipWeaverCatalogueError(
            f"RipWeaver Catalogue returned invalid {field} metadata"
        )
    return value.strip()


def _required_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise RipWeaverCatalogueError(
            f"RipWeaver Catalogue returned invalid {field} metadata"
        )
    return value


def _quarantine_receipt(
    value: object, *, expected_content_hash: str
) -> ContributionReceipt:
    if not isinstance(value, dict) or value.get("status") not in {
        "pending",
        "rejected",
    }:
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue submission was not quarantined"
        )
    if value.get("consensus") is not None:
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue quarantine returned forbidden consensus metadata"
        )
    validation_version = _required_int(
        value.get("validation_version"), "validation version", minimum=1
    )
    if validation_version != 1:
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue returned an unsupported validation version"
        )
    if _required_bool(value.get("publication_eligible"), "publication eligibility"):
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue returned an unsafe publication-eligible receipt"
        )
    returned_hash = _required_string(value.get("content_hash"), "content hash")
    if returned_hash.upper() != expected_content_hash.upper():
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue quarantined another disc submission"
        )
    payload_sha256 = _required_string(value.get("payload_sha256"), "payload digest")
    if re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None:
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue returned an invalid payload digest"
        )
    status: Literal["pending", "rejected"] = (
        "pending" if value["status"] == "pending" else "rejected"
    )
    return ContributionReceipt(
        submission_id=_required_string(value.get("submission_id"), "submission ID"),
        content_hash=returned_hash.upper(),
        payload_sha256=payload_sha256,
        status=status,
        validation_version=validation_version,
        publication_eligible=False,
    )


def _usage(value: object) -> CatalogueUsage:
    if not isinstance(value, dict):
        raise RipWeaverCatalogueError("RipWeaver Catalogue returned invalid usage")
    return CatalogueUsage(
        monthly_limit=_required_int(value.get("monthly_limit"), "usage"),
        monthly_used=_required_int(value.get("monthly_used"), "usage"),
        monthly_remaining=_required_int(value.get("monthly_remaining"), "usage"),
        contribution_credits=_required_int(value.get("contribution_credits"), "usage"),
        purchased_credits=_required_int(value.get("purchased_credits"), "usage"),
        total_automatic_remaining=_required_int(
            value.get("total_automatic_remaining"), "usage"
        ),
    )


def _policy(value: object) -> SupportPolicy:
    if not isinstance(value, dict):
        raise RipWeaverCatalogueError(
            "RipWeaver Catalogue returned invalid support policy"
        )
    return SupportPolicy(
        policy_version=_required_string(value.get("policy_version"), "policy"),
        terms_version=_required_string(value.get("terms_version"), "policy"),
        minimum_amount_cents=_required_int(
            value.get("minimum_amount_cents"), "policy", minimum=1
        ),
        minimum_rate_cents=_required_int(
            value.get("minimum_rate_cents"), "policy", minimum=1
        ),
        maximum_rate_cents=_required_int(
            value.get("maximum_rate_cents"), "policy", minimum=1
        ),
        default_rate_cents=_required_int(
            value.get("default_rate_cents"), "policy", minimum=1
        ),
        payments_enabled=value.get("payments_enabled") is True,
        support_message=_required_string(value.get("support_message"), "policy"),
        availability_disclosure=_required_string(
            value.get("availability_disclosure"), "policy"
        ),
        refund_disclosure=_required_string(value.get("refund_disclosure"), "policy"),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _disc_from_record(value: object, content_hash: str) -> TheDiscDbDisc:
    if not isinstance(value, dict):
        raise RipWeaverCatalogueError("RipWeaver Catalogue returned an invalid disc")
    returned_hash = _required_string(value.get("content_hash"), "content hash")
    if returned_hash.upper() != content_hash:
        raise RipWeaverCatalogueError("RipWeaver Catalogue returned another disc")
    raw_titles = value.get("titles")
    if not isinstance(raw_titles, list) or not raw_titles:
        raise RipWeaverCatalogueError("RipWeaver Catalogue returned no disc titles")
    titles: list[TheDiscDbTitle] = []
    series_names: list[str] = []
    movie_names: list[str] = []
    for item in raw_titles:
        if not isinstance(item, dict):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue returned an invalid title"
            )
        classification = _required_string(item.get("classification"), "title")
        series_name = item.get("series_name")
        movie_title = item.get("movie_title")
        if isinstance(series_name, str) and series_name.strip():
            series_names.append(series_name.strip())
        if isinstance(movie_title, str) and movie_title.strip():
            movie_names.append(movie_title.strip())
        raw_segments = item.get("segment_map")
        if not isinstance(raw_segments, list) or not all(
            isinstance(segment, str) for segment in raw_segments
        ):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue returned an invalid segment map"
            )
        titles.append(
            TheDiscDbTitle(
                index=_required_int(item.get("title_index"), "title index"),
                source_file=_required_string(item.get("source_file"), "source file"),
                segment_map=",".join(raw_segments) if raw_segments else None,
                duration=(
                    str(item["duration_seconds"])
                    if _optional_int(item.get("duration_seconds")) is not None
                    else None
                ),
                size=_optional_int(item.get("size_bytes")),
                item_title=(
                    item.get("episode_title")
                    if isinstance(item.get("episode_title"), str)
                    else movie_title
                    if isinstance(movie_title, str)
                    else item.get("display_title")
                    if isinstance(item.get("display_title"), str)
                    else None
                ),
                item_type=classification,
                season=_optional_int(item.get("season_number")),
                episode=_optional_int(item.get("episode_number")),
            )
        )
    media_title = (
        series_names[0]
        if series_names and len(set(series_names)) == 1
        else movie_names[0]
        if movie_names and len(set(movie_names)) == 1
        else _required_string(value.get("release_name") or "Reviewed disc", "release")
    )
    return TheDiscDbDisc(
        media_title=media_title,
        media_type="Series" if series_names else "Movie" if movie_names else None,
        tmdb_id=None,
        release_title=(
            value.get("release_name")
            if isinstance(value.get("release_name"), str)
            else None
        ),
        disc_index=1,
        disc_name=None,
        disc_format=_required_string(value.get("media_type"), "media type"),
        titles=tuple(titles),
    )


class RipWeaverCatalogueClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.ripweaver.com",
        request: Callable[..., Any] = requests.request,
    ) -> None:
        self._base_url = validate_catalogue_url(base_url)
        self._request = request

    def _send(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = self._request(
                method,
                f"{self._base_url}{path}",
                timeout=15,
                **kwargs,
            )
        except (requests.RequestException, OSError) as exc:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue request failed safely ({type(exc).__name__})"
            ) from exc
        return response

    def capabilities(self) -> CatalogueCapabilities:
        response = self._send("GET", "/v1/schema")
        if response.status_code != 200:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue schema check failed (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue schema check returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue schema check returned invalid metadata"
            )
        schema_version = _required_int(
            payload.get("schema_version"), "schema version", minimum=1
        )
        capabilities = CatalogueCapabilities(
            schema_version=schema_version,
            service_version=_required_string(
                payload.get("service_version"), "service version", maximum=64
            ),
            public_lookup=_required_bool(payload.get("public_lookup"), "schema"),
            installation_registration=_required_bool(
                payload.get("installation_registration"), "schema"
            ),
            metered_lookup=_required_bool(payload.get("metered_lookup"), "schema"),
            manual_lookup_after_prompt=_required_bool(
                payload.get("manual_lookup_after_prompt"), "schema"
            ),
            contribution_credits=_required_bool(
                payload.get("contribution_credits"), "schema"
            ),
            support_checkout=_required_bool(payload.get("support_checkout"), "schema"),
            authenticated_submissions=_required_bool(
                payload.get("authenticated_submissions"), "schema"
            ),
            automatic_piecewise_consensus=_required_bool(
                payload.get("automatic_piecewise_consensus"), "schema"
            ),
            provisional_help=_required_bool(payload.get("provisional_help"), "schema"),
            independent_quorum=_required_int(
                payload.get("independent_quorum"), "schema quorum", minimum=1
            ),
            human_moderation_required=_required_bool(
                payload.get("human_moderation_required"), "schema"
            ),
            submissions_quarantined=(
                _required_bool(payload.get("submissions_quarantined"), "schema")
                if schema_version >= 4
                else False
            ),
            quarantine_publication_enabled=(
                _required_bool(payload.get("quarantine_publication_enabled"), "schema")
                if schema_version >= 4
                else True
            ),
            attachments_accepted=_required_bool(
                payload.get("attachments_accepted"), "schema"
            ),
            media_accepted=_required_bool(payload.get("media_accepted"), "schema"),
        )
        return capabilities

    def register(self) -> InstallationRegistration:
        if not self.capabilities().compatible:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue protocol is not compatible with this desktop"
            )
        response = self._send(
            "POST",
            "/v1/installations/register",
            headers={"User-Agent": "RipWeaver/catalogue-registration"},
        )
        if response.status_code != 201:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue registration failed (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue registration returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue registration returned invalid metadata"
            )
        token = _required_string(payload.get("access_token"), "access token")
        if re.fullmatch(r"rwc_[A-Za-z0-9_-]{32,100}", token) is None:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue registration returned an invalid token"
            )
        return InstallationRegistration(
            installation_id=_required_string(
                payload.get("installation_id"), "installation ID"
            ),
            access_token=token,
        )

    def support_policy(self) -> SupportPolicy:
        response = self._send("GET", "/v1/support/policy")
        if response.status_code != 200:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue policy failed (HTTP {response.status_code})"
            )
        try:
            return _policy(response.json())
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue policy returned invalid JSON"
            ) from exc

    def usage(self, token: str) -> CatalogueUsage:
        response = self._send(
            "GET",
            "/v1/account/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            raise RipWeaverCatalogueAuthenticationError(
                "RipWeaver Catalogue installation authentication failed"
            )
        if response.status_code != 200:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue usage failed (HTTP {response.status_code})"
            )
        try:
            return _usage(response.json())
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue usage returned invalid JSON"
            ) from exc

    def lookup(  # noqa: C901 - validates a deliberately narrow remote response
        self,
        content_hash: str,
        inventory: DiscInventory,
        *,
        token: str,
        idempotency_key: str,
        mode: Literal["automatic", "manual"] = "automatic",
        support_prompt_version: str | None = None,
    ) -> CatalogueLookup | None:
        normalized_hash = content_hash.strip().upper()
        if CONTENT_HASH_PATTERN.fullmatch(normalized_hash) is None:
            raise RipWeaverCatalogueError("RipWeaver Catalogue content hash is invalid")
        body: dict[str, object] = {"mode": mode}
        if mode == "manual":
            if not support_prompt_version:
                raise RipWeaverCatalogueError(
                    "Manual catalogue lookup requires the displayed support prompt"
                )
            body["support_prompt_version"] = support_prompt_version
        response = self._send(
            "POST",
            f"/v1/lookups/discs/{normalized_hash}",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": idempotency_key,
                "User-Agent": "RipWeaver/catalogue-lookup",
            },
        )
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise RipWeaverCatalogueAuthenticationError(
                "RipWeaver Catalogue installation authentication failed"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue lookup returned invalid JSON"
            ) from exc
        if response.status_code == 402:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(detail, dict):
                raise RipWeaverCatalogueError(
                    "RipWeaver Catalogue returned an invalid support prompt"
                )
            raise RipWeaverCatalogueSupportRequiredError(
                usage=_usage(detail.get("usage")),
                policy=_policy(detail.get("policy")),
            )
        if response.status_code != 200 or not isinstance(payload, dict):
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue lookup failed (HTTP {response.status_code})"
            )
        raw_disc = payload.get("disc")
        disc = _disc_from_record(raw_disc, normalized_hash)
        credit_source = payload.get("credit_source")
        if credit_source not in {
            "monthly",
            "contribution",
            "purchased",
            "manual",
            "cached",
        }:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue returned an invalid credit source"
            )
        resolution = enrich_inventory(inventory, disc)
        resolution = TheDiscDbResolution(
            status=resolution.status,
            media_title=resolution.media_title,
            media_type=resolution.media_type,
            tmdb_id=resolution.tmdb_id,
            episode_assignments=tuple(
                {**assignment, "identification_source": "ripweaver-catalogue"}
                for assignment in resolution.episode_assignments
            ),
            matched_title_indexes=resolution.matched_title_indexes,
            unmatched_title_indexes=resolution.unmatched_title_indexes,
        )
        consensus = raw_disc.get("consensus") if isinstance(raw_disc, dict) else None
        unresolved_indexes: tuple[int, ...] = ()
        consensus_complete = True
        whole_disc_consistent = True
        if isinstance(consensus, dict):
            raw_items = consensus.get("items")
            if not isinstance(raw_items, list):
                raise RipWeaverCatalogueError(
                    "RipWeaver Catalogue returned invalid consensus metadata"
                )
            unresolved_indexes = tuple(
                sorted(
                    _required_int(item.get("title_index"), "consensus title")
                    for item in raw_items
                    if isinstance(item, dict) and item.get("state") != "confirmed"
                )
            )
            consensus_complete = consensus.get("complete") is True
            whole_disc_consistent = consensus.get("whole_disc_consistent") is True
        return CatalogueLookup(
            resolution=resolution,
            usage=_usage(payload.get("usage")),
            credit_source=credit_source,
            confirmed_title_count=len(disc.titles),
            unresolved_title_indexes=unresolved_indexes,
            consensus_complete=consensus_complete,
            whole_disc_consistent=whole_disc_consistent,
        )

    def contribute(
        self,
        payload: dict[str, object],
        *,
        token: str,
        idempotency_key: str,
        client_version: str,
    ) -> ContributionReceipt:
        content_hash = payload.get("content_hash")
        if (
            payload.get("schema_version") != 2
            or not isinstance(content_hash, str)
            or CONTENT_HASH_PATTERN.fullmatch(content_hash.upper()) is None
        ):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue contribution metadata is invalid"
            )
        response = self._send(
            "POST",
            "/v1/submissions",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": idempotency_key,
                "X-RipWeaver-Version": client_version,
                "User-Agent": "RipWeaver/catalogue-contribution",
            },
        )
        if response.status_code == 401:
            raise RipWeaverCatalogueAuthenticationError(
                "RipWeaver Catalogue installation authentication failed"
            )
        if response.status_code != 202:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue contribution failed (HTTP {response.status_code})"
            )
        try:
            receipt = response.json()
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue contribution returned invalid JSON"
            ) from exc
        return _quarantine_receipt(
            receipt,
            expected_content_hash=content_hash,
        )

    def create_support_checkout(
        self,
        *,
        token: str,
        amount_cents: int,
        support_rate_cents: int,
        terms_version: str,
        idempotency_key: str,
    ) -> SupportCheckout:
        response = self._send(
            "POST",
            "/v1/support/checkout",
            json={
                "amount_cents": amount_cents,
                "support_rate_cents": support_rate_cents,
                "terms_version": terms_version,
                "accept_best_effort_terms": True,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": idempotency_key,
                "User-Agent": "RipWeaver/support-checkout",
            },
        )
        if response.status_code == 401:
            raise RipWeaverCatalogueAuthenticationError(
                "RipWeaver Catalogue installation authentication failed"
            )
        if response.status_code != 200:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue checkout failed (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue checkout returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue checkout returned invalid metadata"
            )
        return SupportCheckout(
            order_id=_required_string(payload.get("order_id"), "order ID"),
            amount_cents=_required_int(
                payload.get("amount_cents"), "support amount", minimum=1
            ),
            support_rate_cents=_required_int(
                payload.get("support_rate_cents"), "support rate", minimum=1
            ),
            automatic_lookup_credits=_required_int(
                payload.get("automatic_lookup_credits"), "lookup credits", minimum=1
            ),
            checkout_url=_required_string(payload.get("checkout_url"), "checkout URL"),
        )

    def help(  # noqa: C901 - validates untrusted candidate evidence defensively
        self,
        content_hash: str,
        inventory: DiscInventory,
        *,
        token: str,
    ) -> TheDiscDbResolution | None:
        """Return single-candidate hints; callers must not treat them as consensus."""

        normalized_hash = content_hash.strip().upper()
        if CONTENT_HASH_PATTERN.fullmatch(normalized_hash) is None:
            raise RipWeaverCatalogueError("RipWeaver Catalogue content hash is invalid")
        response = self._send(
            "GET",
            f"/v1/help/discs/{normalized_hash}",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "RipWeaver/catalogue-help",
            },
        )
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise RipWeaverCatalogueAuthenticationError(
                "RipWeaver Catalogue installation authentication failed"
            )
        if response.status_code != 200:
            raise RipWeaverCatalogueError(
                f"RipWeaver Catalogue help failed (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue help returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue help returned invalid metadata"
            )
        returned_hash = _required_string(payload.get("content_hash"), "content hash")
        raw_items = payload.get("items")
        if returned_hash.upper() != normalized_hash or not isinstance(raw_items, list):
            raise RipWeaverCatalogueError(
                "RipWeaver Catalogue help returned another disc"
            )
        candidate_titles: list[dict[str, object]] = []
        for item in raw_items:
            if not isinstance(item, dict) or not isinstance(
                item.get("candidates"), list
            ):
                raise RipWeaverCatalogueError(
                    "RipWeaver Catalogue help returned invalid candidates"
                )
            candidates = item["candidates"]
            if item.get("state") == "confirmed" or len(candidates) != 1:
                continue
            candidate = candidates[0]
            if (
                not isinstance(candidate, dict)
                or _required_int(
                    candidate.get("independent_support"), "candidate support"
                )
                < 1
                or not isinstance(candidate.get("title"), dict)
            ):
                continue
            candidate_titles.append(candidate["title"])
        if not candidate_titles:
            return None
        disc = _disc_from_record(
            {
                "content_hash": normalized_hash,
                "media_type": payload.get("media_type"),
                "release_name": "Candidate disc",
                "titles": candidate_titles,
            },
            normalized_hash,
        )
        resolution = enrich_inventory(inventory, disc)
        if not resolution.matched_title_indexes:
            return None
        return TheDiscDbResolution(
            status="candidate-help",
            media_title=resolution.media_title,
            media_type=resolution.media_type,
            tmdb_id=None,
            episode_assignments=tuple(
                {
                    **assignment,
                    "identification_source": "ripweaver-catalogue-help",
                }
                for assignment in resolution.episode_assignments
            ),
            matched_title_indexes=resolution.matched_title_indexes,
            unmatched_title_indexes=resolution.unmatched_title_indexes,
        )
