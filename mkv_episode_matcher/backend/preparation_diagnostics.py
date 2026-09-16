"""Bounded diagnostics without exception messages, paths, or provider payloads."""

import re

from fastapi import HTTPException

_REASONS = {
    "Selected disc disappeared during inventory": "disc-disappeared",
    "MakeMKV returned no titles for the selected disc": "empty-inventory",
    "Configure an existing MakeMKV rip staging root before preparing a pipeline": "staging-root-unavailable",
    "Media context structure is invalid": "invalid-media-context",
    "Saved inventory lacks complete batch title metadata": "incomplete-batch-metadata",
    "The failed rip no longer contains a verifiable relevant title": "empty-recovery-scope",
}


def safe_preparation_failure(exc: Exception) -> dict[str, object]:
    """Only exact known literals and bounded application code locations may escape."""
    reason = "unclassified"
    locations: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        detail = (
            current.detail
            if isinstance(current, HTTPException)
            else (current.args[0] if len(current.args) == 1 else None)
        )
        if isinstance(detail, str) and detail in _REASONS:
            reason = _REASONS[detail]
        frame = current.__traceback__
        while frame is not None:
            module = frame.tb_frame.f_globals.get("__name__", "")
            function = frame.tb_frame.f_code.co_name
            if (
                isinstance(module, str)
                and module.startswith("mkv_episode_matcher.")
                and re.fullmatch(r"[A-Za-z0-9_.]+", module)
                and re.fullmatch(r"[A-Za-z0-9_]+", function)
            ):
                locations.append(f"{module}:{function}:{frame.tb_lineno}")
            frame = frame.tb_next
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {"reason_code": reason, "code_locations": locations[-6:]}
