import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace monkeypatch of execute_gemini_fallback with unittest.mock.patch context manager

code = code.replace(
'''    monkeypatch.setattr("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback)
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)''',
'''    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)'''
)

# And wrap the assert lines in the context manager
# For test_m5_sibling_revision_idempotent_retry:
code = code.replace(
'''    store.routing_append = mock_routing_append

    assert worker._apply_automatic_assessed_gemini_route() is True
    assert store.get(media_id).state == "identify_running"''',
'''    store.routing_append = mock_routing_append

    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        assert worker._apply_automatic_assessed_gemini_route() is True
        assert store.get(media_id).state == "identify_running"'''
)

# For test_m5_sibling_revision_idempotent_retry_fail:
code = code.replace(
'''    store.routing_append = mock_routing_append

    assert worker._apply_automatic_assessed_gemini_route() is False''',
'''    store.routing_append = mock_routing_append

    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        assert worker._apply_automatic_assessed_gemini_route() is False'''
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
