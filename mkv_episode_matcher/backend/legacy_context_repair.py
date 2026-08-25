"""Safe metadata-only repair for verified legacy television rip contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
    QueuedPipelineItem,
    build_artifact,
)


def upgrade_legacy_disc_context(  # noqa: C901
    store: PipelineQueueStore,
    *,
    disc_fingerprint: str,
    fresh_context: MediaContext,
    contract_root: Path,
) -> tuple[QueuedPipelineItem, ...]:
    """Upgrade one complete legacy disc only when fresh identity is unambiguous."""

    if (
        not store.is_paused()
        or fresh_context.season is None
        or fresh_context.content_hint != "tv"
    ):
        return ()
    scope = store.disc_matching_scope(disc_fingerprint)
    if not scope:
        return ()

    selected: dict[int, tuple[QueuedPipelineItem, dict[str, object]]] = {}
    for item in store.list_items():
        artifact = store.rip_artifact(item.media_id)
        try:
            payload = json.loads(artifact.contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Verified rip contract is invalid") from exc
        if payload.get("disc_fingerprint") != disc_fingerprint:
            continue
        title_index = payload.get("title_index")
        if (
            isinstance(title_index, bool)
            or not isinstance(title_index, int)
            or title_index < 0
        ):
            raise PipelineQueueError("Verified disc title identity is invalid")
        if title_index in selected:
            raise PipelineQueueError("Verified disc title identity is duplicated")
        selected[title_index] = (item, payload)
    if not selected or set(selected) != set(scope):
        return ()
    if any(
        item.state != "review_required"
        or item.stage != "identify"
        or not isinstance(payload.get("media_context"), dict)
        or payload["media_context"].get("season") is not None
        or payload["media_context"].get("episode_assignments") not in (None, [], ())
        for item, payload in selected.values()
    ):
        return ()

    existing_series_names = {
        str(payload["media_context"].get("series_name")).strip()
        for _item, payload in selected.values()
        if isinstance(payload["media_context"].get("series_name"), str)
        and str(payload["media_context"].get("series_name")).strip()
        and str(payload["media_context"].get("series_name")).casefold()
        not in {"unmatched", "unknown"}
    }
    if len(existing_series_names) > 1:
        raise PipelineQueueError("Legacy disc series context is inconsistent")
    series_name = (
        next(iter(existing_series_names))
        if existing_series_names
        else fresh_context.series_name.strip()
    )

    contract_root.mkdir(parents=True, exist_ok=True)
    replacements = {}
    for title_index in sorted(selected):
        item, payload = selected[title_index]
        original_context = payload["media_context"]
        revised = dict(payload)
        revised["media_context"] = {
            **original_context,
            "series_name": series_name,
            "season": fresh_context.season,
            "disc_number": fresh_context.disc_number,
            "content_hint": "tv",
        }
        serialized = json.dumps(revised, indent=2, sort_keys=True) + "\n"
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        path = contract_root / f"{item.media_id}.recontextualized-{digest}.json"
        if path.exists():
            try:
                if path.read_text(encoding="utf-8") != serialized:
                    raise PipelineQueueError(
                        "Digest-bound context repair contract has different content"
                    )
            except OSError as exc:
                raise PipelineQueueError(
                    "Context repair contract could not be read"
                ) from exc
        else:
            try:
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(serialized)
            except OSError as exc:
                raise PipelineQueueError(
                    "Context repair contract could not be written"
                ) from exc
        replacements[title_index] = build_artifact("rip", path)

    return store.repair_legacy_disc_media_context(
        disc_fingerprint,
        replacements,
        expected_series_name=series_name,
        expected_season=fresh_context.season,
        expected_disc_number=fresh_context.disc_number,
    )
