"""One-use, owner-authorized Season 8 Disc 2 metadata context repair."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from mkv_episode_matcher.backend.dependencies import (
    get_pipeline_contract_root,
    get_pipeline_queue_store,
)
from mkv_episode_matcher.backend.legacy_context_repair import (
    upgrade_legacy_disc_context,
)
from mkv_episode_matcher.disc.content_policy import parse_tv_disc_label_context
from mkv_episode_matcher.disc.rip_manifest import MediaContext


def main() -> None:  # noqa: C901
    with urllib.request.urlopen(
        "http://127.0.0.1:8001/rip/drives", timeout=30
    ) as response:
        drive_payload = json.load(response)
    targets = [
        drive
        for drive in drive_payload.get("drives", [])
        if drive.get("drive_index") == 0
    ]
    if len(targets) != 1:
        raise RuntimeError("Expected optical drive 1 was not found exactly once")
    fingerprint = targets[0].get("current_disc_fingerprint")
    parsed_context = parse_tv_disc_label_context(targets[0].get("disc_label"))
    if not isinstance(fingerprint, str) or len(fingerprint) != 16:
        raise RuntimeError("Optical drive 1 does not have an exact saved identity")
    if (
        parsed_context is None
        or parsed_context.season != 8
        or parsed_context.disc_number != 2
    ):
        raise RuntimeError("Optical drive 1 is not the authorized Season 8 Disc 2")

    store = get_pipeline_queue_store()
    if not store.is_paused():
        raise RuntimeError("Pipeline queue is not paused")
    if store.disc_matching_scope(fingerprint) != tuple(range(1, 9)):
        raise RuntimeError("Drive 1 matching scope is not the authorized title set")
    if store.title_history(fingerprint):
        raise RuntimeError("Drive 1 already has accepted title history")

    backup_dir = store.database_path.parent / "private-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        "pipeline-before-s8d2-context-repair-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-")
        + uuid4().hex[:8]
        + ".sqlite3"
    )
    source_connection = sqlite3.connect(store.database_path, timeout=30)
    backup_connection = sqlite3.connect(backup_path, timeout=30)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()

    verification_connection = sqlite3.connect(backup_path, timeout=30)
    try:
        if verification_connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Private SQLite backup verification failed")
    finally:
        verification_connection.close()

    repaired = upgrade_legacy_disc_context(
        store,
        disc_fingerprint=fingerprint,
        fresh_context=MediaContext(
            disc_id="disc-01",
            series_name="The Office",
            season=8,
            disc_number=2,
            content_hint="tv",
        ),
        contract_root=get_pipeline_contract_root(),
    )
    if len(repaired) != 8:
        raise RuntimeError("Drive 1 did not repair the exact eight-title set")
    if not store.is_paused():
        raise RuntimeError("Pipeline queue unexpectedly resumed")

    for item in repaired:
        payload = json.loads(
            store.rip_artifact(item.media_id).contract_path.read_text(encoding="utf-8")
        )
        context = payload.get("media_context")
        if (
            item.state != "queued"
            or item.stage != "identify"
            or not isinstance(context, dict)
            or context.get("series_name") != "The Office"
            or context.get("season") != 8
            or context.get("disc_number") != 2
            or context.get("content_hint") != "tv"
        ):
            raise RuntimeError("Drive 1 repaired state did not verify")

    print(
        json.dumps(
            {
                "private_backup_created": backup_path.is_file(),
                "private_backup_verified": True,
                "repaired_title_count": len(repaired),
                "series_name": "The Office",
                "season": 8,
                "disc_number": 2,
                "content_hint": "tv",
                "stage": "identify",
                "state": "queued",
                "queue_paused": store.is_paused(),
                "media_mutated": False,
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
