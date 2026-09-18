import re

with open('mkv_episode_matcher/backend/routers/rip.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Add the fields to PipelineItemResponse
response_model = '''class PipelineItemResponse(BaseModel):
    media_id: str
    artifact_sha256: str
    disc_fingerprint: str | None = None
    title_index: int | None = None
    duration_seconds: int | None = None
    short_title: bool = False
    short_title_review_threshold_seconds: int | None = None
    series_name: str | None = None
    display_name: str | None = None
    match_summary: str | None = None
    user_hint: str | None = None
    assessed_role: str | None = None
    current_route: str | None = None
    evidence_status: str | None = None
    location_label: str'''

text = re.sub(r'class PipelineItemResponse\(BaseModel\):.*?location_label: str', response_model, text, flags=re.DOTALL)

# Add them to _pipeline_item_response
response_assignment = '''    user_hint = None
    assessed_role = None
    current_route = None
    evidence_status = None
    
    if disc_fingerprint and title_index is not None:
        try:
            from mkv_episode_matcher.pipeline_queue import get_pipeline_queue_store
            store_instance = get_pipeline_queue_store()
            assessment = store_instance.routing_latest(disc_fingerprint)
            if assessment:
                user_hint = assessment.user_hint
                evidence = next((e for e in assessment.evidence if e.title_index == title_index), None)
                if evidence:
                    assessed_role = evidence.role
                    evidence_status = evidence.status
                attempts = store_instance.routing_attempts(disc_fingerprint, title_index)
                if attempts:
                    current_route = attempts[-1].route
        except Exception:
            pass

    response = {
        "media_id": item.media_id,
        "artifact_sha256": item.artifact.contract_sha256,
        "disc_fingerprint": disc_fingerprint,
        "title_index": title_index,
        "duration_seconds": duration_seconds,
        "short_title": bool(
            duration_seconds is not None
            and short_title_review_threshold_seconds
            and 0 < duration_seconds < short_title_review_threshold_seconds
        ),
        "short_title_review_threshold_seconds": (short_title_review_threshold_seconds),
        "series_name": series_name,
        "display_name": _pipeline_item_display_name(item, payload),
        "match_summary": _pipeline_item_match_summary(item, payload),
        "user_hint": user_hint,
        "assessed_role": assessed_role,
        "current_route": current_route,
        "evidence_status": evidence_status,'''

text = re.sub(r'    response = \{\s*"media_id": item.media_id,.*?"match_summary": _pipeline_item_match_summary\(item, payload\),', response_assignment, text, flags=re.DOTALL)

with open('mkv_episode_matcher/backend/routers/rip.py', 'w', encoding='utf-8') as f:
    f.write(text)
