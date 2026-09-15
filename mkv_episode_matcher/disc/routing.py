"""Saved-data disc routing proposals; never identity or execution authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from mkv_episode_matcher.disc.content_policy import identification_order

_ROLES = frozenset({"tv", "movie", "extras"})
_HINTS = _ROLES | {"mixed", None}
# Structural clues prioritize investigation; validated title evidence takes
# precedence. Neither grants permission to name, process, or move a file.
_SOURCE_PRIORITY = {"inventory": 1, "label": 1, "database": 2, "content": 3}
_MAX_TITLES = 10_000
_MAX_EVIDENCE = 40_000


class RoutingError(ValueError):
    """A saved routing proposal is invalid or stale."""


def assessment_from_contract(payload: object) -> DiscRoutingAssessment:
    """Validate the routing identity at an immutable contract boundary."""
    if not isinstance(payload, dict):
        raise RoutingError("Routing contract is invalid")
    context = payload.get("media_context")
    if not isinstance(context, dict):
        raise RoutingError("Routing context is unavailable")
    assessment = DiscRoutingAssessment.from_dict(context.get("routing_assessment"))
    if (
        context.get("routing_assessment_digest") != assessment.digest
        or context.get("routing_assessment_revision") != assessment.revision
        or payload.get("disc_fingerprint") != assessment.inventory_fingerprint
        or type(payload.get("title_index")) is not int
        or payload["title_index"] not in assessment.title_indexes
    ):
        raise RoutingError("Routing contract identity is inconsistent")
    return assessment


@dataclass(frozen=True)
class TitleRoutingEvidence:
    title_index: int
    role: str
    source: str

    def __post_init__(self) -> None:
        if type(self.title_index) is not int or not 0 <= self.title_index < _MAX_TITLES:
            raise RoutingError("Routing evidence title index is invalid")
        if not isinstance(self.role, str) or self.role not in _ROLES:
            raise RoutingError("Routing evidence role is invalid")
        if not isinstance(self.source, str) or self.source not in _SOURCE_PRIORITY:
            raise RoutingError("Routing evidence source is invalid")


@dataclass(frozen=True)
class TitleRoute:
    title_index: int
    role: str
    reason: str
    investigation_order: tuple[str, ...]


@dataclass(frozen=True)
class DiscRoutingAssessment:
    inventory_fingerprint: str
    title_indexes: tuple[int, ...]
    user_hint: str | None = None
    evidence: tuple[TitleRoutingEvidence, ...] = ()
    revision: int = 1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.inventory_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{16}", self.inventory_fingerprint) is None
        ):
            raise RoutingError("Routing inventory fingerprint is invalid")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise RoutingError("Routing schema version is unsupported")
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
            raise RoutingError("Routing title inventory is invalid")
        if self.user_hint is not None and (
            not isinstance(self.user_hint, str) or self.user_hint not in _HINTS
        ):
            raise RoutingError("Routing user hint is invalid")
        if (
            not isinstance(self.evidence, tuple)
            or len(self.evidence) > _MAX_EVIDENCE
            or any(not isinstance(e, TitleRoutingEvidence) for e in self.evidence)
        ):
            raise RoutingError("Routing evidence is invalid")
        indexes = set(self.title_indexes)
        if any(e.title_index not in indexes for e in self.evidence):
            raise RoutingError("Routing evidence is outside the inventory")
        if len(set(self.evidence)) != len(self.evidence):
            raise RoutingError("Routing evidence is duplicated")

    def title_routes(self) -> tuple[TitleRoute, ...]:
        grouped: dict[int, list[TitleRoutingEvidence]] = {}
        for entry in self.evidence:
            grouped.setdefault(entry.title_index, []).append(entry)
        routes = []
        for index in self.title_indexes:
            entries = grouped.get(index, [])
            priority = max((_SOURCE_PRIORITY[e.source] for e in entries), default=0)
            roles = {e.role for e in entries if _SOURCE_PRIORITY[e.source] == priority}
            if len(roles) == 1:
                role = next(iter(roles))
                reason = "title_evidence"
                order = identification_order(role)
            else:
                role = "unknown"
                reason = "conflicting_evidence" if roles else "insufficient_evidence"
                # A hint orders searches, never changes an unknown into a fact.
                # Conflicts require classification before any preferred route.
                order = identification_order("mixed" if roles else self.user_hint)
            routes.append(TitleRoute(index, role, reason, order))
        return tuple(routes)

    @property
    def composition(self) -> str:
        roles = {route.role for route in self.title_routes()} - {"unknown"}
        if not roles:
            return "unknown"
        if {"tv", "movie"} <= roles:
            return "mixed"
        if "tv" in roles:
            return "tv_with_extras" if "extras" in roles else "tv"
        if "movie" in roles:
            return "movies_with_extras" if "extras" in roles else "movies"
        return "extras"

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["title_indexes"] = list(self.title_indexes)
        value["evidence"] = [
            asdict(e)
            for e in sorted(
                self.evidence, key=lambda e: (e.title_index, e.source, e.role)
            )
        ]
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: object) -> DiscRoutingAssessment:
        keys = {
            "inventory_fingerprint",
            "title_indexes",
            "user_hint",
            "evidence",
            "revision",
            "schema_version",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise RoutingError("Routing assessment structure is invalid")
        if not isinstance(value["title_indexes"], list) or not isinstance(
            value["evidence"], list
        ):
            raise RoutingError("Routing assessment sequences are invalid")
        if (
            len(value["title_indexes"]) > _MAX_TITLES
            or len(value["evidence"]) > _MAX_EVIDENCE
        ):
            raise RoutingError("Routing assessment exceeds its bounds")
        try:
            entries = tuple(TitleRoutingEvidence(**e) for e in value["evidence"])
            return cls(**{
                **value,
                "title_indexes": tuple(value["title_indexes"]),
                "evidence": entries,
            })
        except (TypeError, KeyError) as exc:
            raise RoutingError(
                "Routing assessment evidence structure is invalid"
            ) from exc
