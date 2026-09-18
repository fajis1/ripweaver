import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

def replace_test(match):
    return '''def test_m5_sibling_revision_idempotent_retry_fail(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import GeminiTitleOutcome, GeminiFallbackOutcome
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence, RoutingError
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json

    config_mock = SimpleNamespace(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_config_manager", lambda: type("m", (), {"load": lambda self: config_mock})())

    def fake_execute_gemini_fallback(store, media_ids, *args, **kwargs):
        contract_path = tmp_path / f"{media_ids[0]}_new.json"
        contract_path.write_text("{}", encoding="utf-8")
        from mkv_episode_matcher.pipeline_queue import build_artifact
        store.apply_reviewed_identification_input(media_ids[0], build_artifact("rip", contract_path))
        return GeminiFallbackOutcome(handled_ids=media_ids, titles=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    
    initial_assessment = DiscAssessment("0"*16, (1,2,3))
    initial_assessment = store.routing_append(initial_assessment, expected_revision=0)

    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {
                "routing_assessment": initial_assessment.to_dict(),
                "routing_assessment_digest": initial_assessment.digest,
                "routing_assessment_revision": initial_assessment.revision
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_queue_store", lambda: store)

    worker = DownstreamWorker(SimpleNamespace(store=store), allowed_stages=("identify",))

    real_routing_append = store.routing_append
    
    call_count = 0
    def mock_routing_append(assessment, expected_revision=None):
        nonlocal call_count
        call_count += 1
        from dataclasses import replace
        current = store.routing_latest("0"*16)
        sibling = replace(current, revision=current.revision + 1, evidence=current.evidence + (TitleEvidence(title_index=100+call_count, source="content", status="supported", role="tv"),))
        real_routing_append(sibling, expected_revision=current.revision)
        raise RoutingError("Routing revision is stale")
        
    store.routing_append = mock_routing_append

    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        with mock.patch("mkv_episode_matcher.backend.downstream_worker._verified_gemini_route_assignment", return_value=True):
            assert worker._apply_automatic_assessed_gemini_route() is False
'''

code = re.sub(
    r'def test_m5_sibling_revision_idempotent_retry_fail\(.*?\n(?=def test_manual_endpoints_queue_transition_lock)',
    replace_test,
    code,
    flags=re.DOTALL
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
