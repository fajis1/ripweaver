"""Path-free, non-authorizing proposals for disc and title content roles."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

_ROLES = frozenset({"tv", "movie", "extra"})
_HINTS = _ROLES | {"extras", "mixed"}
_SOURCES = {"inventory": 1, "label": 1, "database": 2, "content": 3}
_STATUSES = frozenset({"supported", "ambiguous", "unavailable"})
_MAX_TITLES = 10_000
_MAX_EVIDENCE = 40_000
_ROUTES = frozenset({"classify", "tv", "movie", "extra"})
_OUTCOMES = frozenset({
    "running",
    "matched",
    "no_match",
    "review",
    "service_failed",
    "interrupted",
})


class RoutingError(ValueError):
    """A routing proposal or revision is invalid."""


@dataclass(frozen=True)
class TitleEvidence:
    title_index: int
    source: str
    status: str
    role: str | None = None

    def __post_init__(self) -> None:
        if type(self.title_index) is not int or not 0 <= self.title_index < _MAX_TITLES:
            raise RoutingError("Routing evidence title index is invalid")
        if (
            not isinstance(self.source, str)
            or self.source not in _SOURCES
            or not isinstance(self.status, str)
            or self.status not in _STATUSES
        ):
            raise RoutingError("Routing evidence source or status is invalid")
        if (
            self.status == "supported"
            and (not isinstance(self.role, str) or self.role not in _ROLES)
        ) or (self.status != "supported" and self.role is not None):
            raise RoutingError("Routing evidence role is invalid")


@dataclass(frozen=True)
class TitleRole:
    title_index: int
    role: str
    reason: str


@dataclass(frozen=True)
class RouteAttempt:
    title_index: int
    revision: int
    route: str
    outcome: str

    def __post_init__(self) -> None:
        if type(self.title_index) is not int or not 0 <= self.title_index < _MAX_TITLES:
            raise RoutingError("Route attempt title index is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise RoutingError("Route attempt revision is invalid")
        if (
            not isinstance(self.route, str)
            or self.route not in _ROUTES
            or not isinstance(self.outcome, str)
            or self.outcome not in _OUTCOMES
        ):
            raise RoutingError("Route attempt route or outcome is invalid")


@dataclass(frozen=True)
class DiscAssessment:
    inventory_fingerprint: str
    title_indexes: tuple[int, ...]
    user_hint: str | None = None
    evidence: tuple[TitleEvidence, ...] = ()
    revision: int = 1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.inventory_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{16}", self.inventory_fingerprint) is None
        ):
            raise RoutingError("Routing fingerprint is invalid")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise RoutingError("Routing schema is unsupported")
        if type(self.revision) is not int or not 1 <= self.revision <= 2**31 - 1:
            raise RoutingError("Routing revision is invalid")
        if (
            not isinstance(self.title_indexes, tuple)
            or not 1 <= len(self.title_indexes) <= _MAX_TITLES
            or any(
                type(i) is not int or not 0 <= i < _MAX_TITLES
                for i in self.title_indexes
            )
            or tuple(sorted(set(self.title_indexes))) != self.title_indexes
        ):
            raise RoutingError("Routing inventory is invalid")
        if self.user_hint is not None and (
            not isinstance(self.user_hint, str) or self.user_hint not in _HINTS
        ):
            raise RoutingError("Routing hint is invalid")
        if (
            not isinstance(self.evidence, tuple)
            or len(self.evidence) > _MAX_EVIDENCE
            or any(not isinstance(item, TitleEvidence) for item in self.evidence)
            or len(set(self.evidence)) != len(self.evidence)
            or any(item.title_index not in self.title_indexes for item in self.evidence)
        ):
            raise RoutingError("Routing evidence is invalid")

    def title_roles(self) -> tuple[TitleRole, ...]:
        by_title: dict[int, list[TitleEvidence]] = {}
        for item in self.evidence:
            if item.status == "supported":
                by_title.setdefault(item.title_index, []).append(item)
        roles = []
        for index in self.title_indexes:
            candidates = by_title.get(index, [])
            priority = max((_SOURCES[item.source] for item in candidates), default=0)
            strongest = {
                item.role for item in candidates if _SOURCES[item.source] == priority
            }
            if len(strongest) == 1:
                roles.append(
                    TitleRole(index, next(iter(strongest)), "supported_evidence")
                )
            elif strongest:
                roles.append(TitleRole(index, "conflicting", "equal_strength_conflict"))
            else:
                roles.append(TitleRole(index, "unknown", "insufficient_evidence"))
        return tuple(roles)

    @property
    def composition(self) -> str:
        roles = {item.role for item in self.title_roles()} - {"unknown", "conflicting"}
        if not roles:
            return "unknown"
        if {"tv", "movie"} <= roles:
            return "mixed"
        if "tv" in roles:
            return "tv_with_extras" if "extra" in roles else "tv"
        if "movie" in roles:
            return "movies_with_extras" if "extra" in roles else "movies"
        return "extras"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["title_indexes"] = list(self.title_indexes)
        payload["evidence"] = [
            asdict(item)
            for item in sorted(
                self.evidence,
                key=lambda item: (
                    item.title_index,
                    item.source,
                    item.status,
                    item.role or "",
                ),
            )
        ]
        return payload

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: object) -> DiscAssessment:
        required = {
            "inventory_fingerprint",
            "title_indexes",
            "user_hint",
            "evidence",
            "revision",
            "schema_version",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise RoutingError("Routing assessment structure is invalid")
        indexes = value["title_indexes"]
        evidence = value["evidence"]
        if not isinstance(indexes, list) or not isinstance(evidence, list):
            raise RoutingError("Routing assessment sequences are invalid")
        if len(indexes) > _MAX_TITLES or len(evidence) > _MAX_EVIDENCE:
            raise RoutingError("Routing assessment exceeds its bounds")
        try:
            items = tuple(
                TitleEvidence(**item)
                if isinstance(item, dict)
                and set(item) == {"title_index", "source", "status", "role"}
                else _invalid_evidence()
                for item in evidence
            )
            return cls(**{**value, "title_indexes": tuple(indexes), "evidence": items})
        except (TypeError, KeyError) as exc:
            raise RoutingError("Routing assessment evidence is invalid") from exc


def _invalid_evidence() -> TitleEvidence:
    raise RoutingError("Routing assessment evidence is invalid")


def assessment_from_contract(payload: object) -> DiscAssessment | None:
    """Read a bound proposal; a fully legacy contract has no proposal."""

    if not isinstance(payload, dict):
        raise RoutingError("Routing contract is invalid")
    context = payload.get("media_context")
    if not isinstance(context, dict):
        raise RoutingError("Routing context is invalid")
    fields = (
        "routing_assessment",
        "routing_assessment_digest",
        "routing_assessment_revision",
    )
    present = tuple(field in context for field in fields)
    if not any(present):
        return None
    if not all(present):
        raise RoutingError("Routing contract binding is incomplete")
    assessment = DiscAssessment.from_dict(context["routing_assessment"])
    if (
        context["routing_assessment_digest"] != assessment.digest
        or context["routing_assessment_revision"] != assessment.revision
        or payload.get("disc_fingerprint") != assessment.inventory_fingerprint
        or type(payload.get("title_index")) is not int
        or payload["title_index"] not in assessment.title_indexes
    ):
        raise RoutingError("Routing contract binding is inconsistent")
    return assessment
