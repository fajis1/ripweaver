import os

m5_tests = '''
def test_m5_sibling_revision_idempotent_retry(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import GeminiTitleOutcome
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json

    config_mock = SimpleNamespace(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_config_manager", lambda: type("m", (), {"load": lambda: config_mock})())

    class FakeEngine:
        asr = None
        gemini_fallback = None
        def execute_gemini_fallback(self, *args, **kwargs):
            return type("GeminiFallbackOutcome", (), {"errors": (), "results": (GeminiTitleOutcome(media_id=media_id, disposition="matched", accepted_role="movie"),)})()
            
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine", lambda: FakeEngine())
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {},
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")
    media_id = store.list_items()[0].media_id
    
    initial_assessment = DiscAssessment("0"*16, (1,2,3))
    initial_assessment = store.routing_append(initial_assessment)

    # Monkeypatch get_pipeline_queue_store
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_pipeline_queue_store", lambda: store)

    worker = DownstreamWorker(SimpleNamespace(store=store), allowed_stages=("identify",))

    # To simulate sibling appending the same role, we will mock routing_append to first advance the revision with the same role, then call real routing_append.
    real_routing_append = store.routing_append
    
    call_count = 0
    def mock_routing_append(assessment, expected_revision=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Sibling advances revision!
            from dataclasses import replace
            sibling_assessment = replace(initial_assessment, revision=initial_assessment.revision + 1, evidence=(TitleEvidence(title_index=1, source="content", status="supported", role="movie"),))
            real_routing_append(sibling_assessment, expected_revision=initial_assessment.revision)
            
        return real_routing_append(assessment, expected_revision=expected_revision)
        
    store.routing_append = mock_routing_append

    # Test that the retry logic catches the same role and safely returns True
    assert worker._apply_automatic_assessed_gemini_route() is True
    assert store.get(media_id).state == "identify_running"

def test_m5_sibling_revision_idempotent_retry_fail(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import GeminiTitleOutcome
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json

    config_mock = SimpleNamespace(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_config_manager", lambda: type("m", (), {"load": lambda: config_mock})())

    class FakeEngine:
        asr = None
        gemini_fallback = None
        def execute_gemini_fallback(self, *args, **kwargs):
            return type("GeminiFallbackOutcome", (), {"errors": (), "results": (GeminiTitleOutcome(media_id=media_id, disposition="matched", accepted_role="movie"),)})()
            
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine", lambda: FakeEngine())
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {},
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")
    media_id = store.list_items()[0].media_id
    
    initial_assessment = DiscAssessment("0"*16, (1,2,3))
    initial_assessment = store.routing_append(initial_assessment)

    # Monkeypatch get_pipeline_queue_store
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_pipeline_queue_store", lambda: store)

    worker = DownstreamWorker(SimpleNamespace(store=store), allowed_stages=("identify",))

    real_routing_append = store.routing_append
    
    call_count = 0
    def mock_routing_append(assessment, expected_revision=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Sibling advances revision but with a DIFFERENT role
            from dataclasses import replace
            sibling_assessment = replace(initial_assessment, revision=initial_assessment.revision + 1, evidence=(TitleEvidence(title_index=1, source="content", status="supported", role="tv"),))
            real_routing_append(sibling_assessment, expected_revision=initial_assessment.revision)
            
        return real_routing_append(assessment, expected_revision=expected_revision)
        
    store.routing_append = mock_routing_append

    # Test that the retry logic catches a different role and fails (or fails appending eventually)
    # Actually wait, if the sibling advanced it with a different role, and we are still trying to append "movie", it will hit a RoutingError, and we expect _apply_automatic_assessed_gemini_route to return False!
    
    assert worker._apply_automatic_assessed_gemini_route() is False

def test_manual_endpoints_queue_transition_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.routers.rip import execute_pipeline_gemini_fallback, GeminiFallbackExecutionRequest
    from mkv_episode_matcher.backend.automatic_rip import _downstream_lock
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json
    import threading
    import time

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {},
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine", lambda: SimpleNamespace(execute_gemini_fallback=lambda *a,**k: None))
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    _downstream_lock.acquire()
    try:
        # lock is held by us (simulating worker).
        # if the endpoint transitions the queue outside the lock, the state will change NOW.
        # if the endpoint transitions the queue inside the lock, it will BLOCK (or fail).
        
        request = GeminiFallbackExecutionRequest(media_ids=[media_id], confirm_media_read=True, confirm_external_transmission=True, confirm_classification=True, disc_fingerprint="0"*16)
        
        # We must run it in a thread so it doesn't block this test!
        # Actually execute_pipeline_gemini_fallback already runs the hard part in a thread and returns {"status": "queued"}.
        response = execute_pipeline_gemini_fallback(request, store, tmp_path)

        # Verify the queue state is still untouched!
        assert store.get(media_id).review_code == "gemini_evidence_required"
        
    finally:
        _downstream_lock.release()
        
    time.sleep(0.1)
    
    # Should have transitioned or finished.
    assert store.get(media_id).review_code == "gemini_analysis_running" or store.get(media_id).state != "review_required"
'''

with open('tests/test_downstream_worker.py', 'a', encoding='utf-8') as f:
    f.write('\n\n')
    f.write(m5_tests)
