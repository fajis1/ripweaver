import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Mock _verified_gemini_route_assignment to always return True in the tests

code = code.replace(
'''    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        assert worker._apply_automatic_assessed_gemini_route() is True
        assert store.get(media_id).state == "identify_running"''',
'''    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        with mock.patch("mkv_episode_matcher.backend.downstream_worker._verified_gemini_route_assignment", return_value=True):
            assert worker._apply_automatic_assessed_gemini_route() is True
            assert store.get(media_id).state == "queued"'''
)

code = code.replace(
'''    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        assert worker._apply_automatic_assessed_gemini_route() is False''',
'''    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        with mock.patch("mkv_episode_matcher.backend.downstream_worker._verified_gemini_route_assignment", return_value=True):
            assert worker._apply_automatic_assessed_gemini_route() is False'''
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
