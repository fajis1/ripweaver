"""Explicit SQLite persistence for immutable, non-authorizing routing revisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from mkv_episode_matcher.disc.routing import (
    DiscRoutingAssessment,
    RoutingError,
    TitleRoutingEvidence,
)


class DiscRoutingStore:
    """No default database, background worker, provider, or media access."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS disc_routing_revisions (
                    inventory_fingerprint TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    assessment_json TEXT NOT NULL,
                    assessment_sha256 TEXT NOT NULL,
                    PRIMARY KEY (inventory_fingerprint, revision)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS disc_routing_attempts (
                    inventory_fingerprint TEXT NOT NULL,
                    title_index INTEGER NOT NULL,
                    assessment_revision INTEGER NOT NULL,
                    route TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (inventory_fingerprint, title_index,
                                 assessment_revision, route)
                )"""
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> DiscRoutingAssessment:
        try:
            assessment = DiscRoutingAssessment.from_dict(
                json.loads(row["assessment_json"])
            )
        except (ValueError, TypeError) as exc:
            raise RoutingError("Stored routing assessment is invalid") from exc
        if (
            assessment.digest != row["assessment_sha256"]
            or assessment.inventory_fingerprint != row["inventory_fingerprint"]
            or assessment.revision != row["revision"]
        ):
            raise RoutingError("Stored routing assessment identity is invalid")
        return assessment

    def latest(self, fingerprint: str) -> DiscRoutingAssessment | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM disc_routing_revisions WHERE inventory_fingerprint=? "
                "ORDER BY revision DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            return self._decode(row) if row is not None else None

    def forget(self, fingerprint: str) -> tuple[int, int]:
        """Remove routing revisions and attempts for one explicitly forgotten disc."""
        with self._connect() as connection:
            revisions = connection.execute(
                "SELECT COUNT(*) FROM disc_routing_revisions WHERE inventory_fingerprint=?",
                (fingerprint,),
            ).fetchone()[0]
            attempts = connection.execute(
                "SELECT COUNT(*) FROM disc_routing_attempts WHERE inventory_fingerprint=?",
                (fingerprint,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM disc_routing_attempts WHERE inventory_fingerprint=?",
                (fingerprint,),
            )
            connection.execute(
                "DELETE FROM disc_routing_revisions WHERE inventory_fingerprint=?",
                (fingerprint,),
            )
            return int(revisions), int(attempts)

    def append(
        self,
        assessment: DiscRoutingAssessment,
        *,
        expected_revision: int,
    ) -> DiscRoutingAssessment:
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or assessment.revision != expected_revision + 1
        ):
            raise RoutingError("Routing expected revision is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM disc_routing_revisions WHERE inventory_fingerprint=? "
                "AND revision=?",
                (assessment.inventory_fingerprint, assessment.revision),
            ).fetchone()
            if existing is not None:
                saved = self._decode(existing)
                if saved.digest == assessment.digest:
                    return saved  # Retrying this exact revision is idempotent.
                raise RoutingError("Routing assessment revision is stale")
            latest = connection.execute(
                "SELECT * FROM disc_routing_revisions WHERE inventory_fingerprint=? "
                "ORDER BY revision DESC LIMIT 1",
                (assessment.inventory_fingerprint,),
            ).fetchone()
            previous = self._decode(latest) if latest is not None else None
            if (previous.revision if previous else 0) != expected_revision:
                raise RoutingError("Routing assessment revision is stale")
            if previous and previous.title_indexes != assessment.title_indexes:
                raise RoutingError("Routing revision cannot replace its inventory")
            connection.execute(
                "INSERT INTO disc_routing_revisions VALUES (?, ?, ?, ?)",
                (
                    assessment.inventory_fingerprint,
                    assessment.revision,
                    assessment.to_json(),
                    assessment.digest,
                ),
            )
        return assessment

    def save_observation(self, assessment: DiscRoutingAssessment) -> DiscRoutingAssessment:
        """Reuse identical evidence across revisions; reject competing changes."""
        previous = self.latest(assessment.inventory_fingerprint)
        if previous is not None:
            # A fresh inventory cannot revoke content learned from staged media.
            retained = tuple(e for e in previous.evidence if e.source == "content" and e not in assessment.evidence)
            assessment = replace(assessment, evidence=assessment.evidence + retained)
            candidate = replace(assessment, revision=previous.revision)
            if candidate.digest == previous.digest:
                return previous
        expected = previous.revision if previous else 0
        return self.append(
            replace(assessment, revision=expected + 1), expected_revision=expected
        )

    def record_attempt(
        self,
        fingerprint: str,
        title_index: int,
        revision: int,
        route: str,
        outcome: str,
    ) -> None:
        """Append one idempotent route outcome for restart-safe fallback."""
        from mkv_episode_matcher.disc.routing_controller import RouteAttempt

        attempt = RouteAttempt(revision, route, outcome)
        if attempt.assessment_revision != revision:
            raise RoutingError("Route attempt revision is invalid")
        with self._connect() as connection:
            connection.execute(
                "UPDATE disc_routing_attempts SET outcome=? WHERE inventory_fingerprint=? "
                "AND title_index=? AND assessment_revision=? AND route=? AND outcome='running'",
                (outcome, fingerprint, title_index, revision, route),
            )
            connection.execute(
                "INSERT OR IGNORE INTO disc_routing_attempts VALUES (?, ?, ?, ?, ?)",
                (fingerprint, title_index, revision, route, outcome),
            )

    def claim_next(self, assessment: DiscRoutingAssessment, title_index: int) -> str | None:
        """Atomically choose and reserve a route; interrupted reservations hold."""
        from mkv_episode_matcher.disc.routing_controller import RouteAttempt, next_route

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM disc_routing_revisions WHERE inventory_fingerprint=? AND revision=?",
                (assessment.inventory_fingerprint, assessment.revision),
            ).fetchone()
            if row is None or self._decode(row).digest != assessment.digest:
                raise RoutingError("Routing claim assessment is unavailable")
            latest_row = connection.execute(
                "SELECT * FROM disc_routing_revisions WHERE inventory_fingerprint=? ORDER BY revision DESC LIMIT 1",
                (assessment.inventory_fingerprint,),
            ).fetchone()
            latest = self._decode(latest_row)
            if {e for e in latest.evidence if e.title_index == title_index} != {
                e for e in assessment.evidence if e.title_index == title_index
            }:
                raise RoutingError("Routing claim title evidence is stale")
            running = connection.execute(
                "SELECT 1 FROM disc_routing_attempts WHERE inventory_fingerprint=? AND title_index=? AND outcome='running'",
                (assessment.inventory_fingerprint, title_index),
            ).fetchone()
            if running is not None:
                return None
            rows = connection.execute(
                "SELECT assessment_revision, route, outcome FROM disc_routing_attempts "
                "WHERE inventory_fingerprint=? AND title_index=? AND assessment_revision=?",
                (assessment.inventory_fingerprint, title_index, assessment.revision),
            ).fetchall()
            route = next_route(assessment, title_index=title_index, attempts=tuple(
                RouteAttempt(row[0], row[1], row[2]) for row in rows
            ))
            if route is not None:
                connection.execute("INSERT INTO disc_routing_attempts VALUES (?, ?, ?, ?, 'running')",
                    (assessment.inventory_fingerprint, title_index, assessment.revision, route))
            return route

    def attempts(
        self, fingerprint: str, title_index: int, revision: int
    ) -> tuple[object, ...]:
        """Load only attempts belonging to the exact assessment revision."""
        from mkv_episode_matcher.disc.routing_controller import RouteAttempt

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT assessment_revision, route, outcome FROM disc_routing_attempts "
                "WHERE inventory_fingerprint=? AND title_index=? AND assessment_revision=? "
                "ORDER BY route",
                (fingerprint, title_index, revision),
            ).fetchall()
        return tuple(RouteAttempt(row[0], row[1], row[2]) for row in rows)

    def record_content_role(
        self, base: DiscRoutingAssessment, title_index: int, role: str
    ) -> DiscRoutingAssessment:
        """Merge independent sibling results, refusing stale same-title evidence."""
        latest = self.latest(base.inventory_fingerprint)
        if latest is None or latest.title_indexes != base.title_indexes:
            raise RoutingError("Routing evidence has no matching inventory")
        if title_index not in base.title_indexes:
            raise RoutingError("Routing evidence title is outside inventory")
        old_title = {e for e in base.evidence if e.title_index == title_index}
        new_title = {e for e in latest.evidence if e.title_index == title_index}
        incoming = TitleRoutingEvidence(title_index, role, "content")
        if old_title != new_title and incoming not in new_title:
            raise RoutingError("Routing title evidence changed during analysis")
        if incoming in new_title:
            return latest
        return self.append(
            replace(latest, revision=latest.revision + 1, evidence=latest.evidence + (incoming,)),
            expected_revision=latest.revision,
        )
