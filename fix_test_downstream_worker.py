import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
in_test = False
test_code = '''def test_manual_endpoints_queue_transition_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.routers.rip import execute_pipeline_gemini_fallback, GeminiFallbackExecutionRequest
    from mkv_episode_matcher.backend.automatic_rip import _downstream_lock
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json
    import threading
    import time
    import unittest.mock as mock

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

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    _downstream_lock.acquire()
    try:
        request = GeminiFallbackExecutionRequest(media_ids=[media_id], confirm_media_read=True, confirm_external_transmission=True, confirm_classification=True, disc_fingerprint="0"*16)
        
        with mock.patch("mkv_episode_matcher.backend.routers.rip.execute_gemini_fallback") as mock_exec:
            mock_exec.side_effect = lambda store, ids, *a: [ids[0]]
            response = execute_pipeline_gemini_fallback(request, store, tmp_path)
            assert store.get(media_id).review_code == "gemini_evidence_required"
            
            _downstream_lock.release()
            time.sleep(0.1)
            
            assert store.get(media_id).review_code == "gemini_analysis_running" or store.get(media_id).state != "review_required"
    except Exception:
        if _downstream_lock.locked():
            _downstream_lock.release()
        raise
'''

found = False
for line in lines:
    if line.startswith('def test_manual_endpoints_queue_transition_lock'):
        in_test = True
        found = True
        new_lines.append(test_code)
    elif in_test:
        if line.startswith('def ') and not line.startswith('def test_manual_endpoints_queue_transition_lock'):
            in_test = False
            new_lines.append(line)
    else:
        new_lines.append(line)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
