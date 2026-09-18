import re

with open('mkv_episode_matcher/backend/routers/rip.py', 'r', encoding='utf-8') as f:
    text = f.read()

model = '''class ShortTitleDispositionRequest(BaseModel):'''
new_model = '''class DiscReassessmentRequest(BaseModel):
    content_hint: Literal["tv", "movie", "extras"] | None = None
    confirm_reassessment: bool = False

class ShortTitleDispositionRequest(BaseModel):'''
text = text.replace(model, new_model)

endpoint = '''@router.post("/drives/{drive_index}/forget-disc-identity")'''
new_endpoint = '''@router.post("/pipeline/discs/{disc_fingerprint}/reassess")
def reassess_disc_metadata(
    disc_fingerprint: str,
    request: DiscReassessmentRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Reassess a known disc without accessing a physical drive."""
    if not request.confirm_reassessment:
        raise HTTPException(
            status_code=400, detail="Reassessment confirmation is required"
        )
    latest = store.routing_latest(disc_fingerprint)
    if not latest:
        raise HTTPException(status_code=404, detail="No existing disc assessment found")
        
    from dataclasses import replace
    new_assessment = replace(latest, user_hint=request.content_hint, revision=latest.revision + 1)
    store.routing_append(new_assessment, expected_revision=latest.revision)
    return {"status": "reassessed", "disc_fingerprint": disc_fingerprint}

@router.post("/drives/{drive_index}/forget-disc-identity")'''

text = text.replace(endpoint, new_endpoint)

with open('mkv_episode_matcher/backend/routers/rip.py', 'w', encoding='utf-8') as f:
    f.write(text)
