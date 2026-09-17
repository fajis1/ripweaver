import re

with open("mkv_episode_matcher/backend/unmatched_disc_analysis.py", "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace(
    "    if unresolved_for_gemini and allow_gemini:",
    "    explicit_tv_no_match: set[str] = set()\n    if unresolved_for_gemini and allow_gemini:"
)

old_confirmation_reason = \"\"\"                confirmation_reason = (
                    "accepted"
                    if file_id in resolved_matches
                    else "gemini_returned_no_episode"
                    if match.episode_id is None
                    else "gemini_passes_disagreed"
                    if match.episode_id != initial_match.episode_id
                    else "gemini_initial_below_confidence_threshold"
                    if initial_match.confidence < automatic_min_confidence
                    else "gemini_confirmation_below_confidence_threshold"
                    if match.confidence < automatic_min_confidence
                    else "runtime_mismatch"
                    if not runtime_consistent
                    else "gemini_candidate_rejected"
                )\"\"\"
new_confirmation_reason = old_confirmation_reason + \"\"\"
                if (
                    season is None
                    and match.episode_id is None
                    and initial_match.episode_id is None
                    and match.confidence >= automatic_min_confidence
                    and initial_match.confidence >= automatic_min_confidence
                ):
                    explicit_tv_no_match.add(file_id)\"\"\"

content = content.replace(old_confirmation_reason, new_confirmation_reason)

old_fallback_1 = \"\"\"                if (
                    item.state == "review_required"
                    and item.review_code != "visual_content_review_required"
                ):
                    store.choose_review_path(
                        media_id, "independent_episode_evidence_required"
                    )
    elif unresolved:
        for media_id in unresolved:
            store.choose_review_path(media_id, "independent_episode_evidence_required")\"\"\"

new_fallback_1 = \"\"\"                if (
                    item.state == "review_required"
                    and item.review_code != "visual_content_review_required"
                ):
                    if media_id in explicit_tv_no_match:
                        store.choose_review_path(media_id, "tv_title_no_match")
                    else:
                        store.choose_review_path(
                            media_id, "independent_episode_evidence_required"
                        )
    elif unresolved:
        for media_id in unresolved:
            if media_id in explicit_tv_no_match:
                store.choose_review_path(media_id, "tv_title_no_match")
            else:
                store.choose_review_path(media_id, "independent_episode_evidence_required")\"\"\"

content = content.replace(old_fallback_1, new_fallback_1)

with open("mkv_episode_matcher/backend/unmatched_disc_analysis.py", "w", encoding="utf-8") as f:
    f.write(content)
